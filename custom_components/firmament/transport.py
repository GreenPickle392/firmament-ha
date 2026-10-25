"""BLE link for the SP642E: persistent connection, supervised reconnect,
single-writer command serialisation.

Two properties make this more reliable than connecting on demand:

1. Every GATT write happens on the writer task, so a service call and a status
   query can never interleave on one characteristic.
2. Reconnect is started by the disconnect callback and gated on a fresh
   advertisement, so a controller that is powered off or held by the phone app
   is not hammered with connect attempts that cannot succeed.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from bleak_retry_connector import (
    BLEAK_RETRY_EXCEPTIONS,
    BleakClientWithServiceCache,
    BleakNotFoundError,
    close_stale_connections,
    establish_connection,
    get_device,
)
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback

from .const import (
    BACKOFF_BASE,
    BACKOFF_CAP,
    BACKOFF_JITTER,
    COMMAND_SETTLE,
    NOTIFY_TIMEOUT,
    RSSI_BAD,
    WRITE_TIMEOUT,
)
from .protocol import FrameReader

try:  # BleakDBusError moved between bleak majors.
    from bleak.exc import BleakDBusError
except ImportError:  # pragma: no cover
    class BleakDBusError(Exception):  # type: ignore[no-redef]
        """Placeholder when bleak exposes no D-Bus errors on this platform."""

_LOGGER = logging.getLogger(__name__)

SERVICE_UUIDS = (
    "0000ffe0-0000-1000-8000-00805f9b34fb",
    "0000e0ff-0000-1000-8000-00805f9b34fb",
)
WRITE_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

# A connect that drops this quickly, more than once in a row, means another
# central is taking the controller off us - the usual cause is the phone app.
QUICK_DROP_SECONDS = 5.0
QUICK_DROP_LIMIT = 3


class LinkState:
    DISCONNECTED = "disconnected"
    SCANNING = "waiting for advertisement"
    CONNECTING = "connecting"
    CONFIGURING = "configuring gatt"
    READY = "connected"
    BACKOFF = "backing off"
    EVICTED = "evicted by another central"


class LinkError(Exception):
    """The link could not deliver a command."""


class ConnectionLost(LinkError):
    """The link dropped while a transaction was in flight."""


@dataclass(slots=True)
class Transaction:
    frame: bytearray
    expect_cmd: int | None = None
    coalesce_key: str | None = None
    result: asyncio.Future | None = None
    reply_ready: asyncio.Future | None = None
    reply: bytearray | None = None


@dataclass(slots=True)
class LinkStats:
    connects: int = 0
    drops: int = 0
    reconnect_failures: int = 0
    writes: int = 0
    superseded: int = 0
    frames_in: int = 0
    last_error: str | None = None
    last_rx: float | None = None


class Sp642eLink:
    """Owns one persistent GATT connection to a controller."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        name: str,
        on_frame: Callable[[bytearray], None],
        on_state: Callable[[], None],
    ) -> None:
        self._hass = hass
        self._address = address
        self._name = name
        self._on_frame = on_frame
        self._on_state = on_state

        self._client: BleakClientWithServiceCache | None = None
        self._write_char: Any = None
        self._ble_device: Any = None

        self._queue: deque[Transaction] = deque()
        self._by_key: dict[str, Transaction] = {}
        self._awaiting: Transaction | None = None
        self._reader = FrameReader()

        self._work = asyncio.Event()
        self._reconnect = asyncio.Event()
        self._advertised = asyncio.Event()

        self._connect_lock = asyncio.Lock()
        self._writer: asyncio.Task | None = None
        self._supervisor: asyncio.Task | None = None
        self._closing = False

        self._state = LinkState.DISCONNECTED
        self._backoff = 0.0
        self._backoff_until = 0.0
        self._connected_at = 0.0
        self._quick_drops = 0
        self._recent_rx: deque[bytes] = deque(maxlen=24)

        self.stats = LinkStats()

    # ------------------------------------------------------------------ public

    @property
    def state(self) -> str:
        return self._state

    @property
    def ready(self) -> bool:
        return self._state == LinkState.READY and self._client is not None

    @property
    def rssi(self) -> int:
        info = bluetooth.async_last_service_info(
            self._hass, self._address, connectable=True
        )
        return info.advertisement.rssi if info else RSSI_BAD

    async def async_start(self) -> None:
        self._closing = False
        if self._writer is None or self._writer.done():
            self._writer = asyncio.create_task(self._writer_loop(), name=self._name)
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = asyncio.create_task(
                self._supervisor_loop(), name=f"{self._name}-supervisor"
            )
        self._set_state(LinkState.DISCONNECTED)
        self._reconnect.set()
        self._work.set()

    async def async_stop(self) -> None:
        self._closing = True
        for task in (self._writer, self._supervisor):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("%s: task raised during shutdown", self._name)
        self._writer = None
        self._supervisor = None
        await self._teardown("link stopped")
        self._set_state(LinkState.DISCONNECTED)

    async def async_send(
        self,
        frame: bytearray,
        expect_cmd: int | None = None,
        coalesce_key: str | None = None,
    ) -> bytearray | None:
        """Queue a transaction and await its outcome.

        Raises LinkError when the link cannot deliver within the write budget,
        so the caller can stay optimistic instead of hanging a service call
        behind a 60 s backoff.
        """
        if self._closing:
            raise ConnectionLost("link stopped")

        loop = asyncio.get_running_loop()
        tx = Transaction(
            frame=frame,
            expect_cmd=expect_cmd,
            coalesce_key=coalesce_key,
            result=loop.create_future(),
            reply_ready=loop.create_future() if expect_cmd is not None else None,
        )

        if coalesce_key is not None:
            self._supersede(coalesce_key, tx)

        self._queue.append(tx)
        self._work.set()

        assert tx.result is not None
        try:
            async with asyncio.timeout(WRITE_TIMEOUT):
                return await tx.result
        except TimeoutError:
            self._forget(tx)
            raise LinkError(
                f"{self._name} did not accept the command within "
                f"{WRITE_TIMEOUT:.0f}s (link {self._state})"
            ) from None

    @callback
    def async_advertisement(self, seen: bool) -> None:
        """Feed the supervisor with presence from the bluetooth integration."""
        if seen:
            self._advertised.set()
        else:
            self._advertised.clear()

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "backoff_seconds": round(self._backoff, 1),
            "backoff_remaining": round(
                max(0.0, self._backoff_until - asyncio.get_running_loop().time()), 1
            ),
            "queued": len(self._queue),
            "quick_drops": self._quick_drops,
            "advertising": self._advertised.is_set(),
            "stats": {
                "connects": self.stats.connects,
                "drops": self.stats.drops,
                "reconnect_failures": self.stats.reconnect_failures,
                "writes": self.stats.writes,
                "superseded": self.stats.superseded,
                "frames_in": self.stats.frames_in,
                "last_error": self.stats.last_error,
            },
            "recent_rx": [chunk.hex(" ") for chunk in list(self._recent_rx)[-6:]],
        }

    # ------------------------------------------------------------- writer task

    async def _writer_loop(self) -> None:
        while not self._closing:
            if not self._queue:
                self._work.clear()
                try:
                    async with asyncio.timeout(30):
                        await self._work.wait()
                except TimeoutError:
                    continue
            while self._queue and not self._closing:
                tx = self._queue.popleft()
                await self._execute(tx)

    async def _execute(self, tx: Transaction) -> None:
        if tx.result is None or tx.result.done():  # superseded or already given up
            return

        if not await self._ensure_connected():
            self._reconnect.set()
            await self._wait_for_link()
            if not await self._ensure_connected():
                self._reject(
                    tx, ConnectionLost(f"link unavailable ({self._state})")
                )
                return

        self._awaiting = tx
        try:
            try:
                await self._write(tx)
            except LinkError as err:
                self._note_error(err)
                self._reject(tx, err)
                return

            if tx.expect_cmd is None:
                self._resolve(tx, None)
                return

            assert tx.reply_ready is not None
            try:
                async with asyncio.timeout(NOTIFY_TIMEOUT):
                    await tx.reply_ready
            except TimeoutError:
                self._reject(
                    tx,
                    LinkError(
                        f"no 0x{tx.expect_cmd:02x} reply from {self._name} "
                        f"within {NOTIFY_TIMEOUT:.0f}s"
                    ),
                )
                return
            except LinkError as err:
                # The link was torn down while the reply was outstanding.
                self._note_error(err)
                self._reject(tx, err)
                return
            self._resolve(tx, tx.reply)
            return
        except asyncio.CancelledError:
            if tx.result is not None and not tx.result.done():
                tx.result.cancel()
            raise
        finally:
            if self._awaiting is tx:
                self._awaiting = None
            if tx.reply_ready is not None and not tx.reply_ready.done():
                tx.reply_ready.cancel()

    def _resolve(self, tx: Transaction, value: bytearray | None) -> None:
        if tx.result is not None and not tx.result.done():
            tx.result.set_result(value)

    def _reject(self, tx: Transaction, err: Exception) -> None:
        if tx.result is not None and not tx.result.done():
            tx.result.set_exception(err)

    async def _write(self, tx: Transaction) -> None:
        client = self._client
        if client is None or self._write_char is None:
            raise ConnectionLost("not connected")
        try:
            await client.write_gatt_char(self._write_char, tx.frame, response=True)
        except BleakDBusError as err:
            await self._teardown(f"dbus write error: {err}")
            self._reconnect.set()
            raise LinkError(f"gatt write failed: {err}") from err
        except BLEAK_RETRY_EXCEPTIONS as err:
            await self._teardown(f"bleak write error: {err}")
            self._reconnect.set()
            raise LinkError(f"gatt write failed: {err}") from err

        self.stats.writes += 1
        # Another frame is queued for this same connection; give the controller
        # a moment between writes.
        if self._queue:
            await asyncio.sleep(COMMAND_SETTLE)

    # -------------------------------------------------------------- supervisor

    async def _supervisor_loop(self) -> None:
        """Restore the link as soon as it is lost, without waiting for a poll."""
        while not self._closing:
            await self._reconnect.wait()
            self._reconnect.clear()
            if self._closing or self.ready:
                continue
            await self._reconnect_now()

    async def _reconnect_now(self) -> None:
        while not self._closing and not self.ready:
            await self._wait_for_link()
            if self._closing or self.ready:
                return
            try:
                await self._ensure_connected()
            except asyncio.CancelledError:
                raise
            except LinkError as err:
                self.stats.reconnect_failures += 1
                self._note_error(err)
                _LOGGER.info("%s: reconnect attempt failed: %s", self._name, err)

    async def _wait_for_link(self) -> None:
        """Hold until the controller is advertising and the backoff has elapsed."""
        if not self._advertised.is_set():
            self._set_state(LinkState.SCANNING)
            try:
                async with asyncio.timeout(BACKOFF_CAP):
                    await self._advertised.wait()
            except TimeoutError:
                bluetooth.async_rediscover_address(self._hass, self._address)

        remaining = self._backoff_until - asyncio.get_running_loop().time()
        if remaining > 0:
            self._set_state(LinkState.BACKOFF)
            try:
                async with asyncio.timeout(remaining):
                    await asyncio.sleep(remaining)
            except TimeoutError:
                pass
        elif self._backoff > 0:
            self._arm_backoff()

    def _arm_backoff(self) -> None:
        """Schedule the next attempt at the current step, then grow the step."""
        loop = asyncio.get_running_loop()
        jitter = self._backoff * BACKOFF_JITTER
        delay = max(0.2, self._backoff + random.uniform(-jitter, jitter))
        self._backoff_until = loop.time() + delay
        self._backoff = min(BACKOFF_CAP, self._backoff * 2)

    def _on_link_failure(self) -> None:
        if self._backoff <= 0:
            self._backoff = BACKOFF_BASE
        self._arm_backoff()

    # -------------------------------------------------------------- connection

    async def _ensure_connected(self) -> bool:
        client = self._client
        if client is not None and client.is_connected and self._write_char is not None:
            self._set_state(LinkState.READY)
            return True
        if self._closing:
            return False

        async with self._connect_lock:
            client = self._client
            if client is not None and client.is_connected:
                self._set_state(LinkState.READY)
                return True
            if self._closing:
                return False

            ble_device = await self._resolve_device()
            if ble_device is None:
                self._set_state(LinkState.SCANNING)
                return False

            self._set_state(LinkState.CONNECTING)
            await close_stale_connections(ble_device)
            try:
                new_client: BleakClientWithServiceCache = await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    self._name,
                    self._on_disconnect,
                    use_services_cache=True,
                    ble_device_callback=lambda: ble_device,
                )
            except (BleakNotFoundError, *BLEAK_RETRY_EXCEPTIONS) as err:
                self._note_error(err)
                self._on_link_failure()
                _LOGGER.debug("%s: connect failed: %s", self._name, err)
                raise LinkError(f"connect failed: {err}") from err

            self._client = new_client
            self.stats.connects += 1
            self._connected_at = time.monotonic()

            if not self._resolve_characteristics(new_client.services):
                _LOGGER.warning(
                    "%s: characteristic %s missing, clearing service cache",
                    self._name,
                    WRITE_UUID,
                )
                await new_client.clear_cache()
                await self._teardown("missing characteristic")
                self._on_link_failure()
                raise LinkError("write characteristic missing")

            self._set_state(LinkState.CONFIGURING)
            self._reader.reset()
            try:
                await new_client.start_notify(self._write_char, self._on_notify)
            except (BleakDBusError, *BLEAK_RETRY_EXCEPTIONS) as err:
                self._note_error(err)
                await new_client.clear_cache()
                await self._teardown(f"start_notify failed: {err}")
                self._on_link_failure()
                raise LinkError(f"subscribe failed: {err}") from err

            self._backoff = 0.0
            self._backoff_until = 0.0
            self._set_state(LinkState.READY)
            _LOGGER.info(
                "%s: link up after %s failed attempt(s)",
                self._name,
                self.stats.reconnect_failures,
            )
            return True

    async def _resolve_device(self) -> Any:
        device = bluetooth.async_ble_device_from_address(
            self._hass, self._address, connectable=True
        )
        if device is None:
            device = await get_device(self._address)
        if device is not None:
            self._ble_device = device
        return self._ble_device

    def _resolve_characteristics(self, services: Any) -> bool:
        for uuid in SERVICE_UUIDS:
            service = services.get_service(uuid)
            if not service:
                continue
            for char in service.characteristics:
                if str(char.uuid).lower() == WRITE_UUID:
                    self._write_char = char
                    return True
        return False

    async def _teardown(self, reason: str) -> None:
        client, self._client = self._client, None
        self._write_char = None
        self._advertised.clear()
        self._reader.reset()
        self._fail_pending(ConnectionLost(reason))
        if client is not None:
            try:
                if client.is_connected:
                    await client.disconnect()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("%s: disconnect in teardown: %s", self._name, err)

    def _fail_pending(self, err: Exception) -> None:
        pending = list(self._queue)
        self._queue.clear()
        self._by_key.clear()
        if self._awaiting is not None:
            pending.append(self._awaiting)
            self._awaiting = None
        for tx in pending:
            if tx.result is not None and not tx.result.done():
                tx.result.set_exception(err)
            if tx.reply_ready is not None and not tx.reply_ready.done():
                tx.reply_ready.set_exception(err)

    # ---------------------------------------------------------------- callbacks

    def _on_disconnect(self, *_args: Any) -> None:
        """Bleak's disconnect callback: start recovery immediately."""
        if self._closing:
            return
        self.stats.drops += 1
        held = time.monotonic() - self._connected_at
        if held < QUICK_DROP_SECONDS:
            self._quick_drops += 1
        else:
            self._quick_drops = 0

        if self._quick_drops >= QUICK_DROP_LIMIT:
            self._set_state(LinkState.EVICTED)
            _LOGGER.warning(
                "%s: dropped after %.1fs on %s connects in a row - another "
                "central such as the phone app is likely holding the controller; "
                "close it to let Home Assistant reconnect",
                self._name,
                held,
                self._quick_drops,
            )
            # Back off hard: retrying eagerly just keeps bouncing the other
            # central off, and the user sees the light flapping.
            self._backoff = BACKOFF_CAP / 4
        else:
            self._set_state(LinkState.DISCONNECTED)

        self._client = None
        self._write_char = None
        self._advertised.clear()
        self._reader.reset()
        self._fail_pending(ConnectionLost("disconnected"))
        self._on_link_failure()
        self._reconnect.set()
        self._work.set()

    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        chunk = bytes(data)
        self._recent_rx.append(chunk)
        self.stats.frames_in += 1
        self.stats.last_rx = time.monotonic()
        for frame in self._reader.feed(chunk):
            self._on_frame(frame)
            self._correlate(frame)

    def _correlate(self, frame: bytearray) -> None:
        tx = self._awaiting
        if tx is None or tx.expect_cmd is None or tx.reply_ready is None:
            return
        if len(frame) < 2 or frame[1] != tx.expect_cmd:
            return
        tx.reply = frame
        self._awaiting = None
        if not tx.reply_ready.done():
            tx.reply_ready.set_result(None)

    # ------------------------------------------------------------------ helpers

    def _supersede(self, key: str, tx: Transaction) -> None:
        """Retire a still-queued command for the same parameter.

        A command already handed to the radio cannot be recalled, so only
        transactions still waiting their turn are replaced.
        """
        old = self._by_key.get(key)
        self._by_key[key] = tx
        if old is None or old is tx:
            return
        if not any(item is old for item in self._queue):
            return
        self._queue.remove(old)
        self.stats.superseded += 1
        self._resolve(old, None)
        if old.reply_ready is not None and not old.reply_ready.done():
            old.reply_ready.cancel()

    def _forget(self, tx: Transaction) -> None:
        try:
            self._queue.remove(tx)
        except ValueError:
            pass
        if tx.coalesce_key is not None and self._by_key.get(tx.coalesce_key) is tx:
            del self._by_key[tx.coalesce_key]
        if self._awaiting is tx:
            self._awaiting = None

    def _note_error(self, err: BaseException) -> None:
        self.stats.last_error = f"{type(err).__name__}: {err}"

    def _set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        self._on_state()
