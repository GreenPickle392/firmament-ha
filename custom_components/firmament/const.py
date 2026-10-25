"""Constants for the Firmament SP642E CCT controller integration."""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "firmament"
MANUFACTURER: Final = "SPLED (BanlanX)"

CONF_MODEL_ID: Final = "model_id"
CONF_MANUFACTURER: Final = "manufacturer"
CONF_MIN_KELVIN: Final = "min_kelvin"
CONF_MAX_KELVIN: Final = "max_kelvin"
CONF_RECONCILE_INTERVAL: Final = "reconcile_interval"

DEFAULT_MIN_KELVIN: Final = 2000
DEFAULT_MAX_KELVIN: Final = 6500

# Reconnect budget. Attempts are only made while the controller is known to be
# advertising, so backoff can be generous without delaying a real recovery.
BACKOFF_BASE: Final = 1.0
BACKOFF_CAP: Final = 60.0
BACKOFF_JITTER: Final = 0.25

# Status replies arrive as several GATT notifications; a reply slower than this
# means the frame was lost rather than fragmented.
NOTIFY_TIMEOUT: Final = 5.0
WRITE_TIMEOUT: Final = 10.0
# Gap between frames of one multi-command transaction.
COMMAND_SETTLE: Final = 0.05

# Status is pushed by the controller, so the polling interval only reconciles
# drift caused by changes made in the phone app.
RECONCILE_INTERVAL: Final = 300.0
# How long entry setup waits for the first status before letting the entities
# come up and leaving recovery to the link supervisor.
STARTUP_STATUS_BUDGET: Final = 12.0
# Reuse a status frame that is fresher than this instead of hitting the radio.
STATUS_CACHE_WINDOW: Final = 3.0

RSSI_BAD: Final = -127
