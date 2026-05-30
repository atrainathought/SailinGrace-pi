#!/bin/bash
# scripts/setup_pi.sh — bring up a Pi for SailinGrace's Path A (SignalK ingest)
#
# Run on a fresh Bookworm or Trixie Pi *after* you have:
#   - SSH working
#   - Wi-Fi or Ethernet working
#
# What this does (idempotent — safe to re-run):
#   1. Enables SPI + I2C
#   2. Adds PiCAN-M MCP2515 device-tree overlay to /boot/firmware/config.txt
#   3. Brings up can0 at 250 kbps via systemd-networkd
#   4. Installs can-utils, i2c-tools, smbus2, Node.js LTS, signalk-server
#   5. Installs the Waveshare UPS HAT (D) monitor systemd unit (only when
#      the script is run from inside a SailinGrace repo clone that has
#      scripts/systemd/ups-monitor.service)
#
# What this does NOT do (do these manually):
#   - Hostname / Wi-Fi (use the Pi Imager customize step, or raspi-config)
#   - signalk-server first-time bootstrap (run `signalk-server-setup` once
#     to pick port + run-as-user)
#   - signalk-server can0 connection (configure once via the admin web UI
#     at http://<pi>:3000/admin)
#
# Usage — from inside a SailinGrace clone on the Pi:
#   bash scripts/setup_pi.sh
#
# Usage — one-shot from a fresh Pi without cloning:
#   curl -fsSL https://raw.githubusercontent.com/atrainathought/SailinGrace/master/scripts/setup_pi.sh | bash
#   (the UPS monitor pieces will be skipped — clone the repo to get them)

set -euo pipefail

# ── helpers ──────────────────────────────────────────────────────────

log()  { printf "\033[1;34m==>\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m!! \033[0m %s\n" "$*" >&2; }

# Append a heredoc block to a file only if a marker line is not already
# there. Lets the script be re-run without duplicating config lines.
# Usage:
#   append_if_missing /path/to/file "# unique marker string" <<'EOF'
#   ...block...
#   EOF
append_if_missing() {
  local file="$1" marker="$2"
  local block
  block=$(cat)
  if [ -f "$file" ] && grep -qF "$marker" "$file"; then
    log "block already in $file — skipping"
  else
    log "appending block to $file"
    printf "%s\n" "$block" | sudo tee -a "$file" > /dev/null
  fi
}

# ── 1. SPI + I2C ─────────────────────────────────────────────────────

log "enabling SPI + I2C via raspi-config"
sudo raspi-config nonint do_spi 0
sudo raspi-config nonint do_i2c 0

# ── 2. PiCAN-M MCP2515 device-tree overlay ───────────────────────────

# 16 MHz oscillator is the PiCAN-M default. If you have a clone board
# with an 8 MHz crystal, change oscillator=8000000 here and re-run.
append_if_missing /boot/firmware/config.txt "# PiCAN-M (NMEA 2000 HAT)" <<'EOF'

# PiCAN-M (NMEA 2000 HAT) - 16 MHz oscillator, IRQ on GPIO 25
dtparam=spi=on
dtparam=i2c_arm=on
dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=25
dtoverlay=spi-bcm2835-overlay
EOF

# ── 3. can0 persistence via systemd-networkd ─────────────────────────

if [ ! -f /etc/systemd/network/80-can.network ]; then
  log "writing /etc/systemd/network/80-can.network"
  sudo tee /etc/systemd/network/80-can.network > /dev/null <<'EOF'
[Match]
Name=can0

[CAN]
BitRate=250000
EOF
else
  log "/etc/systemd/network/80-can.network already exists — skipping"
fi

# systemd-networkd may already be running; --now is a no-op then
sudo systemctl enable --now systemd-networkd

# ── 4. User-space tools ──────────────────────────────────────────────

