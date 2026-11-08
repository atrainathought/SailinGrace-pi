# USB bench test — ESP32 + Pi on one dev computer

Tracked as **N2K-1** in the [SailinGrace backlog](https://github.com/atrainathought/SailinGrace/blob/master/docs/backlog.md#n2k-1-bench-test-the-nmea-2000-path).

A cut-down version of [bench_test.md](bench_test.md) for this setup: the
dev computer has **no N2K adapter**. Instead, the ESP32 and the Pi each
plug into it over USB, and the two of them talk to each other over CAN.

```
                       ┌──────────── dev computer ────────────┐
                       │  serial monitor    browser / curl /  │
                       │  (script truth)    ssh / SailinGrace │
                       └──────┬─────────────────────┬─────────┘
             USB (power+serial)│                     │USB-C (power + USB-ethernet, 10.55.0.1)
                       ┌──────┴──────┐  CAN link  ┌─┴──────────────┐
                       │ ESP32 +     ├────────────┤ Pi + PiCAN-M   │
                       │ transceiver │ (2 nodes)  │ can0 → SignalK │
                       └─────────────┘            └────────────────┘
                         GENERATOR                 DEVICE UNDER TEST
```

**What counts as truth:**

| Layer | Where you read it | What it proves |
|---|---|---|
| Script | ESP32 serial monitor | What the generator *meant* to send |
| Bus | `candump can0` on the **reader Pi** if you have one (below), else on the Pi under test (kernel SocketCAN, below canboat-js / SignalK) | What actually crossed the wire |
| Relay | SignalK REST / Data Browser at `10.55.0.1:3000` | What the Pi decoded |
| App | SailinGrace on the dev computer | What routing / fusion sees |

Without a second CAN interface, the bus truth comes from the Pi's own
kernel rather than a separate device. That's acceptable, because the kernel
CAN layer is independent of the canboat-js → SignalK decode path being
tested. A **second Pi with a Waveshare RS485 CAN HAT** removes the
compromise: it becomes a separate bus reader. See
[Optional: a reader Pi](#optional-a-reader-pi-pi-4b--waveshare-rs485-can-hat).

---

## Before you start

### Hardware

- [ ] ESP32 with transceiver wired as in [firmware-n2ksim/README](https://github.com/atrainathought/SailinGrace-esp32/tree/main/firmware-n2ksim#wiring-feather-huzzah32) (TX GPIO 32, RX GPIO 27)
- [ ] Pi 4B + PiCAN-M with `setup_pi.sh` already run and rebooted once,
      and `can0` added as a signalk-server source ([install_rpi.md § 3](install_rpi.md#3-wire-can0-as-a-signalk-source))
- [ ] ESP32 USB cable that carries data (some micro-USB cables are power-only)
- [ ] USB-C cable for the Pi, plugged into a **USB-C port** on the dev
      computer (USB-A is marginal for a Pi 4 + HAT)
- [ ] The CAN link between them: choose A or B below
- [ ] Multimeter

> **Pi 400:** its USB-C port is power only, with no USB-ethernet (gadget)
> mode. Power it separately and use ethernet, then replace `10.55.0.1` with
> `<pi-hostname>.local` throughout.

### The CAN link: choose one

**A. Direct two-node cable (simplest, no backbone).**
Make an M12 pigtail from the ESP32 transceiver (CANH → pin 4 white,
CANL → pin 5 blue, GND → pin 3 black) and plug it straight into the
PiCAN-M. A two-node bus still needs **both ends terminated**:

- PiCAN-M termination jumper **ON**
- 120 Ω across CANH/CANL at the ESP32 end (the transceiver board's
  termination resistor if it has one, or a loose resistor)

**B. Through the bench backbone.**
Use backbone tees with the two 120 Ω terminators. PiCAN-M jumper **OFF**
and no termination at the ESP32.

**In both cases, leave the 12 V bus power OFF for this test.** CAN
signalling doesn't need NET-S, because each transceiver is powered by its
own board (the ESP32 from USB, the PiCAN-M from the Pi). If the backbone is
powered and your PiCAN-M has its 12 V→5 V regulator fitted, the Pi would be
powered from the bus **and** from the dev computer's USB-C at the same time.
Two supplies on the Pi's 5 V rail is asking for trouble.

Ground: both boards share the dev computer's USB ground, so you don't need
a separate ground wire beyond M12 pin 3.

### Optional: a reader Pi (Pi 4B + Waveshare RS485 CAN HAT)

A second Pi 4B with a **Waveshare RS485 CAN HAT** (MCP2515 + a 3.3 V
transceiver) is a full Linux CAN node. It does the job of the laptop N2K
adapter in [bench_test.md](bench_test.md):
- it's the **independent bus reader**, so the PiCAN-M Pi is checked
  against a separate device;
- it can **replay recordings** onto a real bus with `canplayer`.

Keep the PiCAN-M Pi as the device under test. It's the boat unit.

**Set it up:**

1. Give it its own hostname (e.g. `sg-reader`, via `raspi-config`), so
   `.local` names don't clash with the PiCAN-M Pi.
2. Check the crystal on the HAT. Current boards say **12.000** (12 MHz);
   older ones are 8 MHz.
3. Run the setup with the matching HAT setting, then reboot:
   ```bash
   CAN_HAT=waveshare bash scripts/setup_pi.sh          # 12 MHz
   # older 8 MHz board:  CAN_HAT=waveshare CAN_OSCILLATOR=8000000 bash scripts/setup_pi.sh
   sudo reboot
   ```
   It installs everything the boat Pi gets. On the reader you only need
   `can0` and can-utils, so **don't** add `can0` as a signalk-server source.
4. Check the HAT came up:
   ```bash
   dmesg | grep -i mcp251        # "MCP2515 successfully initialized"
   ip -details link show can0    # UP, bitrate 250000, restart-ms 100
   ```

**Reach it:** give it **its own 5 V / 3 A supply** and connect over
ethernet or WiFi as `sg-reader.local`. Two Pi 4s on the dev computer's
USB-C ports is more power than most laptops supply. Also, both USB-C links
default to `10.55.0.1`, so the dev computer couldn't tell them apart.

If you must use USB-C for both, move the reader to another subnet:
`sudo nmcli connection modify sg-usb ipv4.addresses 10.56.0.1/24 && sudo nmcli connection up sg-usb`.
Then reach it at `10.56.0.1`. Its config page will ask for the setup
token there.

**Wire it in.** The HAT's green terminal has **H** and **L** (CAN) and
**B** / **A** (RS485, unused). It has **no ground terminal**, so take
ground from a Pi header GND pin (pin 6).

The generic 4-wire lead with a small white plug that comes with some of
these HATs is **not** an N2K cable: wrong connector, wrong colors. For N2K,
white = CAN_H and blue = CAN_L.

| Link | Wiring | Termination (two ends only) |
|---|---|---|
| **R1. ESP32 ↔ reader only** (quickest first test, no M12 needed) | Three jumper wires: ESP32 transceiver CANH → **H**, CANL → **L**, GND → Pi GND pin 6 | HAT **120R switch, CAN = ON** + 120 Ω at the ESP32 |
| **R2. ESP32 — reader — PiCAN-M** (three nodes, no backbone) | As R1, plus an M12 **male** pigtail from the same H / L / GND points to the PiCAN-M (white → H, blue → L, black → GND) | ESP32 120 Ω **ON** · HAT switch **OFF** (it's in the middle) · PiCAN-M jumper **ON** |
| **R3. Backbone** | M12 **male** drop cable → H (white), L (blue), Pi GND (black). **Red (12 V) unconnected:** the HAT has no 12 V input. | HAT switch **OFF**; the backbone's two terminators do it |

In every case, measure **~60 Ω** across H/L with the power off (U3).
Backbone tees have **female** sockets, so anything plugged into one needs
a **male** M12.

### Dev computer software

| | Linux | Windows |
|---|---|---|
| ESP32 serial driver | CP2104 is in-kernel → `/dev/ttyUSB0`. Add yourself to `dialout` (`sudo usermod -aG dialout $USER`, then log out and back in). | Silicon Labs **CP210x VCP driver** (usually installed by Windows Update) → `COMx` in Device Manager |
| PlatformIO | `pipx install platformio` or the VS Code extension | VS Code + PlatformIO extension |
| Pi USB-ethernet | Shows up automatically as `usb0`/`enx…`, with a DHCP lease in 10.55.0.x | Shows up as **"RNDIS/Ethernet Gadget"**. If Device Manager lists it under *Other devices* with no driver: *Update driver → Browse → Let me pick → Network adapters → Microsoft → Remote NDIS Compatible Device*. |
| Shell tools | `curl`, `ssh` | PowerShell has `curl.exe` and `ssh` built in |
| SailinGrace | Native or Docker, per its README | Docker / WSL2, per its README |

---

## The test ladder

Run in order and don't skip ahead. Record each result in the results sheet
at the bottom.

### U1 — Dev computer ↔ Pi over USB-C

Plug in the Pi's USB-C. Wait about 60 s for it to boot.

```bash
ping 10.55.0.1                      # Windows: ping 10.55.0.1
ssh pi@10.55.0.1                    # use your Pi username
```

On the Pi (over SSH):

```bash
vcgencmd get_throttled              # expect throttled=0x0 (no under-voltage)
ip -details -statistics link show can0
#   expect: state UP (or ERROR-ACTIVE), bitrate 250000, restart-ms 100
systemctl is-active signalk-server sailingrace-logger
```

Browser: `http://10.55.0.1:8080` (config UI) and `http://10.55.0.1:3000`
(signalk-server admin).

**Pass:**
- ping and SSH work
- `throttled=0x0`
- `can0` is UP at 250000 with `restart-ms 100`
- both services are active

`restart-ms 0` means the bus-off fix isn't applied yet. Re-run
`bash scripts/setup_pi.sh` from an updated checkout.

> If `get_throttled` isn't `0x0`, the dev computer's port can't power the
> Pi. Use a powered USB-C hub or a separate Pi supply, and keep the
> dev-computer cable for data.

### U2 — Dev computer ↔ ESP32 over USB (CAN link still unplugged)

```bash
cd SailinGrace-esp32/firmware-n2ksim
pio run -t upload                   # auto-detects the port; or --upload-port /dev/ttyUSB0 / COM5
pio device monitor                  # 115200
```

Expect a banner, then one status line per second. With **no other node
on the bus**, you should also see:

```
SailinGrace N2K bench simulator — BENCH ONLY
WARN: SendMsg failed (no other node on the bus to ACK?)
t=  3.0 src=42 aws=10kn awa=30 hdg=350.5 bsp=6.5 sog=7.0 depth=25.0 lat=… lon=…
```

**Pass:**
- Flashing succeeds
- The status lines tick once per second
- `t` counts up and wraps at 120
- The `WARN` appears, which proves the firmware notices that nothing is
  acknowledging its frames

Leave the serial monitor open for the rest of the test.

**With a reader Pi: quick win (R1).** Before touching the PiCAN-M, wire
the ESP32 to the reader alone (R1 above). The ESP32's `WARN` lines should
stop, and `candump can0` on the reader should show the frames listed in U4.
This proves the ESP32, its transceiver and the firmware with nothing but
jumper wires. Then move to R2 or R3 for the rest of the test.

### U3 — Physical link (everything unplugged from USB)

Unplug both USB cables. Connect the CAN link (A or B). Measure across
CANH/CANL at any accessible point:

| Reading | Means |
|---|---|
| **~60 Ω** | ✅ two terminators |
| ~120 Ω | one end isn't terminated (option A: PiCAN-M jumper or the ESP32-end resistor) |
| ~40 Ω | three terminators (option B: a jumper is ON as well as the backbone terminators) |
| 0 Ω / open | swapped or broken wire |

Plug both USB cables back in. With the Pi booted, measure CANH and CANL to
GND: both should be **≈ 2.5 V** when idle.

**Pass:** 60 Ω and 2.5 V / 2.5 V.

### U4 — Frames cross the wire

ESP32 serial: the `WARN` lines **stop**, and `src=42` is shown (or another
address if the Pi's canboat-js already holds 42).

On the Pi:

```bash
candump -ta can0 | head -40
#   expect 29-bit IDs ending in the sim's source address (…2A for 42), e.g.
#   09FD022A [8] …   130306 wind
#   09F1122A [8] …   127250 heading
#   09F8022A [8] …   129026 COG/SOG
#   09F8012A [8] …   129025 position
ip -details -statistics link show can0
#   expect: ERROR-ACTIVE; berr-counter tx 0 rx 0; bus-off 0
sudo python3 ~/SailinGrace-pi/scripts/discover_signals.py scan --channels can --duration 10 --json
```

**Pass:**
- Frames are present
- The `discover_signals` PGN set includes 130306, 127250, 129026, 129025,
  128259 and 128267
- Rates are about 10 Hz for the first four and about 1 Hz for the last two
  (±10 %)
- Error counters stay at 0
- **With a reader Pi:** the same `candump` / `discover_signals.py` on the
  reader shows the **same PGN set and rates** as the Pi under test. If they
  disagree, the Pi under test is losing frames.

| Symptom | Likely cause |
|---|---|
| ESP32 still WARNs, Pi sees nothing | CANH/CANL swapped, TX/RX swapped at the transceiver, or a wrong oscillator in `config.txt` (PiCAN-M 16 MHz, Waveshare 12 MHz or 8 MHz on old boards); fix it by re-running with `CAN_HAT=` / `CAN_OSCILLATOR=` |
| Pi sees frames but error counters climb | Bad termination (re-check U3) or a long unshielded pigtail |
| `can0` drops to BUS-OFF and comes back | That's the bus-off recovery working; find the wiring fault |

### U5 — Decode on the Pi (independent of SignalK)

canboat decodes the raw frames, so the bytes can be checked without
going through SignalK. With a reader Pi, run this **on the reader**: it's
then fully independent of the Pi under test.

```bash
# on the Pi — signalk-server bundles canboatjs; install its CLI once:
sudo npm i -g @canboat/canboatjs
candump can0 | analyzerjs | grep -E '"pgn":(130306|127250)' | head
```

**Pass:** wind speed, angle and heading in the decoded JSON match the ESP32
serial line for the same moment.

If `analyzerjs` won't parse the candump output, build the C canboat
(`git clone https://github.com/canboat/canboat && cd canboat && make`)
and use `candump can0 | candump2analyzer | analyzer` instead.

### U6 — SignalK on the Pi

From the dev computer (PowerShell: use `curl.exe`):

```bash
curl http://10.55.0.1:3000/signalk/v1/api/vessels/self/environment/wind/speedApparent
curl http://10.55.0.1:3000/signalk/v1/api/vessels/self/environment/wind/angleApparent
curl http://10.55.0.1:3000/signalk/v1/api/vessels/self/navigation/headingMagnetic
curl http://10.55.0.1:3000/signalk/v1/api/vessels/self/navigation/position
```

Better: open **Data Browser** in the admin UI (`http://10.55.0.1:3000`)
next to the serial monitor, and watch one full 120 s cycle:

| Sim time `t` | Serial shows | SignalK must show |
|---|---|---|
| 0–40 s | aws=10 awa=30 | speedApparent **5.14**, angleApparent **+0.524** |
| 40–60 s | aws=10 awa=45 | angleApparent **+0.785** |
| 60–80 s | aws=20 awa=45, hdg 0→… | speedApparent **10.29**; headingMagnetic wraps **6.28 → 0.00** at t = 60 |
| 80–120 s | aws=20 awa=330 | angleApparent **−0.524** (negative = port, **not** +5.76) |
| always | bsp=6.5 sog=7.0 depth=25.0 | speedThroughWater **3.34**, speedOverGround **3.60**, depth.belowTransducer **25.0**, courseOverGroundTrue = heading |

Also check:

- [ ] Position creeps along COG (about 3.6 m/s), with no jumps
- [ ] The Dashboard's Deltas/sec is steady
- [ ] The sim shows up as a source, under **Server → Data Fiddler / Sources**:
      "SailinGrace N2K bench sim" with the address from the serial line
- [ ] The Pi's SignalK delta log (sailingrace-logger) is growing

**Pass:** every row and every box.

### U7 — SailinGrace on the dev computer

Use a current SailinGrace `master`: the boat setup now lives in Settings →
System, and the app finds signalk-server by mDNS
([system_architecture.md § Where the setup lives](https://github.com/atrainathought/SailinGrace/blob/master/docs/system_architecture.md#where-the-setup-lives)).
Launch with `INGEST_SOURCE=synthetic` so the sim's samples can't be
mistaken for real instrument data in the database.

1. **Settings → System.** Set combination **C** (Pi relays, the laptop
   runs SailinGrace), instruments **SignalK**, SailinGrace on the
   **laptop**. This matches Lynx's `config/boats/lynx.yaml`, so it's
   usually already the default.
2. **Settings → Network.** The Pi's signalk-server should be offered as a
   one-tap choice (it advertises `_signalk-ws._tcp`), and Settings → System
   should list it under what's on the network. mDNS over the USB-C link
   depends on the dev computer's OS and firewall. If nothing is offered
   within about 15 s, enter
   `ws://10.55.0.1:3000/signalk/v1/stream?subscribe=self` by hand. Record
   which one happened.
3. **Pre-race check** (Settings → System). The feed-type check should
   pass: the SignalK feed matches the instruments setting.

```bash
curl http://localhost:8000/api/sensors/capabilities
#   expect available: true, source: "signalk"
curl http://localhost:8000/api/system/setup
#   expect combination C, instruments signalk; the Pi listed under discovered services if mDNS worked
```

**Pass:**
- The pre-race check is green for instruments
- Wind, heading and position light up
- The dashboard shows 10 → 20 kn and starboard → port in the right units
- Heading crosses north with no 180° spike in the UKF output
- Freshness indicators show green

### U8 — Fault handling

| Test | Do | Pass |
|---|---|---|
| Unplug the CAN link | Pull it for 30 s, then reconnect | ESP32 WARNs while it's out. The Pi stays up, and data resumes in SignalK within about 5 s. SailinGrace's instrument freshness goes stale, then recovers. |
| Quiet feed | Pull the CAN link for more than 60 s | The instrument freshness indicator goes red, then recovers when the link is reconnected. No **Switch** banner appears and the pre-race check stays quiet: both act only when a *second* SignalK server is visible. The drop isn't logged anywhere yet (backlog SYS-5). |
| Unplug the Pi under test (reader fitted) | Pull the PiCAN-M's CAN connection | The ESP32 shows **no** WARNs, because the reader still acknowledges its frames. The reader's `candump` doesn't miss a beat. When the PiCAN-M is reconnected, SignalK resumes. |
| Reset the ESP32 | Press its reset button | A new address claim on the Pi (`candump` shows PGN 60928 from the sim), and data resumes |
| Reboot the Pi | `sudo reboot` | ESP32 WARNs until `can0` is back. signalk-server reconnects to can0 by itself after boot. |
| Force bus-off | Short CANH–CANL for about 2 s with a jumper | `ip -statistics` shows a `bus-off` count of 1 or more, **and** the link is back to ERROR-ACTIVE without you doing anything (restart-ms 100) |
| Power sag | Run for 30 min, then re-check `vcgencmd get_throttled` | Still `0x0` |
| Soak | Leave it running overnight | Rates unchanged. The logger respects `MAX_LOG_MB`. No growth in signalk-server memory (`systemctl status signalk-server`). |

### U9 — Save a golden recording

Once U6 passes, record a session for regression tests later:

```bash
# on the Pi
mkdir -p ~/bench && cd ~/bench
timeout 300 candump -l can0          # 5 min → candump-<date>.log
```

Copy it to the dev computer (`scp pi@10.55.0.1:bench/*.log .`). To replay
it later with the **ESP32 unplugged** (the same source address would
collide): `canplayer -I candump-*.log -l i can0` on the Pi. Then repeat U6
and U7 without the ESP32.

**With a reader Pi,** record on the reader and replay from the reader.
That puts real frames on the wire into the PiCAN-M, which is a better test
than the Pi replaying into its own `can0`.

---

## Results sheet

| Date | Dev OS | Link (A/B) | Step | Pass/Fail | Notes |
|---|---|---|---|---|---|
| | | | U1 dev ↔ Pi | | throttled: ___  restart-ms: ___ |
| | | | U2 dev ↔ ESP32 | | port: ___ |
| | | | U2 R1 ESP32 → reader | | reader crystal: ___ MHz |
| | | | U3 physical | | Ω: ___  V: ___ / ___ |
| | | | U4 frames | | rates: |
| | | | U5 decode | | |
| | | | U6 SignalK | | |
| | | | U7 SailinGrace | | |
| | | | U8 faults | | |
| | | | U9 golden log | | file: |

Once U1–U8 pass, the Pi's N2K path is proven on the bench. With the reader
Pi in R2 or R3, that includes an independent bus reader, which covers
[bench_test.md](bench_test.md) Levels 3–7 without buying a laptop adapter.
The remaining gap is a real backbone with real instruments (Level 8).
