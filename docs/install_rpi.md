# Install — Raspberry Pi (boat appliance)

Pi 4 + PiCAN-M kit reading NMEA 2000 from the boat's bus and exposing it as SignalK. This is the long-running, boat-side rig — the Pi powers on with the boat and stays up.

Two variants based on where SailinGrace itself runs:

| Variant | Pi runs | Laptop runs | When to pick |
|---------|---------|-------------|--------------|
| **A1 — All-in-one** | signalk-server + SailinGrace backend + frontend | browser only | No laptop available; pure boat-side appliance |
| **A2 — Pi as relay** (recommended) | signalk-server only | SailinGrace (Docker or venv) | You have a laptop. Better CPU, bigger screen, Pi stays simple |

Hardware bring-up is identical for both; only the SailinGrace-install step differs (A1 installs on the Pi via this doc; A2 installs on the laptop via [install_docker.md](https://github.com/atrainathought/SailinGrace/blob/master/docs/install_docker.md) or [install_dev.md](https://github.com/atrainathought/SailinGrace/blob/master/docs/install_dev.md) and points at the Pi's SignalK URL).

## What's in the kit

- **Raspberry Pi 4B** (4 GB or 8 GB; 4 GB is plenty). Pi 3B+ also works — playbook is identical, just slower (Pi 3B+ is ~2-3× slower in CPU but live ingest at 1 Hz doesn't care). Power-input connector differs: Pi 4 = USB-C; Pi 3B+ = micro-USB
- **PiCAN-M HAT** — exposes `can0` on SocketCAN at 250 kbps for NMEA 2000, plus a screw-terminal NMEA 0183 input
- **Waveshare UPS HAT (D)** — 2× 18650 cell holder. Board labels: **USB-C input** for charging (boat 12 V → USB-C buck → UPS); **USB-B output** for the Pi (USB-B → micro-USB or USB-B → USB-C cable depending on which Pi). Sits at the **top** of the stack. Battery telemetry over I²C (INA219 at `0x42`)
- **SD card** — 64 GB Class 10 minimum; A2-rated (high random IOPS) preferred for the SQLite WAL
- **5V/3A USB-C power supply** for the bench (feeds the UPS HAT, not the Pi). On the boat, feed the UPS HAT's USB-C from a 12 V → 5 V buck or a USB-C car charger off the house bus
- **Marine-grade enclosure** — anything sealed. Even a dry bag works for the first sail
- **NMEA cabling** — DeviceNet T-connector and ~1 m M12 5-pin drop cable for NMEA 2000; bare-wire for NMEA 0183 RX if you also want to read legacy autopilot bridges

## Stack order (bottom to top)

1. **Raspberry Pi** — power input port on the side, unused until the UPS loops back into it (USB-C on Pi 4, micro-USB on Pi 3B+)
2. **PiCAN-M HAT** directly on the 40-pin GPIO header. Leave the on-board termination jumper **OFF** if your boat's N2K backbone already has terminators at both ends (almost always)
3. **Waveshare UPS HAT (D)** on top. Passes I²C through to the GPIO header for battery telemetry. The UPS-output **USB-B** port carries the 5 V the Pi runs on; short cable from there down to the Pi's power input (USB-B → USB-C for Pi 4, USB-B → micro-USB for Pi 3B+). Right-angle adapters reduce strain

PiCAN-M uses SPI (pins 19/21/23/24) + one GPIO interrupt (typically GPIO 25, pin 22). The Waveshare UPS uses I²C (pins 3/5). No collision. Sanity-check after assembly:

```bash
sudo i2cdetect -y 1      # should show device at 0x42 (UPS INA219)
ip link show can0        # should be present and bring-uppable
```

## What's NOT in the kit

- A separate access point. The Pi 4 has Wi-Fi; configure it as an AP so the laptop / tablet joins `SailinGrace-XX` directly — see [Wi-Fi access point](#wi-fi-access-point--pi-as-the-boat-network) below. Alternative: tether the Pi to a phone hotspot
- A display. Headless. UI lives on the tablet/phone browser

## Software bring-up

**Use the script.** Steps 1–5 below are bundled in `scripts/setup_pi.sh` — idempotent, works on Bookworm and Trixie, takes ~5 min. Manual commands kept underneath so you understand what the script does.

The relay Pi (Variant A2) deploys from the **public [`SailinGrace-pi`](https://github.com/atrainathought/SailinGrace-pi) repo** — relay files only (logger + UPS monitor + setup), no routing code, **no PAT needed**. This is the recommended path; it's generated from this repo via `scripts/build_pi_bundle.sh`.

```bash
# Recommended — clone the public relay repo + run (UPS monitor + logger included):
sudo apt update && sudo apt install -y git
git clone https://github.com/atrainathought/SailinGrace-pi.git
bash SailinGrace-pi/scripts/setup_pi.sh

# Reboot once after first run so the MCP2515 overlay loads:
sudo reboot
```

No git on the Pi at all? Build the bundle on your machine and copy it over:

```bash
# On a machine with the main repo:
scripts/build_pi_bundle.sh
scp dist/sailingrace-pi.tar.gz pi@<pi-host>:~
ssh pi@<pi-host> 'tar xzf sailingrace-pi.tar.gz && bash SailinGrace/scripts/setup_pi.sh && sudo reboot'
```

> **A1 only** (Pi runs the full app — not the recommended setup) needs the **private** main repo and a GitHub personal-access-token (classic, `repo` scope): `git clone https://<PAT>@github.com/atrainathought/SailinGrace.git`. The relay path above does not.

After the script runs, do four things by hand (interactive / browser-based — not scriptable):

### 1. Run `signalk-server-setup` (~2 min)

```bash
signalk-server-setup
```

Answer the prompts:

| Prompt | Answer | Why |
|---|---|---|
| **Use port 80?** | `no` | Port 80 needs root and silently breaks any other web service. Stick with 3000. |
| **Enable SSL?** | `no` | Self-signed cert friction on every visit; iOS Safari blocks `ws://` once it's seen `https://` from the same host. Private boat network — threat model doesn't justify it. |
| **Install as system service (sudo)?** | `yes` | Installs `signalk.service` so it starts at boot. Per-user install only starts when `pi` logs in (useless headless). |

Verify:

```bash
sudo systemctl status signalk.service       # expect: active (running)
journalctl -u signalk.service -f --since '1 min ago'   # Ctrl-C after a few lines
```

Expected log noise that **is fine**:

- `Could not parse security config at /home/pi/.signalk/security.json: ENOENT` — file doesn't exist *yet*; admin UI creates it on first visit
- `ExperimentalWarning: WASI is an experimental feature` — Node.js notice; cosmetic
- `signalk-server running at 0.0.0.0:[object Object]` — log-formatting bug in signalk-server; server IS listening on 3000

### 2. Create the admin user (~30 s)

Newer signalk-server versions ship with **security enabled by default** — every API call returns `Unauthorized` until an admin exists. Open the admin UI from your laptop:

```
http://<pi>.local:3000/admin
```

First visit shows a "Create admin user" wizard. Pick any username/password. This writes `~/.signalk/security.json` on the Pi and you land on the admin dashboard.

### 3. Wire `can0` as a SignalK source

In the admin UI: **Server → Connections → Add**. Fill in:

| Field | Value |
|---|---|
| **Provider ID** | `nmea2000` (just a label) |
| **Type** | `NMEA 2000` |
| **Source** | `canboat-js` |
| **Interface** | `can0` |

Submit and **Activate**. Then **Server → Dashboard** — watch the "Deltas/sec" counter. Zero on the bench (no N2K bus attached); non-zero means canboat-js is processing live traffic.

> **Headless alternative — `discover_signals.py`.** Instead of wiring the source by hand here, the Pi can find which of its inputs is live and configure the connection itself, no admin UI. This is also the tool to use *first* to record raw data and work out a source's parse format. See [Signal discovery](#signal-discovery) below.

### 4. Enable anonymous read so SailinGrace can subscribe without a token

By default, security-on means even the WebSocket subscription needs auth — and we don't want the boat-side feed breaking from token expiry mid-passage. Easiest fix:

**Security → Settings → Allow Readonly: ON** → Save.

That lets anonymous clients read `vessels.self` (which is all SailinGrace's WebSocket needs) without a token. Verify from the Pi:

```bash
curl -s http://localhost:3000/signalk/v1/api/vessels/self | head
# Should return JSON instead of "Unauthorized"
```

Alternatives if your signalk-server version doesn't have that toggle:

- **Device token**: Security → Devices → Add → name `sailingrace-laptop`, permissions `read` → copy the token → bake into the laptop's launch:
  ```bash
  SIGNALK_URL="ws://<pi>.local:3000/signalk/v1/stream?subscribe=self&token=<TOKEN>"
  ```
- **Access request flow**: start SailinGrace without a token; approve via Security → Access Requests when the WebSocket client requests access.

### 5. Launch SailinGrace

**Variant A2 (recommended)** — Pi is the SignalK relay, SailinGrace runs on a laptop. Install on the laptop via [install_docker.md](https://github.com/atrainathought/SailinGrace/blob/master/docs/install_docker.md) or [install_dev.md](https://github.com/atrainathought/SailinGrace/blob/master/docs/install_dev.md), then in the Settings modal (Network tab) point at `ws://<pi>.local:3000/signalk/v1/stream?subscribe=all`. Pi side: **nothing more to do**.

**Variant A1** — Pi runs everything. Install via Docker on the Pi:

```bash
# In the SailinGrace clone already on the Pi:
cd ~/SailinGrace
# Install Docker if not already present:
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker pi
# Log out and back in, then:
docker compose up -d --build

# Configure the local SignalK URL via the Settings modal:
# http://<pi>.local:8000   →   ⚙ → Network → ws://localhost:3000/signalk/v1/stream?subscribe=all
```

Or, for the venv install (no Docker), follow [install_dev.md](https://github.com/atrainathought/SailinGrace/blob/master/docs/install_dev.md) on the Pi.

## Manual command reference (what the script does for you)

```bash
# 1. Pi OS 64-bit Lite (Bookworm or Trixie), SSH enabled. raspi-config →
#    Interfaces → SPI ON + I2C ON.
sudo raspi-config nonint do_spi 0
sudo raspi-config nonint do_i2c 0

# 2. PiCAN-M device tree overlay. MCP2515 needs to be declared in
#    /boot/firmware/config.txt with the right oscillator (16 MHz for
#    PiCAN-M; some clones are 8 MHz — wrong value = silent can0).
sudo tee -a /boot/firmware/config.txt <<'EOF'
dtparam=spi=on
dtparam=i2c_arm=on
dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=25
dtoverlay=spi-bcm2835-overlay
EOF
sudo reboot

# 3. SocketCAN — bring up can0 at 250kbps (NMEA 2000 standard).
sudo apt install -y can-utils
sudo tee /etc/systemd/network/80-can.network <<'EOF'
[Match]
Name=can0

[CAN]
BitRate=250000
EOF
sudo systemctl enable --now systemd-networkd
ip -details link show can0    # expect: UP, type can, bitrate 250000

# Smoke-test wiring (need backbone powered + instruments on):
candump can0
# Expect frames in the 0x09FF... range. If silent, check: backbone power,
# termination, T-connector orientation, oscillator value.

# 4. signalk-server (the Pi-side broker).
curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo bash -
sudo apt install -y nodejs
sudo npm install -g signalk-server
signalk-server-setup
sudo systemctl enable --now signalk-server

# 5. UPS monitor (always on the Pi — A1 and A2).
sudo apt install -y python3-venv git
git clone https://github.com/atrainathought/SailinGrace.git
cd SailinGrace
pip install --break-system-packages smbus2    # or use a venv
sudo cp scripts/systemd/ups-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ups-monitor
journalctl -u ups-monitor -f       # confirm battery telemetry is flowing
```

## UPS HAT (D) — graceful shutdown

The HAT keeps the Pi running until the cells flatline, then drops the rail. That cold power-off mid-write is exactly the failure mode the UPS was bought to prevent. `scripts/ups_monitor.py` polls the on-board INA219 over I²C and calls `systemctl poweroff` when bus voltage drops below `UPS_LOW_V` (default 6.4 V on the 2-cell pack, ~10 % remaining, ~2 min buffer).

Tune via env vars on the systemd unit (`scripts/systemd/ups-monitor.service`):

| Var | Default | Purpose |
|---|---|---|
| `UPS_LOW_V` | `6.4` | Shutdown threshold in volts. Sized for 2× 18650; raise to 6.6 for more margin |
| `UPS_POLL_S` | `30` | Poll interval seconds |
| `UPS_I2C_ADDR` | `0x42` | INA219 address; some Waveshare boards ship at `0x40` or `0x43` — check `i2cdetect -y 1` |
| `UPS_DRY_RUN` | unset | When set, logs instead of shutting down. Use on the bench to watch a discharge curve |

## Pi-side raw SignalK delta logger (enabled by default)

`scripts/pi/sailingrace-logger.service` runs `scripts/capture_signalk.py` continuously against the local signalk-server, rotating to a new file at UTC midnight. Output lands in `/home/pi/sailingrace-logs/signalk_<YYYY-MM-DD>.log` — newline-delimited JSON, timestamped. **This is the relay Pi — the logger is not the SailinGrace app, just a disk capture.**

`scripts/setup_pi.sh` installs *and enables* it as part of the one install (it also creates the `.venv` and installs the `websockets` dep the capture needs). Confirm it's running:

```bash
journalctl -u sailingrace-logger -f       # confirm it's tailing deltas
ls -la /home/pi/sailingrace-logs/          # see the rolling file
```

### It can't fill the SD card

The logger is bounded three ways (env vars on the unit; defaults shown) so a full card can never crash the Pi:

| Var | Default | What it caps |
|---|---|---|
| `KEEP_DAYS` | `14` | Delete log files older than this many days |
| `MAX_LOG_MB` | `4000` | Delete oldest files until the log dir is under this size |
| `MIN_FREE_MB` | `1000` | Hard filesystem free-space floor — below it the logger prunes its own logs and, if *something else* is filling the card, **pauses writing** instead of consuming the last free bytes |
| `GUARD_INTERVAL_S` | `900` | How often the above are re-checked (also the capture-chunk length; capture appends, so it's still one file per UTC day) |

These checks run continuously, not just at startup, so a multi-week passage without a reboot stays bounded. Tune the values on the unit (`/etc/systemd/system/sailingrace-logger.service`) to your SD card — e.g. a 32 GB card might want `MAX_LOG_MB=2000`. `journald`'s own logs are separately self-capping (`SystemMaxUse`), so they won't fill the card either.

Independent of `sailingrace.service` — the Pi logs whether the laptop is connected or not. Feeds the post-trip pipeline-adjustment loop (PROJECT.md #15-19).

## Simulation Pi — fake boat for testing (no instruments)

A second, much simpler Pi (or any spare machine) that pretends to be the
boat's instruments, so you can exercise the whole laptop ingest path —
coalescer → UKF → estimators → `instrument_samples` → dashboard — with
no PiCAN-M, no N2K backbone, nothing on the water.

`scripts/nmea0183_simulator.py` is a single self-contained, stdlib-only
file — no `pip` / venv / Docker for the TCP/UDP transports. **If the Pi
already has the repo** (the real-instrument Pi does), it's already there:
just `python3 scripts/nmea0183_simulator.py …`.

For a fresh, dedicated sim Pi, clone the repo the same way everything
else deploys (private repo → personal-access-token URL; Pi OS Lite needs
`git` first), then run the script straight from the clone:

```bash
sudo apt update && sudo apt install -y git
git clone https://<PAT>@github.com/atrainathought/SailinGrace.git
python3 SailinGrace/scripts/nmea0183_simulator.py --transport udp --port 10110
```

Update later with `git pull` — no loose files to re-copy. Unlike the
full appliance, the sim needs neither `setup_pi.sh` nor Docker; it's just
`python3` + the script.

> **You may not even need a Pi.** The simulator runs anywhere — for pure
> ingest-pipeline testing, run it on the laptop and point the backend at
> `localhost`. Reach for a Pi only when you want the real over-the-network
> path (a separate box broadcasting → laptop receiving over Wi-Fi).

Pick a transport to match the wire path you want to test:

```bash
python3 nmea0183_simulator.py --transport tcp --port 10110     # Expedition / OpenCPN style (app connects to the Pi)
python3 nmea0183_simulator.py --transport udp --port 10110     # MFD-WiFi style (app listens for the broadcast)
python3 nmea0183_simulator.py --transport serial --serial-device /dev/ttyUSB0 --baud 4800   # bare 0183 bus (needs pyserial-asyncio)
```

It models the same deterministic boat as the shore-side
`scripts/signalk_simulator.py` (Newport → Bermuda, 6.5 kt, sinusoidal
wind) but emits real NMEA 0183 sentences — RMC, VHW, HDG, MWV, MWD, plus
XDR/ROT so the IMU-heel and gyro-yaw capabilities light up. Knobs:
`--bsp --tws --twd --bearing --hz`, and `--no-imu` / `--no-mwd` to test
degraded sensor sets.

Point the laptop's backend at it — and **tag the run as synthetic** so
the fake samples are distinguishable from real instrument data in the DB:

```bash
INGEST_SOURCE=synthetic NMEA0183_URL=tcp://simpi.local:10110 \
  python -m uvicorn backend.main:app --port 8015
```

`INGEST_SOURCE=synthetic` stamps every persisted `InstrumentSample.source`
so a sim session can't be mistaken for (or pollute) a real log. Without
it the source reflects the wire format (`nmea0183` / `signalk`). The app
itself behaves identically either way — the sentences are real, so this
is a faithful end-to-end test, not a mock.

> Want the **full boat topology** (Pi → signalk-server → app) instead of
> feeding the app directly? Run the simulator with `--transport tcp`,
> add it as an "NMEA 0183" connection in signalk-server's admin UI, and
> leave the backend on `SIGNALK_URL` — same sentences, real relay path.

## Config interface — browser setup over a USB-C cable

For the bring-up phase — figuring out how the boat connects, which password, which channel is live — full-headless is awkward. `scripts/pi/config_server.py` is a small browser UI (installed + enabled by `setup_pi.sh` as `sailingrace-config.service`) that wraps the WiFi-join and discovery tools: scan/join the boat WiFi, run signal discovery, see what's live, and wire the relay — all from a page. Once configured, the Pi runs headless; this is only for setup.

It binds `0.0.0.0:8080`, so it's reachable however you connect:

| Connect via | Reach the UI at |
|---|---|
| **USB-C cable from the laptop** (recommended) | `http://10.55.0.1:8080` |
| Ethernet | `http://<pi-hostname>.local:8080` |
| The Pi's WiFi AP | `http://10.42.0.1:8080` (or `.local`) |

**The USB-C one-cable trick.** The Pi 4's USB-C port is power-in **and** a USB 2.0 OTG (gadget) port. `setup_pi.sh` enables gadget mode (`dtoverlay=dwc2,dr_mode=peripheral` in `config.txt`, `modules-load=dwc2,g_ether` in `cmdline.txt`, applied on the next reboot) and gives `usb0` a fixed address with DHCP (NetworkManager *shared*, `10.55.0.1/24`). So a single USB-C cable from your laptop to the Pi:

- **powers the Pi** (no separate supply at the bench — the UPS HAT isn't needed for setup), and
- **appears as a USB-ethernet adapter** on the laptop, which gets a `10.55.0.x` lease automatically.

Plug in → browse to `http://10.55.0.1:8080`. No WiFi, no router, no admin UI.

> **Power note:** use the laptop's **USB-C** port if you can — it supplies enough for a Pi 4 at config-time load. A plain USB-A port (especially with the PiCAN-M HAT attached) is marginal and may trip the undervoltage warning; data still works but stability doesn't.
>
> **Bench vs boat:** at the bench the laptop powers the Pi over USB-C; on the boat the UPS HAT powers that same port (power needs no data partner) and the laptop connects over ethernet or WiFi instead.

## Joining a password-protected boat WiFi

NMEA 2000 (`can0`) and bare 0183 (serial) have **no password** — tapping the wire is the access. But if the instrument data only reaches you over a **WiFi MFD network** or the boat WiFi that **Expedition's NMEA output** rides on, the Pi must *join that network as a client* before the `udp`/`tcp`/`signalk` discovery channels can see anything.

`scripts/pi/join_wifi.sh` wraps `nmcli` for this. **Credentials are stored by NetworkManager (root-only `/etc/NetworkManager/system-connections/`), never in the repo or bundle** — this is a public bundle, so no password is ever hard-coded.

```bash
scripts/pi/join_wifi.sh --scan                      # list visible networks
scripts/pi/join_wifi.sh "Lynx MFD" "the-password"   # join one
scripts/pi/join_wifi.sh "Lynx MFD" --try creds.txt  # try each password in a gitignored file
scripts/pi/join_wifi.sh --status                    # what am I on?
```

> Keep any candidates file out of git (`chmod 600 /home/pi/wifi_candidates.txt`). The `--try` mode is for when you're unsure of the exact variant.

**Single-radio constraint.** The built-in WiFi can be a *client* of the boat network **or** your laptop's *access point*, not both at once. Pick a topology:

| Topology | Pi built-in WiFi | Laptop reaches the Pi via | Trade-off |
|---|---|---|---|
| **A — shared WiFi** | client of the boat WiFi | the same boat WiFi | One radio; laptop also needs the boat password |
| **B — client + AP dongle** | boat client; **USB dongle** runs the AP (`--ifname wlan1` to join on the dongle, AP on wlan0) | Pi's own AP | Two radios; keeps a clean private net |
| **C — client + wired laptop** | client of the boat WiFi | **ethernet or USB cable** to the Pi | One radio, most reliable — recommended |

If the data is plain NMEA 0183 over that WiFi (no N2K), note the **laptop could read it directly** once it's joined — the Pi then adds discovery + logging + a stable relay rather than being strictly required. Tapping a spare **N2K backbone T-connector** avoids the WiFi password entirely and is worth checking for on the boat.

## Signal discovery

`scripts/discover_signals.py` turns the Pi into a headless signal-discovery box: plug a boat data source into *any* of its inputs and it works out what's live, classifies it, and (optionally) wires signalk-server to it — no admin UI, no prompts. It probes all channels at once:

| Channel | Source | How it's classified |
|---|---|---|
| `can0` | NMEA 2000 via PiCAN-M | raw SocketCAN → live PGN list |
| serial | NMEA 0183 via USB-RS422/RS232 | `/dev/ttyUSB*`/`ttyACM*` at 4800 & 38400 → talker IDs + sentences |
| `udp` | WiFi MFD broadcast (B&G/Garmin/Raymarine) | listens on 2000/10110/50000 |
| `tcp` | Expedition "NMEA Output" | localhost + gateway + `--host`, or `--sweep` the whole subnet |
| `signalk` | another SignalK server | HTTP discovery + WS path count |

Three modes:

```bash
# What's live right now? (ranked; add --sweep to scan the subnet for Expedition)
sudo .venv/bin/python scripts/discover_signals.py scan --sweep

# Record raw data + a classification summary, to work out a source's parse
# format offline before trusting it (this is the "use it first" workflow):
.venv/bin/python scripts/discover_signals.py capture --duration 120 --out-dir data/discovery
#   → data/discovery/<ts>_<channel>.log   (raw samples)
#   → data/discovery/<ts>_summary.json    (sentences/PGNs/paths + rates per channel)

# Wire the relay to the best live source, headlessly (the boat-day path):
sudo .venv/bin/python scripts/discover_signals.py apply --auto
#   or pin a specific one:  apply --source can0
```

`apply` is **safe headless**: it backs up `~/.signalk/settings.json`, writes the connection, restarts signalk-server, polls the SignalK API to confirm data is actually flowing, and **rolls the config back** if nothing arrives. It also disables any other connections so two sources can't double-feed the downstream estimators, and prints the `ws://…:3000` URL on each of the Pi's interfaces (so you know what to point the laptop at over wifi/eth/usb).

> Ranking favours richer feeds and, in a tie, sources carrying wind (MWV/MWD or N2K 130306/130577) — what routing needs most. `apply --auto` never picks a `signalk:` source as the relay input (that would be a loop).

## Wi-Fi access point — Pi as the boat network

When there's no boat Wi-Fi to join, make the Pi broadcast its own SSID so the laptop (and any tablets) connect straight to it. This is what turns the Pi into a self-contained "Wi-Fi server": instruments in over NMEA 2000, SignalK out over the Pi's own Wi-Fi, working with **zero internet** offshore. SailinGrace still runs on the laptop (Variant A2) — the AP just carries the SignalK stream from the Pi to it.

Two ways. Both target the NetworkManager default on Bookworm/Trixie — don't hand-roll `hostapd` + `dnsmasq` config files on these OSes, NetworkManager fights them.

### Option 1 — `nmcli` hotspot (no extra packages)

```bash
# Bring up an AP on the built-in Wi-Fi. NetworkManager runs DHCP + DNS
# internally (dnsmasq), handing the Pi 10.42.0.1 and leasing clients.
sudo nmcli device wifi hotspot ifname wlan0 ssid SailinGrace-01 password "<choose-one>"

# Make it come back on every boot:
sudo nmcli connection modify Hotspot connection.autoconnect yes
```

The laptop then joins SSID `SailinGrace-01` and points SailinGrace at:

```
ws://10.42.0.1:3000/signalk/v1/stream?subscribe=self
```

(`<pi>.local` mDNS also resolves once the laptop is on the AP.)

### Option 2 — RaspAP (browser admin UI)

If you want a web page to manage SSID / password / channel / clients:

```bash
curl -sL https://install.raspap.com | bash   # reboot when it finishes
```

Default AP: SSID `raspi-webgui`, gateway `10.3.141.1`, admin at `http://10.3.141.1` (login `admin` / `secret` — **change both**). SignalK URL becomes `ws://10.3.141.1:3000/...`.

### The dual-network catch (important)

If the laptop joins the Pi's AP for instruments, that Wi-Fi radio is now busy and the AP has **no internet** — but SailinGrace still needs internet for GRIB / weather fetch. Don't expect one Wi-Fi interface to do both. Give the laptop two paths: Wi-Fi → Pi AP for NMEA, and ethernet (or a second USB Wi-Fi adapter / phone tether) → Starlink/cell for weather. This is the same dual-network setup described in [deployment.md](https://github.com/atrainathought/SailinGrace/blob/master/docs/deployment.md#dual-network-reality-starlink--boat-local-nmea).

> The Pi only needs internet **once**, during `setup_pi.sh` (npm pulls signalk-server). After that it runs the relay fully offline — so set the AP up after software bring-up, or bring it up on ethernet first, install, then switch to AP mode.

## Risks / gotchas

- **SD card wear** — SQLite WAL writes constantly when SignalK ingest is on. Mitigations: A2-rated card; mount `/data` with `noatime`; USB SSD if the boat will be on >1 month at a stretch.
- **PiCAN-M oscillator** — the most common bring-up failure. PiCAN-M is 16 MHz; clones can be 8 MHz. Wrong value = `can0` comes up clean but receives nothing. Check the silkscreen.
- **N2K termination** — backbone must have exactly two 120 Ω terminators. PiCAN-M's on-board jumper should be **OFF** for normal backbone use; ON is only for bench testing the Pi standalone.
- **Conformal coating** on both HATs before mounting. Salt + humidity will eat unprotected boards in a season.
- **Power input** — feed the UPS HAT's USB-C input, not the Pi's. Pi's own power port stays on the *output* side of the UPS.
- **Pi 3B+ vs Pi 4** — micro-USB on the 3B+ is mechanically weaker. Hot-glue or strain-relief on the micro-USB plug.
- **Network surface** — when the Pi is the AP, it's exposing FastAPI on 8000 with no auth. Acceptable on a private boat network; do not run it on a public Wi-Fi.

## Uninstall

`scripts/uninstall.sh` is the inverse of `setup_pi.sh`. Dry-run by default:

```bash
sudo bash scripts/uninstall.sh                        # preview
sudo bash scripts/uninstall.sh --confirm              # actually remove
sudo bash scripts/uninstall.sh --confirm --archive-logs   # copy logs + DB to /tmp first
sudo bash scripts/uninstall.sh --confirm --remove-repo    # nuke the clone too
```

Removes SailinGrace systemd units (sailingrace, sailingrace-logger, ups-monitor) + runtime config overrides. Leaves signalk-server, can-utils, Node.js, system Python, user venvs untouched — those predate SailinGrace and may be used by other tools.

## Tested-on / known-working

- Pi 4B 4 GB, Bookworm 64-bit, PiCAN-M, Waveshare UPS HAT (D), signalk-server 2.x → bench-validated. Not yet validated against a real NMEA 2000 backbone.
- Pi 400, Trixie 64-bit Lite, PiCAN-M (UPS HAT held off — Pi 400 form factor doesn't stack cleanly), signalk-server 2.x → fully bench-validated end-to-end 2026-05-25: can0 UP, signalk-server with Allow Readonly on, SailinGrace on laptop subscribed via `ws://trainapi4.local:3000/...`, `/api/sensors/capabilities` reports `available: true, source: "signalk"`.