log "installing can-utils + i2c-tools + python helpers"
sudo apt update
sudo apt install -y can-utils i2c-tools python3-smbus python3-venv git

# ── 5. Node.js LTS + signalk-server ──────────────────────────────────

if ! command -v node >/dev/null 2>&1; then
  log "installing Node.js LTS (NodeSource)"
  curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo bash -
  sudo apt install -y nodejs
else
  log "Node.js already installed ($(node --version)) — skipping"
fi

if ! command -v signalk-server >/dev/null 2>&1; then
  log "installing signalk-server (npm global; takes 2-3 min on a Pi)"
  sudo npm install -g signalk-server
else
  log "signalk-server already installed — skipping"
fi

# ── 6. Python venv for Pi-side services (repo clone only) ────────────
# Both the UPS monitor and the SignalK logger run from $REPO/.venv. The
# curl one-shot has no clone, so these steps are skipped there — re-run
# from a clone to get them.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
HAVE_REPO=0

# Copy a systemd unit into place, rewriting the hard-coded
# /home/pi/SailinGrace install path to wherever this repo/bundle actually
# lives. Lets the units work whether you cloned the full repo, unpacked a
# tarball, or cloned a standalone SailinGrace-pi repo to any path.
install_unit() {
  local src="$1" dest="/etc/systemd/system/$(basename "$1")"
  sed "s#/home/pi/SailinGrace#${REPO_ROOT}#g" "$src" | sudo tee "$dest" >/dev/null
}
if [ -f "$REPO_ROOT/scripts/capture_signalk.py" ]; then
  HAVE_REPO=1
  if [ ! -d "$REPO_ROOT/.venv" ]; then
    log "creating Python venv at $REPO_ROOT/.venv"
    python3 -m venv "$REPO_ROOT/.venv"
  else
    log "venv already exists at $REPO_ROOT/.venv — skipping create"
  fi
  log "installing Pi-side Python deps (websockets, smbus2, pyserial)"
  "$REPO_ROOT/.venv/bin/pip" install --quiet --upgrade pip
  # websockets: logger + signalk discovery; smbus2: UPS monitor;
  # pyserial: serial-channel signal discovery. N2K discovery uses raw
  # SocketCAN (stdlib), so no python-can needed.
  "$REPO_ROOT/.venv/bin/pip" install --quiet websockets smbus2 pyserial
else
  warn "not running from a repo clone — skipping venv + Pi-side services"
  warn "→ clone the repo and re-run to install the UPS monitor + logger"
fi

# ── 7. UPS monitor (repo clone only) ─────────────────────────────────
if [ "$HAVE_REPO" = 1 ] \
   && [ -f "$SCRIPT_DIR/systemd/ups-monitor.service" ] \
   && [ -f "$SCRIPT_DIR/ups_monitor.py" ]; then
  if [ ! -f /etc/systemd/system/ups-monitor.service ]; then
    log "installing UPS monitor systemd unit"
    install_unit "$SCRIPT_DIR/systemd/ups-monitor.service"
    sudo systemctl daemon-reload
    log "→ enable later with: sudo systemctl enable --now ups-monitor"
    log "→ only enable when the Waveshare UPS HAT (D) is actually connected"
  else
    log "ups-monitor.service already installed — skipping"
  fi
fi

