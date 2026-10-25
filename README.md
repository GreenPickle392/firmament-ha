# Firmament

A Home Assistant integration for **SP642E / SP632E CCT LED controllers** — the
2-channel PWM white strips driven by the BanlanX / Zengge firmware over
Bluetooth Low Energy.

It exists because the usual way of driving these controllers from Home
Assistant spends more time disconnected than connected. Firmament's job is the
link first, the light second.

```
# a controller you name "Desk light"
light.desk_light                     on/off · brightness · kelvin · 5 effects
number.desk_light_effect_speed
sensor.desk_light_link_state         <- what the connection is doing
sensor.desk_light_signal_strength
sensor.desk_light_connection_drops
```

## Why it stays connected

| What breaks a BLE controller integration | What Firmament does instead |
| --- | --- |
| Disconnects after a few seconds idle, then pays a full reconnect on the next call | Holds one persistent connection; nothing is thrown away to save radio power |
| A poll interval longer than the idle disconnect, so the device is always gone when it is asked | State arrives by GATT notification; a slow reconcile timer (default 5 min) only corrects drift from the phone app |
| Reconnect only attempted inside a log-only disconnect callback | A supervisor task owns recovery and starts it the moment the link drops |
| Flat, immediate retries that hammer an adapter that is not ready | Exponential backoff with jitter, capped at 60 s, and an attempt is only made while the controller is actually being advertised |
| Concurrent writes interleaved on one characteristic | One writer task, one transaction queue, strictly one in-flight frame |
| A slider drag queues dozens of commands | Commands coalesce: only the newest value per parameter survives, superseded ones resolve without touching the radio |
| 53-byte status replies parsed per 23-byte notification chunk, so a split frame looks like corruption | Frames are buffered and reassembled before parsing, with resync on a lost header |
| Silent `except` blocks swallow the real error | Exceptions carry text that reaches the log and the `link_state` sensor |

When the phone app is holding the controller — the most common cause of
"loses connection all the time" — Firmament names it: BLE peripherals serve one
central at a time, so repeated connect-then-instant-drop is reported as
`evicted` rather than an endless retry loop. Close the Zengge/BanlanX app and
the link comes back on its own.

## Requirements

- Home Assistant 2024.8 or newer
- A Bluetooth adapter usable by the `bluetooth` / `bluetooth_adapters`
  integrations, on the same machine as Home Assistant (ESP32 remote proxies
  work but are slower to reconnect)
- The controller not connected to the phone app

## Install with HACS

1. HACS → Integrations → ⋮ → **Custom repositories**
2. Repository: `https://github.com/GreenPickle392/firmament-ha`, category: **Integration**
3. Find **Firmament (SP642E CCT)** → Install → restart Home Assistant

## Add the controller

Settings → Devices & Services → Add Integration → **Firmament**. It is
discovered automatically from Bluetooth advertisements (manufacturer id 20563,
model bytes `0x2D`/`0x21`). If it does not appear, add it by MAC address
instead — the address printed on the controller's label or shown by a BLE
scanner.

After it is set up, open **Configure** to set the warmest and coolest kelvin
your strip actually reaches. The controller does not know what it is wired to.

## What you get

- **Light** with `color_temp`, brightness, and effects: `Solid`, `Breath`,
  `Strobe`, `Heartbeat`, `Music` (mic-reactive mode, if the unit has a microphone)
- **Effect speed** as a number entity
- **Link state / RSSI / connection drops** diagnostic sensors, so an outage is
  a diagnosis rather than a rumour
- **Optimistic writes** plus state restore: the light looks right immediately,
  and still looks right after a Home Assistant restart before the first reply
- **Diagnostics download** on the config entry (MAC redacted to its first octet)

## Troubleshooting

| `link_state` | Meaning |
| --- | --- |
| `ready` | Connected; commands and notifications flowing |
| `scanning` | The controller is not being advertised — powered off, out of range, or held by another central |
| `connecting` / `configuring` | In progress, including MTU negotiation and subscribe |
| `backoff` | Attempts are being paced after failures; the log says why |
| `evicted` | Something else owns the controller. Close the phone app |

If the log reports an **encrypted session**, the controller was paired by an
app that negotiated a session key. This integration speaks the plaintext
BanlanX protocol only; factory-reset the controller.

## Limits, honestly

- BLE only. The network (ESP8266/WiFi) mode of these controllers is not
  implemented.
- Only the CCT light type (`CFG_83`) is wired up. RGB, RGBW and addressable
  IC modes share the transport but need their own command builders.
- Loop, direction and play/pause commands exist in `protocol.py` but have no
  entities yet.
- Colour temperature is interpolated in mired space between your configured
  limits; two PWM channels cannot trace a real blackbody curve exactly.
- 20 offline tests cover framing, reassembly, correlation, coalescing,
  reconnection and command composition. **No test has been run against a real
  radio** — first contact with hardware may surface protocol surprises.

## Development

The protocol and transport layers import no Home Assistant module on purpose,
so the whole suite runs on a bare Python 3.11+:

```
python tests/test_protocol.py
python tests/test_transport.py
python tests/test_device.py
```

## License

MIT — see [LICENSE](LICENSE).
