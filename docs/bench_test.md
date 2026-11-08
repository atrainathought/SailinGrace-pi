# Bench test — NMEA 2000 backbone, Pi, ESP32, laptop

Tracked as **N2K-1** in the [SailinGrace backlog](https://github.com/atrainathought/SailinGrace/blob/master/docs/backlog.md#n2k-1-bench-test-the-nmea-2000-path).

How to prove the Pi + PiCAN-M relay works against a **real NMEA 2000 bus**
before it goes on the boat. The Pi has been bench-validated end-to-end but
never against a live backbone ([install_rpi.md § Tested-on](install_rpi.md#tested-on--known-working));
this procedure closes that gap.

Parts list: **[bench_materials.md](bench_materials.md)**.

> **No laptop N2K adapter yet?** Use **[usb_bench_test.md](usb_bench_test.md)**:
> the ESP32 and the Pi each plug into the dev computer over USB and talk to each
> other over a two-node CAN link. Every step below
has a **Linux** and a **Windows** variant for the laptop.

## Roles

Three nodes, three different jobs, so one device's bug can't hide behind
another's:

```
 ESP32  ── sensor GENERATOR ────┐
 (TWAI + transceiver)           │
                                ├──── N2K backbone (12 V, 2× 120 Ω) ────┐
 Laptop ── independent SNIFFER ─┘                                        │
 (CANable on Linux /                                              Pi + PiCAN-M
  YDWG-02 on Windows)                                          DEVICE UNDER TEST
     ▲                                                     canboat-js → signalk-server
     │                                                          │  logger, discovery
     └──────────── WiFi / ethernet: SignalK ws://<pi>.local:3000 ┘
                   SailinGrace (laptop) consumes it
```

- **ESP32** sends known, scripted sensor values (§ ESP32 generator).
- **Laptop** reads the copper bus directly — this is the *truth* — and
  also runs SailinGrace against the Pi's SignalK output.
- **Pi** is what's being tested: it must turn the bus traffic into correct
  SignalK, log it, and survive faults.

A test passes when **ESP32 script = laptop bus decode = Pi SignalK =
SailinGrace** for every value.

## Test PGNs and expected SignalK paths

The generator sends the set SailinGrace's routing and fusion actually use:

| PGN | Meaning | Rate | Frame type | SignalK path (SI units) |
|---|---|---|---|---|
| 130306 | Wind (apparent) | 10 Hz | single | `environment.wind.speedApparent` (m/s), `environment.wind.angleApparent` (rad, −π..π) |
| 127250 | Vessel heading (magnetic) | 10 Hz | single | `navigation.headingMagnetic` (rad, 0..2π) |
| 129026 | COG & SOG, rapid | 10 Hz | single | `navigation.courseOverGroundTrue` (rad), `navigation.speedOverGround` (m/s) |
| 129025 | Position, rapid | 10 Hz | single | `navigation.position` (deg lat/lon) |
| 128259 | Speed through water | 1 Hz | single | `navigation.speedThroughWater` (m/s) |
| 128267 | Water depth | 1 Hz | single | `environment.depth.belowTransducer` (m) |
| 060928 | ISO address claim | on start | single | — (bus housekeeping) |
| 126996 | Product info | on request | **fast-packet** | — (exercises multi-frame) |

Scripted profile (120 s cycle, repeats):

| t (s) | AWS | AWA | Heading (mag) | Expect in SignalK |
|---|---|---|---|---|
| 0–40 | 10 kn | +30° (stbd) | 350° → 10° ramp, 1° per 6 s | speedApparent 5.144, angleApparent +0.5236 |
| 40–60 | 10 kn | +45° | ″ | angleApparent +0.7854 |
| 60–80 | 20 kn | +45° | crosses 0° at t = 60 | speedApparent 10.289; heading wraps 6.28 → 0.00 |
| 80–120 | 20 kn | −30° (port, sent as 330°) | ″ | angleApparent **−0.5236** (not +5.76) |

Constant: BSP 6.5 kn (3.344 m/s), SOG 7.0 kn (3.601 m/s), COG = heading,
depth 25.0 m, position starts 41.4500 N 71.3200 W and advances along COG.

The deliberate traps: **knots vs m/s**, **degrees vs radians**, the
**0°/360° heading wrap**, and the **port AWA sign** (N2K sends 0..2π,
SignalK wants −π..π).

## ESP32 generator

The firmware lives in **[`SailinGrace-esp32/firmware-n2ksim/`](https://github.com/atrainathought/SailinGrace-esp32/tree/main/firmware-n2ksim)**
— a separate PlatformIO project, so the WiFi race logger in `firmware/`
stays untouched. Same Feather HUZZAH32, different firmware. Wiring is in
[bench_materials.md § ESP32 node](bench_materials.md#3-esp32-node-sensor-generator)
and the project README.

```bash
cd SailinGrace-esp32/firmware-n2ksim
pio run -t upload && pio device monitor      # 115200 baud
```

It sends exactly the profile above. The serial monitor prints one line per
second — that is the **script truth** the other nodes are compared with:

```
t= 42.0 src=42 aws=10kn awa=45 hdg=357.0 bsp=6.5 sog=7.0 depth=25.0 lat=41.45010 lon=-71.32000
```

Build-verified (`espressif32@6.9.0`, `NMEA2000-library@4.25.1`,
`NMEA2000_esp32@1.0.3`); first hardware run is Level 3 below.

> The ESP32 needs **another node on the bus** to ACK its frames. Alone, it
> goes error-passive / bus-off and prints `WARN: SendMsg failed` — that's
> CAN, not a bug. Test it with the laptop or Pi attached.

## Laptop setup

### Linux (CANable 2.0, candleLight firmware)

```bash
sudo apt install -y can-utils
# canboat (analyzer, candump2analyzer):
git clone https://github.com/canboat/canboat && cd canboat && make && sudo make install
# Bring the adapter up (it enumerates as can0 via gs_usb):
sudo ip link set can0 up type can bitrate 250000
ip -details link show can0        # expect: UP, bitrate 250000
```

Clone this repo on the laptop too — `scripts/discover_signals.py` is
stdlib-only and gives the **same PGN/rate table the Pi produces**, so the
two can be diffed directly.

### Windows (YDWG-02 WiFi gateway)

1. Plug the YDWG-02 into a tee; it's powered by the bus.
2. Join its WiFi (or put it on your bench network), open its web UI,
   set one server to **RAW** protocol (note the TCP port).
3. GUI decode: Yacht Devices **CAN Log Viewer** pointed at the gateway.
4. Scripted decode, from WSL2 (it's just TCP, no kernel CAN needed):
   ```bash
   sudo apt install -y netcat-openbsd nodejs npm && sudo npm i -g @canboat/canboatjs
   nc <ydwg-ip> <raw-port> | analyzerjs        # one JSON line per PGN
   ```
5. SignalK / SailinGrace checks below use plain HTTP — `curl.exe` in
   PowerShell works the same as `curl` on Linux.

(USB alternative on Windows: Actisense NGT-1 + the free *NMEA Reader*.)

---

## The test ladder

Run in order. Each level's **Pass** must hold before moving on. Record
results in the sheet at the bottom.

### Level 0 — Software only, no hardware

Proves the Pi's parsing path before any wiring. Run on the **Pi** (or a
Linux laptop). Windows: skip — run this one on the Pi.

```bash
sudo modprobe vcan
sudo ip link add vcan0 type vcan && sudo ip link set vcan0 up
# Fake an apparent-wind frame (PGN 130306, src 0x42): 10 kn @ 30°
cansend vcan0 09FD0242#0102027414FAFFFF
candump vcan0 | candump2analyzer | analyzer        # decodes as Wind Data
```

Optionally add a temporary signalk-server connection on `vcan0` (NMEA 2000
/ canboat-js) and confirm the wind path appears. Remove it afterwards.

**Pass:** analyzer decodes PGN 130306 with wind speed ≈ 5.14 m/s, angle ≈ 30°.

### Level 1 — Physical bus

**Power OFF:** measure across CAN_H (white) / CAN_L (blue) at any spare tee.

| Reading | Means |
|---|---|
| ~60 Ω | ✅ two terminators |
| ~120 Ω | one terminator missing |
| ~40 Ω | three terminators — a device's jumper is ON (PiCAN-M, CANable, transceiver board) |
| ~0 Ω / open | short / broken cable |

**Power ON (12 V):** NET-S–NET-C ≈ 12 V; CAN_H and CAN_L each ≈ 2.5 V to
NET-C at idle.

**Pass:** 60 Ω, 12 V, ~2.5 V/2.5 V.

### Level 2 — Each node on the bus

**Pi:**
```bash
ip -details -statistics link show can0   # UP, bitrate 250000, state ERROR-ACTIVE
candump can0                             # silent is OK with no other node
```
If can0 is UP but never receives anything later: check the PiCAN-M
oscillator (16 MHz vs 8 MHz clone) first.

**Laptop:** Linux — same `ip link` check on the CANable. Windows — YDWG-02
web UI shows it's connected to the bus.

**Pass:** both interfaces up, error counters 0.

### Level 3 — Two nodes: ESP32 → laptop

Pi **off the bus** (unplug its drop). ESP32 + laptop only.

Linux:
```bash
candump -ta can0                                   # raw frames flowing
candump can0 | candump2analyzer | analyzer         # decoded
sudo python3 scripts/discover_signals.py scan --channels can --duration 10 --json
```
Windows: CAN Log Viewer, or `nc … | analyzerjs` in WSL2.

**Pass:**
- Address claim (60928) from the ESP32 seen at start.
- All six data PGNs present at ≈ 10 Hz / 1 Hz (±10 %).
- Decoded values match the ESP32 serial log and the profile table.
- Request product info (Linux: canboat's `analyzer` shows the 126996 reply
  after any node requests it; signalk-server does this automatically in
  Level 4) — fast-packet reassembles cleanly.

### Level 4 — Three nodes, end to end

Plug the Pi back in. On the Pi, `can0` should already be wired to
signalk-server ([install_rpi.md § 3](install_rpi.md#3-wire-can0-as-a-signalk-source),
or `sudo python3 scripts/discover_signals.py apply --source can0`).

1. **Discovery** (Pi): `sudo python3 scripts/discover_signals.py scan --channels can --duration 10 --json`
   → same PGN set and rates as the laptop's Level 3 output.
2. **SignalK** (from the laptop, either OS):
   ```bash
   curl http://<pi>.local:3000/signalk/v1/api/vessels/self/environment/wind/angleApparent
   curl http://<pi>.local:3000/signalk/v1/api/vessels/self/navigation/headingMagnetic
   curl http://<pi>.local:3000/signalk/v1/api/vessels/self/navigation/position
   ```
   Admin UI → Dashboard: Deltas/sec in the tens (≈ 4×10 Hz + 2×1 Hz of source messages) and steady.
3. **Logger** (Pi): the SignalK delta log under the logger's data dir is growing.
4. **SailinGrace** (laptop), with Settings → System set to combination C / SignalK, and the Pi picked from the discovered servers in Settings → Network (or `ws://<pi>.local:3000/signalk/v1/stream?subscribe=self` typed in). The pre-race check should be green for instruments:
   `curl http://localhost:8000/api/sensors/capabilities` → `available: true,
   source: "signalk"`; wind / heading / position features lit on the dashboard.

**Pass:** all four.

### Level 5 — Value correctness (the one that finds bugs)

Watch one full 120 s cycle and check every row of the profile table at
**four places**: ESP32 serial log, laptop bus decode, Pi SignalK, SailinGrace UI.

- [ ] AWS 10 → 20 kn shows as 5.144 → 10.289 m/s in SignalK, 10 → 20 kn in the UI
- [ ] AWA +30 / +45 / **−30** — port side negative in SignalK, port in the UI
- [ ] Heading crosses 359° → 0° with no spike / 180° glitch in the UI or the UKF
- [ ] BSP 6.5 kn, SOG 7.0 kn, depth 25.0 m
- [ ] Position advancing along COG, ~7 kn (≈ 3.6 m/s), no jumps
- [ ] Timestamps monotonic; SignalK update rate ≈ bus rate (no silent decimation)

**Pass:** every box, everywhere.

### Level 6 — Robustness

| Test | How | Pass |
|---|---|---|
| Hot-unplug ESP32 | Pull its drop for 30 s, replug | Pi keeps running; data resumes within ~5 s; SailinGrace freshness indicator goes stale then recovers |
| Hot-unplug Pi | Pull its drop for 30 s, replug | `can0` recovers on its own; SignalK resumes. `setup_pi.sh` sets `RestartSec=100ms` in `80-can.network` for bus-off recovery — confirm with `ip -details link show can0` (`restart-ms 100`) and that `-statistics` shows a `bus-off` count with the link back in ERROR-ACTIVE |
| Short CAN_H–CAN_L for 2 s | Jumper at a spare tee (bench only) | Same as above — this forces bus-off deliberately |
| Bus power cut | Kill 12 V for 10 s | Pi (on its own power) logs the gap, recovers when bus returns |
| Pi power cut | Pull Pi power / let UPS take over | UPS behaves as configured; log on SD intact, no corruption |
| Bus load | Linux laptop: `cangen can0 -g 2 -e -L 8` alongside the sim | Pi doesn't drop the sim's PGNs (compare discovery rates) |
| Low voltage | Bench PSU to 10 V, then 9 V | All nodes still communicate |
| Soak | 24 h, everything on | Log size respects `MAX_LOG_MB`; no memory growth in signalk-server; rates unchanged |

### Level 7 — Repeatable regression (record → replay)

Once Level 5 passes, capture a golden recording so future Pi changes can be
re-tested without the ESP32:

```bash
# Record (Linux laptop, or on the Pi itself):
candump -l can0                     # → candump-YYYY-MM-DD_HHMMSS.log; ~5 min
# Replay — ESP32 UNPLUGGED (same source address would collide):
canplayer -I candump-*.log -l i can0     # -l i = loop forever
```

Windows laptop: do the record/replay from the **Pi** (it has can-utils);
the YDWG-02 stays as the independent sniffer.

Keep the golden log in `data/bench/` and re-run Levels 4–5 against it after
any change to `setup_pi.sh`, signalk-server, or the logger.

### Level 8 — On the boat

Repeat **Level 1** and **Level 4** on Lynx's backbone with the boat's real
instruments. **Leave the ESP32 generator off the boat bus** — fake heading
and wind on a live autopilot network is dangerous. Then mark the
Tested-on line in [install_rpi.md](install_rpi.md#tested-on--known-working).

---

## Results sheet

| Date | Laptop OS / adapter | Level | Pass/Fail | Notes |
|---|---|---|---|---|
| | | 0 software | | |
| | | 1 physical | | Ω: ___  V: ___ |
| | | 2 nodes | | |
| | | 3 ESP32→laptop | | PGN rates: |
| | | 4 end-to-end | | Deltas/sec: |
| | | 5 values | | |
| | | 6 robustness | | |
| | | 7 replay | | golden log: |
| | | 8 boat | | |