# ── 8. Pi-side SignalK delta logger (enabled — part of the install) ──
# Captures raw deltas to disk for the post-trip pipeline-adjustment loop.
# Self-limiting on disk (KEEP_DAYS age / MAX_LOG_MB size / MIN_FREE_MB
# free floor — see scripts/pi/log_signalk.sh) so it can never fill the
# SD card and take the Pi down. This is the relay Pi — the logger is NOT
# the SailinGrace app, just a disk capture; SailinGrace runs on a laptop.
if [ "$HAVE_REPO" = 1 ] \
   && [ -f "$SCRIPT_DIR/pi/sailingrace-logger.service" ] \
   && [ -f "$SCRIPT_DIR/pi/log_signalk.sh" ]; then
  log "installing + enabling the raw SignalK delta logger"
  chmod +x "$SCRIPT_DIR/pi/log_signalk.sh"
  install_unit "$SCRIPT_DIR/pi/sailingrace-logger.service"
  sudo systemctl daemon-reload
  # enable --now; it harmlessly retries connecting until signalk-server
  # is bootstrapped (manual step below). A failure here is non-fatal —
  # the unit is enabled and will come up on the post-setup reboot.
  sudo systemctl enable --now sailingrace-logger \
    || warn "logger didn't start yet (signalk-server not bootstrapped?) — enabled for next boot"
  log "→ logs in /home/pi/sailingrace-logs/signalk_<date>.log"
  log "→ disk-safe: 14d age cap, 4000MB size cap, 1000MB free floor (tune in the unit)"
  log "→ feeds the post-trip pipeline-adjustment loop (PROJECT.md #15-19)"
fi

# ── done ─────────────────────────────────────────────────────────────

cat <<'EOF'

──────────────────────────────────────────────────────────────────────
Setup complete. Next steps (do these manually once):

  1. If this is the first time you ran the script on this SD card,
     REBOOT so the MCP2515 overlay loads:
       sudo reboot

  2. After reboot, verify hardware:
       ip link show can0          # expect state UP
       sudo i2cdetect -y 1        # expect 42 if Waveshare UPS HAT (D)
                                  # is stacked on the GPIO header

  3. First-time signalk-server bootstrap (interactive, ~2 min):
       signalk-server-setup
       # Answers: port 80? NO (keep default 3000). SSL? NO (self-signed
       # cert friction, threat model on a private boat net doesn't
       # justify it). Install as system service via sudo? YES.

  4. Verify the service is up:
       sudo systemctl status signalk.service
       # "Could not parse security config" warnings in the log are
       # NORMAL until step 5 creates security.json. Ignore them.

  5. From your laptop, open the admin UI and create the admin user:
       http://<this-pi>.local:3000/admin
       # First visit prompts for admin username/password. Pick any.
       # This writes ~/.signalk/security.json on the Pi.

  6. In the admin UI, add the can0 connection:
       Server → Connections → Add
         Provider ID: nmea2000      (just a label — pick anything unique)
         Type:        NMEA 2000
         Source:      canboat-js
         Interface:   can0
       Activate. Dashboard "Deltas/sec" should climb if N2K bus is
       connected (zero on the bench is fine).

  7. Allow SailinGrace's WebSocket subscription to read without a
     token — easiest with the global toggle:
       Security → Settings → Allow Readonly: ON → Save
     Verify:
       curl -s http://localhost:3000/signalk/v1/api/vessels/self | head
       # Should return JSON, not "Unauthorized".

  8. On your laptop (NOT the Pi), launch SailinGrace pointed at the Pi:
       SIGNALK_URL=ws://<this-pi>.local:3000/signalk/v1/stream?subscribe=self \
         python -m uvicorn backend.main:app --port 8000
       Then browse http://localhost:8000

  NOTE: The raw SignalK delta logger is already enabled and will start
  capturing once signalk-server is up (steps 3-7). It is disk-safe — it
  caps its own size and stops before the SD card fills, so it will not
  crash the Pi. Watch it with:  journalctl -u sailingrace-logger -f

  SIGNAL DISCOVERY: instead of wiring the can0/serial/TCP/UDP source by
  hand in the admin UI (steps 6), you can let the Pi find and configure
  the live source headlessly:
       sudo .venv/bin/python scripts/discover_signals.py scan          # see what's live
       sudo .venv/bin/python scripts/discover_signals.py capture       # record raw for parsing
       sudo .venv/bin/python scripts/discover_signals.py apply --auto   # wire the relay to the best one

──────────────────────────────────────────────────────────────────────
EOF
