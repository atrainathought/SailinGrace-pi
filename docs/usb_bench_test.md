# USB bench test — two Pis, then the ESP32

Tracked as **N2K-1** in the [SailinGrace backlog](https://github.com/atrainathought/SailinGrace/blob/master/docs/backlog.md#n2k-1-bench-test-the-nmea-2000-path).

A bench test of the Pi's NMEA 2000 path that needs **no laptop N2K
adapter**. It runs in two phases:

- **Phase 1, two Pis (P1–P8).** The **reader Pi** (Pi 4B + Waveshare
  RS485 CAN HAT) runs `scripts/n2k_sim.py` and sends scripted sensor
  data to the **boat Pi** (Pi 4B + PiCAN-M), which is the device under
  test. Nothing to buy except one M12 cable.
- **Phase 2, add the ESP32 (E1–E5).** The ESP32 simulator
  (`SailinGrace-esp32/firmware-n2ksim`) takes over sending, and the
  reader Pi goes back to only watching. You get an independent bus reader,
  and a real embedded N2K device (address claim, product info) on the
  bus.

The laptop is connected to **two devices at a time**.

```
 Phase 1                                        Phase 2
 ┌────────── laptop ──────────┐                 ┌────────── laptop ──────────┐
 │ ssh / browser / SailinGrace│                 │ serial monitor  ssh/browser│
 └──┬──────────────────────┬──┘                 └──┬───────────────────────┬─┘
    │WiFi/eth (or USB-C    │USB-C                  │USB              USB-C │      reader Pi: WiFi/eth
    │ on 10.56.0.1)        │10.55.0.1              │                10.55.0.1     (own supply)
 ┌──┴─────────┐  CAN  ┌────┴───────┐        ┌──────┴──┐  ┌──────────┐  ┌──┴─────────┐
 │ reader Pi  ├───────┤ boat Pi    │        │ ESP32   ├──┤ reader Pi├──┤ boat Pi    │
 │ Waveshare  │       │ PiCAN-M    │        │ sim     │  │ watches  │  │ PiCAN-M    │
 │ n2k_sim.py │       │ → SignalK  │        └─────────┘  └──────────┘  │ → SignalK  │
 └────────────┘       └────────────┘          SENDS        READER      └────────────┘
   SENDS                UNDER TEST                                       UNDER TEST
```

**Where to read each layer:**

| Layer | Phase 1 | Phase 2 | What it proves |
|---|---|---|---|
| Script | `n2k_sim.py` output on the reader | ESP32 serial monitor | What was *meant* to be sent |
| Bus | `candump can0` on the boat Pi (its kernel, below canboat-js / SignalK) | `candump can0` on the **reader**, a separate device | What actually crossed the wire |
| Relay | SignalK at `10.55.0.1:3000` | same | What the boat Pi decoded |
| App | SailinGrace on the laptop | same | What routing / fusion sees |

In phase 1 the bus record comes from the boat Pi's own kernel CAN layer.
That's acceptable, because it sits below the canboat-js → SignalK decode
path being tested. Phase 2 removes that compromise.

---

## Before you start

### What you need

| | Phase 1 | Phase 2 adds |
|---|---|---|
| Boat Pi | Pi 4B + PiCAN-M, `setup_pi.sh` run, `can0` added as a signalk-server source ([install_rpi.md § 3](install_rpi.md#3-wire-can0-as-a-signalk-source)) | — |
| Reader Pi | Pi 4B + Waveshare RS485 CAN HAT, set up as below, **its own 5 V / 3 A supply** | — |
| ESP32 | — | Feather HUZZAH32 + CAN transceiver, flashed with `firmware-n2ksim` ([wiring](https://github.com/atrainathought/SailinGrace-esp32/tree/main/firmware-n2ksim#wiring-feather-huzzah32)), a USB **data** cable |
| CAN cable | One **male** M12 cable with bare wires (a drop cable cut in half, or a field-attachable male connector), into the PiCAN-M | 3 jumper wires, ESP32 → the reader HAT's terminal |
| Laptop | USB-C cable to the boat Pi (a **USB-C port**; USB-A is marginal for a Pi 4 + HAT); WiFi or ethernet to the reader | USB cable to the ESP32 |
| Tools | Multimeter | — |

Until SailinGrace-pi PR #2 is merged, check out branch
`ccr-2e575357-7ob7cj` on **both** Pis (`git fetch && git checkout
ccr-2e575357-7ob7cj`). That branch has `n2k_sim.py`, the Waveshare
support and the bus-off fix.

### Set up the reader Pi

1. Give it its own hostname, e.g. `sg-reader` (via `raspi-config`), so
   `.local` names don't clash with the boat Pi.
2. Check the crystal on the HAT. Yours is marked **12.000** (12 MHz).
3. Run the setup with the matching HAT setting, then reboot:
   ```bash
   CAN_HAT=waveshare bash scripts/setup_pi.sh          # 12 MHz
   # an old 8 MHz board:  CAN_HAT=waveshare CAN_OSCILLATOR=8000000 bash scripts/setup_pi.sh
   sudo reboot
   ```
   This installs everything the boat Pi gets. The reader only needs `can0`
   and can-utils, so **don't** add `can0` as a signalk-server source on it.
4. Check the HAT came up:
   ```bash
   dmesg | grep -i mcp251        # "MCP2515 successfully initialized"
   ip -details link show can0    # UP, bitrate 250000, restart-ms 100
   ```

### The laptop's two connections

| Phase | Connection 1 | Connection 2 | Not on the laptop |
|---|---|---|---|
| 1 | Boat Pi, **USB-C** → `10.55.0.1` (power + network) | Reader Pi over **WiFi / ethernet** → `sg-reader.local` | — |
| 2 | Boat Pi, USB-C → `10.55.0.1` | ESP32, **USB** (flash + serial monitor) | Reader Pi stays on WiFi / ethernet (that doesn't use a port) |

- **Give the reader its own power supply.** Two Pi 4s on a laptop's USB-C
  ports is more power than most laptops supply. Both USB-C links also
  default to `10.55.0.1`, so the laptop couldn't tell them apart.
- **If you must put the reader on USB-C too,** move it to another subnet
  first: `sudo nmcli connection modify sg-usb ipv4.addresses 10.56.0.1/24 && sudo nmcli connection up sg-usb`.
  Then reach it at `10.56.0.1`; its config page will ask for the setup token.
- **If the laptop can only take two connections in phase 2,** flash the
  ESP32 and check its serial output once (E1), then power it from a USB
  charger. The reader's `candump` is then the reference.

### Wiring and termination

**12 V bus power stays OFF for this whole test.** CAN signalling doesn't
need NET-S, because each transceiver is powered by its own board. A
powered backbone could also feed the boat Pi through the PiCAN-M's
regulator while the laptop's USB-C powers it too. Two supplies on the Pi's
5 V rail is asking for trouble.

The Waveshare HAT's green terminal has **H** and **L** (CAN) and **B** /
**A** (RS485, unused). It has **no ground terminal**, so take ground from a
reader-Pi header GND pin (pin 6). The 4-wire lead with a small white plug
that comes with some of these HATs is **not** an N2K cable. In N2K wiring,
**white = CAN_H, blue = CAN_L, black = ground**.

A CAN bus is terminated at its **two physical ends only**:

| Link | Wiring | Termination |
|---|---|---|
| **Phase 1, direct** | M12 male cable into the PiCAN-M: white → **H**, blue → **L**, black → reader GND pin. Red (12 V) and bare shield unconnected. | HAT **120R switch, CAN = ON** · PiCAN-M jumper **ON** |
| **Phase 2, direct** | As phase 1, plus ESP32 transceiver CANH → **H**, CANL → **L**, GND → reader GND pin, at the same terminal | The reader is now in the **middle**: HAT switch **OFF** · ESP32 120 Ω **ON** · PiCAN-M jumper **ON** |
| **Backbone (either phase)** | Each node on its own tee with a male M12 drop | All node termination **OFF**; the backbone's two terminators do it |

Backbone tees have **female** sockets, so anything plugged into one needs a
**male** M12.

### Laptop software

| | Linux | Windows |
|---|---|---|
| Boat Pi USB-ethernet | Appears automatically as `usb0`/`enx…`, with a DHCP lease in 10.55.0.x | Appears as **"RNDIS/Ethernet Gadget"**. If Device Manager lists it under *Other devices* with no driver: *Update driver → Browse → Let me pick → Network adapters → Microsoft → Remote NDIS Compatible Device*. |
| ESP32 serial (phase 2) | CP2104 is in the kernel → `/dev/ttyUSB0`. Add yourself to `dialout` (`sudo usermod -aG dialout $USER`, then log out and back in). | Silicon Labs **CP210x VCP driver** (usually installed by Windows Update) → `COMx` |
| PlatformIO (phase 2) | `pipx install platformio` or the VS Code extension | VS Code + PlatformIO extension |
| Shell tools | `curl`, `ssh` | PowerShell has `curl.exe` and `ssh` built in |
| SailinGrace | Native or Docker, per its README | Docker / WSL2, per its README |

---

## Phase 1 — two Pis

Run in order and don't skip ahead. Record each result in the results sheet
at the bottom. Keep two SSH windows open, one per Pi.

### P1 — The laptop reaches both Pis

```bash
ssh pi@10.55.0.1            # boat Pi over USB-C (use your username)
ssh pi@sg-reader.local      # reader Pi over WiFi / ethernet
```

On **each** Pi:

```bash
vcgencmd get_throttled                  # throttled=0x0 (no under-voltage)
ip -details -statistics link show can0  # UP, bitrate 250000, restart-ms 100
```

On the boat Pi also: `systemctl is-active signalk-server sailingrace-logger`.
In a browser, check `http://10.55.0.1:3000` (signalk-server admin) and
`http://10.55.0.1:8080` (config page).

**Pass:**
- Both SSH sessions work
- Both Pis show `0x0`
- Both `can0` are UP at 250000 with `restart-ms 100`
- The boat Pi's services are active

If you see `restart-ms 0`, the bus-off fix isn't applied: re-run
`setup_pi.sh` from the branch.

If the boat Pi isn't `0x0`, the laptop port can't power it. Use a powered
USB-C hub or a separate supply, and keep the laptop cable for data.

### P2 — Physical link (both Pis powered off)

Wire the phase 1 direct link. Measure across H/L at the HAT terminal:

| Reading | Means |
|---|---|
| **~60 Ω** | ✅ two terminators |
| ~120 Ω | one end isn't terminated (the HAT's 120R switch or the PiCAN-M jumper) |
| ~40 Ω | three terminators (on a backbone, a node's termination is ON) |
| 0 Ω / open | swapped or broken wire |

Power both Pis. Measure H and L to GND: both should be **≈ 2.5 V** when
idle.

**Pass:** 60 Ω and 2.5 V / 2.5 V.

### P3 — Frames cross the wire

On the **reader**, start the simulator. Its output is the script truth for
phase 1:

```bash
cd ~/SailinGrace-pi
python3 scripts/n2k_sim.py
#   SailinGrace N2K bench simulator on can0, address 43 - BENCH ONLY
#   t=  1.0 src=43 aws=10kn awa=30 hdg=350.2 bsp=6.5 sog=7.0 depth=25.0 lat=… lon=…
```

On the **boat Pi**:

```bash
candump -ta can0 | head -40
#   expect IDs ending in 2B (source address 43):
#   18EEFF2B [8] …   060928 address claim (once at start)
#   09FD022B [8] …   130306 wind
#   09F1122B [8] …   127250 heading
#   09F8022B [8] …   129026 COG/SOG
#   09F8012B [8] …   129025 position
#   09F5032B [8] …   128259 speed through water
#   0DF50B2B [8] …   128267 depth
ip -details -statistics link show can0     # ERROR-ACTIVE, berr-counter tx 0 rx 0, bus-off 0
sudo python3 ~/SailinGrace-pi/scripts/discover_signals.py scan --channels can --duration 10 --json
```

**Pass:**
- No `WARN` from the simulator
- All seven PGNs present on the boat Pi
- `discover_signals` rates are about 10 Hz for wind, heading, COG/SOG and
  position, and about 1 Hz for speed and depth (±10 %)
- Error counters stay at 0 on both Pis

| Symptom | Likely cause |
|---|---|
| Simulator `WARN: send failed`, boat Pi sees nothing | H/L swapped; termination (re-check P2); a wrong oscillator on either Pi (PiCAN-M 16 MHz, Waveshare 12 MHz): re-run `setup_pi.sh` with `CAN_HAT=` / `CAN_OSCILLATOR=` |
| Frames arrive but error counters climb | Termination or a long untwisted H/L pair |
| `can0` drops to BUS-OFF and comes back | That's the bus-off recovery working; find the wiring fault |

### P4 — Decode on the boat Pi (independent of SignalK)

```bash
# on the boat Pi — install canboatjs' command-line tools once:
sudo npm i -g @canboat/canboatjs
candump -L can0 | analyzerjs | grep -E '"pgn":(130306|127250)' | head
```

**Pass:** wind speed, wind angle and heading match the simulator's line for
the same moment.

(The simulator's encoding was checked against canboatjs and signalk-server's
N2K→SignalK mapping before this test was written, so a mismatch here
points at the bus or the Pi.)

### P5 — SignalK on the boat Pi

From the laptop (PowerShell: use `curl.exe`):

```bash
curl http://10.55.0.1:3000/signalk/v1/api/vessels/self/environment/wind/angleApparent
curl http://10.55.0.1:3000/signalk/v1/api/vessels/self/navigation/headingMagnetic
```

Better: open **Data Browser** in the admin UI next to the simulator output,
and watch one full 120 s cycle:

| Sim time `t` | Simulator shows | SignalK must show |
|---|---|---|
| 0–40 s | aws=10 awa=30 | speedApparent **5.14**, angleApparent **+0.524** |
| 40–60 s | aws=10 awa=45 | angleApparent **+0.785** |
| 60–80 s | aws=20 awa=45, hdg 0→… | speedApparent **10.29**; headingMagnetic wraps **6.28 → 0.00** at t = 60 |
| 80–120 s | aws=20 awa=330 | angleApparent **−0.524** (negative = port, **not** +5.76) |
| always | bsp=6.5 sog=7.0 depth=25.0 | speedThroughWater **3.34**, speedOverGround **3.60**, depth.belowTransducer **25.0**, courseOverGroundTrue = heading |

Also check:
- [ ] Position creeps along COG (about 3.6 m/s), with no jumps
- [ ] The Dashboard's Deltas/sec is steady
- [ ] The source shows as `can0.43`. The Python simulator doesn't answer
      product-info requests, so no product name appears; the ESP32 does
      that in phase 2.
- [ ] The boat Pi's SignalK delta log (sailingrace-logger) is growing

**Pass:** every row and every box.

### P6 — SailinGrace on the laptop

Use a current SailinGrace `master`: the boat setup lives in Settings →
System, and the app finds signalk-server by mDNS
([system_architecture.md § Where the setup lives](https://github.com/atrainathought/SailinGrace/blob/master/docs/system_architecture.md#where-the-setup-lives)).
Launch with `INGEST_SOURCE=synthetic` so these samples can't be mistaken
for real instrument data in the database.

1. **Settings → System:** combination **C** (Pi relays, the laptop runs
   SailinGrace), instruments **SignalK**, SailinGrace on the **laptop**.
   That matches Lynx's `config/boats/lynx.yaml`.
2. **Settings → Network:** the boat Pi's signalk-server should be offered
   as a one-tap choice. mDNS over USB-C depends on the laptop's OS and
   firewall. If nothing is offered within about 15 s, enter
   `ws://10.55.0.1:3000/signalk/v1/stream?subscribe=self` by hand, and note
   which happened. The reader Pi runs signalk-server too, but has no `can0`
   source. If it shows up in the list, **don't pick it**.
3. **Pre-race check** (Settings → System): the feed-type check passes.

```bash
curl http://localhost:8000/api/sensors/capabilities   # available: true, source: "signalk"
curl http://localhost:8000/api/system/setup           # combination C, instruments signalk
```

**Pass:**
- The pre-race check is green for instruments
- Wind, heading and position light up
- The dashboard shows 10 → 20 kn and starboard → port in the right units
- Heading crosses north with no 180° spike in the UKF output
- Freshness indicators show green

### P7 — Fault handling

| Test | Do | Pass |
|---|---|---|
| Unplug the CAN link | Pull it for 30 s, then reconnect | The simulator may print `WARN` while nothing acknowledges its frames. The boat Pi stays up, and data resumes in SignalK within about 5 s. SailinGrace's freshness goes stale, then recovers. |
| Quiet feed | Stop the simulator (Ctrl-C) for more than 60 s | Freshness goes red, then recovers when it's restarted. A new address claim (`18EEFF2B`) appears on the boat Pi. No **Switch** banner appears and the pre-race check stays quiet: both act only when a *second* SignalK server with data is visible. The drop isn't logged anywhere yet (backlog SYS-5). |
| Reboot the boat Pi | `sudo reboot` | The simulator WARNs until the boat Pi's `can0` is back; then signalk-server reconnects by itself |
| Force bus-off | Short H–L for about 2 s with a jumper | `ip -statistics` on both Pis shows `bus-off` 1 or more, **and** both links return to ERROR-ACTIVE without you doing anything (restart-ms 100) |
| Power sag | Run for 30 min, then re-check `vcgencmd get_throttled` on the boat Pi | Still `0x0` |
| Soak | Overnight | Rates unchanged. The logger respects `MAX_LOG_MB`. No growth in signalk-server memory (`systemctl status signalk-server`). |

### P8 — Golden recording

While the simulator runs, record on the boat Pi:

```bash
mkdir -p ~/bench && cd ~/bench && timeout 300 candump -l can0   # 5 min → candump-<date>.log
scp ~/bench/candump-*.log pi@sg-reader.local:bench/             # make ~/bench on the reader first
```

To replay it, stop the simulator first (the same source address would
collide), then on the reader run `canplayer -I ~/bench/candump-*.log -l i`.
That puts real frames on the wire into the boat Pi. Re-run P5 and P6
against it after any change to `setup_pi.sh`, signalk-server or the
logger.

---

## Phase 2 — add the ESP32

**Stop `n2k_sim.py` on the reader** before the ESP32 starts sending.
Otherwise SignalK gets the same data from two sources; see the optional
two-sources test in E5.

### E1 — Flash the ESP32 and check it alone (not on the bus yet)

Laptop connections: boat Pi (USB-C) + ESP32 (USB).

```bash
cd SailinGrace-esp32/firmware-n2ksim
pio run -t upload                   # auto-detects the port; or --upload-port /dev/ttyUSB0 / COM5
pio device monitor                  # 115200
```

Expected output:

```
SailinGrace N2K bench simulator — BENCH ONLY
WARN: SendMsg failed (no other node on the bus to ACK?)
t=  3.0 src=42 aws=10kn awa=30 hdg=350.5 bsp=6.5 sog=7.0 depth=25.0 lat=… lon=…
```

**Pass:**
- Flashing succeeds
- One status line per second, with `t` wrapping at 120
- The `WARN` appears while it's unconnected, which proves it notices
  nothing is acknowledging its frames

### E2 — Wire it in

Power everything off. Add the ESP32 to the reader HAT's terminal (CANH →
H, CANL → L, GND → reader GND pin). **Flip the HAT's 120R switch OFF**,
because the reader is now in the middle. Fit 120 Ω at the ESP32 and leave
the PiCAN-M jumper ON.

Measure **~60 Ω** across H/L again. With power on, H and L should each be
≈ 2.5 V.

### E3 — Three nodes: frames, independently checked

ESP32 serial: the `WARN`s stop, and `src=42` is shown.

On the **reader** (now the bus reference) **and** on the **boat Pi**:

```bash
candump -ta can0 | head -20       # IDs now end in 2A (address 42)
sudo python3 ~/SailinGrace-pi/scripts/discover_signals.py scan --channels can --duration 10 --json
```

**Pass:**
- Both Pis show the **same PGN set and rates** as P3, with the source now
  42. If they disagree, the boat Pi is losing frames.
- Error counters stay at 0 on both

### E4 — Repeat the decode, SignalK and SailinGrace checks

Re-run **P4** (decode on the **reader** this time, so it's fully
independent), then **P5** and **P6**, with the ESP32 serial monitor as the
script truth. Phase 2 adds:

- [ ] SignalK lists the source with its product name, "SailinGrace N2K
      bench sim". signalk-server asks for product info (126996, a
      multi-frame message) and the ESP32 answers.
- [ ] The values in the P5 table match exactly as before

### E5 — Faults with three nodes

| Test | Do | Pass |
|---|---|---|
| Unplug the boat Pi | Pull the PiCAN-M's M12 for 30 s | The ESP32 shows **no** WARNs, because the reader still acknowledges its frames. The reader's `candump` doesn't miss a beat. SignalK resumes when it's reconnected. |
| Reset the ESP32 | Press its reset button | A new address claim (`18EEFF2A`) appears on both Pis, and data resumes |
| Address conflict | Start `n2k_sim.py --src 42` on the reader | The two sides resolve address 42 by ISO NAME: the ESP32 (lower unique number) keeps 42, and the Python simulator prints "moving to 43". Stop the simulator afterwards. |
| Two sources (optional) | Run `n2k_sim.py` (address 43) alongside the ESP32 | SignalK shows both sources for the same paths. Note which one SailinGrace follows. Real boats often have duplicate sensors, so it's worth knowing. Stop it afterwards. |

---

## Results sheet

| Date | Laptop OS | Link (direct / backbone) | Step | Pass/Fail | Notes |
|---|---|---|---|---|---|
| | | | P1 both Pis | | throttled: ___ / ___  restart-ms: ___ / ___ |
| | | | P2 physical | | Ω: ___  V: ___ / ___ |
| | | | P3 frames | | rates: |
| | | | P4 decode | | |
| | | | P5 SignalK | | |
| | | | P6 SailinGrace | | mDNS offered the Pi? Y / N |
| | | | P7 faults | | |
| | | | P8 golden log | | file: |
| | | | E1 ESP32 alone | | port: ___ |
| | | | E2 wiring | | Ω: ___ |
| | | | E3 three nodes | | reader vs boat Pi rates: |
| | | | E4 decode / SignalK / app | | product name shown? Y / N |
| | | | E5 faults | | |

When P1–P7 and E1–E5 pass, the Pi's N2K path is proven on the bench with
an independent bus reader. That covers [bench_test.md](bench_test.md)
Levels 3–7 without a laptop adapter. What's left is a real backbone with
real instruments (Level 8), with **both simulators off** the boat bus.
