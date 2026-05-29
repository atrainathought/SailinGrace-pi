#!/usr/bin/env bash
#
# Continuous SignalK capture for the Pi+PiCAN-M kit.
#
# Runs scripts/capture_signalk.py against the local signalk-server,
# rotating to a new file at UTC midnight. Lets the Pi log raw deltas
# whether the laptop is connected or not — survives reboots via the
# sister systemd unit (scripts/pi/sailingrace-logger.service).
#
# Output: $LOG_DIR/signalk_<UTC-date>.log, one timestamped delta per
# line (newline-delimited JSON).
#
# SD-card safety: the logger is bounded three ways so it can never fill
# the card and take the Pi down with it —
#   1. age cap     — files older than KEEP_DAYS are deleted
#   2. size budget — oldest files deleted until the log dir is <= MAX_LOG_MB
#   3. free floor  — if filesystem free space drops below MIN_FREE_MB the
#                    logger prunes its own logs and, if that's not enough
#                    (something *else* filled the card), PAUSES writing
#                    instead of consuming the last free bytes.
# These run every GUARD_INTERVAL_S, not just at startup, so a long
# passage without a reboot stays bounded. capture_signalk.py opens the
# daily file in append mode, so chunked capture keeps one file per UTC
# day with only a ~1-2 s reconnect gap at each guard interval.
#
# Env vars (override on the systemd unit if needed):
#   LOG_DIR          target directory (default: /home/pi/sailingrace-logs)
#   SIGNALK_URL      default: ws://localhost:3000/signalk/v1/stream?subscribe=all
#   KEEP_DAYS        age cap in days (default: 14)
#   MAX_LOG_MB       total budget for signalk_*.log (default: 4000)
#   MIN_FREE_MB      filesystem free-space floor (default: 1000)
#   GUARD_INTERVAL_S prune/disk-check + capture-chunk seconds (default: 900)
#   SG_REPO          repo root (default: /home/pi/SailinGrace)

set -euo pipefail

LOG_DIR=${LOG_DIR:-/home/pi/sailingrace-logs}
SIGNALK_URL=${SIGNALK_URL:-ws://localhost:3000/signalk/v1/stream?subscribe=all}
KEEP_DAYS=${KEEP_DAYS:-14}
MAX_LOG_MB=${MAX_LOG_MB:-4000}
MIN_FREE_MB=${MIN_FREE_MB:-1000}
GUARD_INTERVAL_S=${GUARD_INTERVAL_S:-900}
SG_REPO=${SG_REPO:-/home/pi/SailinGrace}

mkdir -p "$LOG_DIR"

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log()   { echo "[$(stamp)] $*"; }
warn()  { echo "[$(stamp)] !! $*" >&2; }

# MB currently used by our log files (0 when none exist yet). The
# trailing `|| true` neutralises pipefail when the glob matches nothing.
log_dir_mb() {
  local n
  n=$(du -cm "$LOG_DIR"/signalk_*.log 2>/dev/null | tail -1 | cut -f1 || true)
  echo "${n:-0}"
}

# Available MB on the filesystem holding LOG_DIR.
free_mb() {
  df -Pm "$LOG_DIR" | tail -1 | awk '{print $4}'
}

# Oldest log file by mtime, or empty string if none.
oldest_log() {
  ls -1tr "$LOG_DIR"/signalk_*.log 2>/dev/null | head -1 || true
}

log_count() {
  ls -1 "$LOG_DIR"/signalk_*.log 2>/dev/null | wc -l
}

# Age cap + size budget. Never deletes the last remaining file here —
# that's reserved for the emergency free-floor path below.
prune() {
  find "$LOG_DIR" -maxdepth 1 -name 'signalk_*.log' -type f \
      -mtime "+$KEEP_DAYS" -delete 2>/dev/null || true
  while [ "$(log_dir_mb)" -gt "$MAX_LOG_MB" ] && [ "$(log_count)" -gt 1 ]; do
    local oldest; oldest=$(oldest_log)
    [ -z "$oldest" ] && break
    warn "size budget ${MAX_LOG_MB}MB exceeded ($(log_dir_mb)MB) — removing $oldest"
    rm -f "$oldest"
  done
}

# Returns 0 if the free-space floor is satisfied, 1 if not. Prunes our
# own logs (oldest first, including the current file as a last resort)
# trying to get back above the floor; if it still can't, something other
# than us filled the card and the caller pauses writing.
ensure_free() {
  prune
  while [ "$(free_mb)" -lt "$MIN_FREE_MB" ]; do
    local oldest; oldest=$(oldest_log)
    [ -z "$oldest" ] && break
    warn "free space $(free_mb)MB < ${MIN_FREE_MB}MB floor — removing $oldest"
    rm -f "$oldest"
  done
  [ "$(free_mb)" -ge "$MIN_FREE_MB" ]
}

# capture_signalk.py exits on Ctrl-C / SIGTERM, so we trap and exit
# cleanly when systemd stops the unit.
trap 'log "shutting down"; exit 0' SIGTERM SIGINT

while true; do
  if ! ensure_free; then
    warn "disk still below ${MIN_FREE_MB}MB free after pruning our logs"
    warn "→ pausing capture ${GUARD_INTERVAL_S}s (the card is filling from something other than this logger)"
    sleep "$GUARD_INTERVAL_S"
    continue
  fi

  TODAY=$(date -u +%Y-%m-%d)
  OUT="$LOG_DIR/signalk_${TODAY}.log"

  # Capture for the guard interval, but never past UTC midnight so each
  # file stays a single day. Whichever comes first bounds this chunk.
  NOW_SEC=$(date -u +%s)
  MIDNIGHT_SEC=$(date -u -d "${TODAY} 23:59:59" +%s)
  TO_MIDNIGHT=$(( MIDNIGHT_SEC - NOW_SEC + 1 ))
  CHUNK=$GUARD_INTERVAL_S
  [ "$TO_MIDNIGHT" -lt "$CHUNK" ] && CHUNK=$TO_MIDNIGHT
  [ "$CHUNK" -lt 1 ] && CHUNK=1

  log "-> $OUT (chunk ${CHUNK}s; ${MAX_LOG_MB}MB cap, ${MIN_FREE_MB}MB free floor, $(free_mb)MB free)"
  # `timeout` SIGINTs the python process at the chunk boundary; exit
  # code 124 means "timeout reached" which is the happy path here.
  timeout --signal=SIGINT "${CHUNK}s" \
    "$SG_REPO/.venv/bin/python" \
    "$SG_REPO/scripts/capture_signalk.py" \
    "$SIGNALK_URL" \
    --out "$OUT" || true
  # If systemd stopped us mid-chunk, the trap above already exited 0.
  # Otherwise loop: re-check disk, then continue appending to today's
  # file (or roll to the new day's file after midnight).
done
