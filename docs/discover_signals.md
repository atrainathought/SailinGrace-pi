# Signal discovery walkthrough — wiring the Pi to a live source

`scripts/discover_signals.py` turns the Pi into a headless signal-discovery
box: it probes every input the Pi can receive on, works out which one is
actually carrying data, and (optionally) wires `signalk-server` to it —
no admin UI, no prompts.

## Scope of this walkthrough

This Pi is used **only for wired instrument data**:

| Channel | Source | Filter name | Report ID |
|---|---|---|---|
| NMEA 2000 | PiCAN-M HAT on the boat's N2K backbone | `can` | `can0` |
| NMEA 0183 | USB-RS422/RS232 adapter on a 0183 bus | `serial` | `/dev/ttyUSB0@4800` |

**If the instrument data is only on WiFi** (an MFD broadcast, Expedition's
NMEA output, another SignalK server), the plan is to put a **WiFi dongle on
the laptop and read it there directly — the Pi is not used at all.** So the
`udp` / `tcp` / `signalk` channels the script can also probe are out of
scope here, and every command below limits probing with
`--channels can,serial` to skip them.

> The Pi earns its place for the wired feeds: it taps the N2K backbone (no
> password) or a bare 0183 bus, normalises everything to SignalK, logs it,
> and relays a stable stream. For WiFi-only data, a laptop dongle is simpler.

## Before you start

Run everything on the Pi (over SSH or USB-C console). The repo lives at
`/home/pi/SailinGrace-pi` and the Python deps are in its venv. The CAN
channel needs `sudo` (it brings `can0` up via SocketCAN).

```bash
cd /home/pi/SailinGrace-pi
```

For an N2K tap you need: the PiCAN-M connected to a **powered** backbone
(boat instruments on), with exactly two 120 Ω terminators on the bus and
the PiCAN-M's own termination jumper **OFF**. Nothing reaches `can0`
without bus power.

## Step 1 — Scan: what's live right now?

Probe the wired channels and print a ranked report. Default listen time is
15 s per channel:

```bash
sudo .venv/bin/python scripts/discover_signals.py scan --channels can,serial
```

Read the report:

- **`live: yes`** with a non-zero `rate_hz` on `can0` → the N2K backbone is
  feeding the Pi. The `detail` column lists the PGNs seen (e.g. `129025`
  position, `130306` wind).
- **`live: yes`** on `/dev/ttyUSB0@4800` → a 0183 bus is wired in; `detail`
  lists talker IDs + sentence types (e.g. `GPRMC`, `IIVHW`, `WIMWV`).
- **`live: no`** everywhere → nothing's reaching the Pi yet. See
  [Troubleshooting](#troubleshooting-a-silent-can0).

The top-ranked live source is what `apply --auto` will pick. Ranking
favours richer feeds and, in a tie, sources carrying wind — what routing
needs most.

Listen longer on a slow/intermittent bus, and add `--json` if you want to
pipe the result somewhere:

```bash
sudo .venv/bin/python scripts/discover_signals.py scan --channels can,serial --duration 60
```

## Step 2 — Capture (optional): record raw data to verify offline

Before trusting a new source, record its raw samples + a classification
summary so you can confirm the parse format offline. This is the
"use it first" workflow — useful for an unfamiliar boat or a flaky bus:

```bash
.venv/bin/python scripts/discover_signals.py capture \
  --channels can,serial --duration 120 --out-dir data/discovery
```

(Add `sudo` if you're including the `can` channel.) It writes, under
`data/discovery/`:

- `<timestamp>_<channel>.log` — the raw samples
- `<timestamp>_summary.json` — sentences/PGNs/paths + per-channel rates

Inspect `_summary.json` to confirm the sentences/PGNs are what you expect
before wiring the relay to that source.

## Step 3 — Apply: wire signalk-server to the live source

`apply` is **safe to run headless**. It:

1. backs up `~/.signalk/settings.json`,
2. writes the connection for the chosen source,
3. **disables any other connections** so two sources can't double-feed the
   downstream estimators,
4. restarts `signalk-server`,
5. polls the SignalK API to confirm data is actually flowing, and
6. **rolls the config back** if nothing arrives.

Let it pick the best live source automatically:

```bash
sudo .venv/bin/python scripts/discover_signals.py apply --auto --channels can,serial
```

Or pin a specific source by its **report ID** from Step 1:

```bash
sudo .venv/bin/python scripts/discover_signals.py apply --source can0
sudo .venv/bin/python scripts/discover_signals.py apply --source /dev/ttyUSB0@4800
```

On success it prints the `ws://…:3000` stream URL on each of the Pi's
interfaces — that's what you point the laptop's SailinGrace at.

## Step 4 — Verify

```bash
# SignalK is serving data (not "Unauthorized", not empty):
curl -s http://localhost:3000/signalk/v1/api/vessels/self | head

# The Pi-side logger is capturing the new deltas:
journalctl -u sailingrace-logger -n 20 --no-pager
tail -n 5 /home/pi/sailingrace-logs/signalk_$(date -u +%F).log
```

The logger's capture-stats `deltas/s` should now be non-zero and `paths:`
should list real navigation/environment paths instead of just `defaults`.

On the laptop, point SailinGrace at the printed URL, e.g.:

```
ws://trainapi4.local:3000/signalk/v1/stream?subscribe=all
```

`/api/sensors/capabilities` should report `available: true,
source: "signalk"`.

## Troubleshooting a silent can0

`can0` coming up clean but receiving nothing is the most common N2K
bring-up failure. In order of likelihood:

- **No bus power** — the backbone must be powered and instruments on.
  `candump can0` should show frames; silence means no traffic reaching the
  HAT.
- **Wrong oscillator** — PiCAN-M is **16 MHz**; some clones are 8 MHz. A
  wrong value in `/boot/firmware/config.txt`
  (`dtoverlay=mcp2515-can0,oscillator=…`) yields a clean-but-deaf `can0`.
  Check the silkscreen on the board.
- **Termination** — the backbone needs exactly two 120 Ω terminators; the
  PiCAN-M's on-board jumper should be **OFF** for normal backbone use.
- **T-connector orientation / drop cable** — reseat the DeviceNet T and the
  M12 drop.

Quick checks:

```bash
ip -details link show can0    # expect: UP, type can, bitrate 250000
candump can0                  # expect frames in the 0x09FF… range
```

For a silent serial channel, confirm the adapter enumerated
(`ls /dev/ttyUSB* /dev/ttyACM*`) and that the bus baud matches what the
scan tried (4800 and 38400).
