#!/usr/bin/env python3
"""Signal discovery for the boat-side Pi — find which input is live, then
optionally wire signalk-server to it. Fully headless: no admin UI, no
interactive prompts.

The Pi can receive boat data many ways at once:

    can0        NMEA 2000 via PiCAN-M (SocketCAN)
    serial      NMEA 0183 via a USB-RS422/RS232 adapter
    udp         NMEA 0183 broadcast from a WiFi MFD (B&G/Garmin/Raymarine)
    tcp         NMEA 0183 from Expedition's "NMEA Output" (TCP server)
    signalk     another SignalK server on the network

This tool probes all of them, classifies what's actually flowing
(talker IDs + sentence types for 0183; PGNs for N2K), ranks the live
sources, and prints a report. It has three modes:

    scan       (default) probe everything, print the ranked report
    capture    probe + record raw logs and per-channel summaries to disk,
               so you can take them offline and build/verify parsers
    apply      pick the best live source (or --source X) and configure
               signalk-server to ingest it, restart, and VERIFY data flows
               (rolls back the config if it doesn't). `scan --auto` is an
               alias for `apply` with the auto-picked source.

Examples:
    sudo python3 scripts/discover_signals.py scan
    python3 scripts/discover_signals.py capture --duration 120 --out-dir data/discovery
    sudo python3 scripts/discover_signals.py apply --auto
    sudo python3 scripts/discover_signals.py apply --source can0

Standalone: stdlib only, with optional `pyserial` (serial channel) and
`websockets` (SignalK WS read). Missing optional deps degrade that one
channel gracefully; everything else still runs. SocketCAN + subnet
detection are Linux-only (the Pi); on other OSes those channels skip.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import glob
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ── optional deps (degrade gracefully) ───────────────────────────────
try:
    import serial  # pyserial
    from serial.tools import list_ports as _serial_list_ports
except ImportError:
    serial = None
    _serial_list_ports = None

# websockets is only needed to *read* a SignalK stream; HTTP probe works
# without it.
try:
    import asyncio
    import websockets  # noqa: F401
    _HAVE_WS = True
except ImportError:
    _HAVE_WS = False


# Default ports we look for NMEA 0183 / SignalK on.
NMEA_TCP_PORTS = (10110, 10111, 2000, 50000)
NMEA_UDP_PORTS = (2000, 10110, 50000)
SIGNALK_PORTS = (3000,)
DEFAULT_BAUDS = (4800, 38400)
CAN_IFACE = "can0"
SIGNALK_SETTINGS = Path(os.environ.get("SIGNALK_NODE_SETTINGS",
                        str(Path.home() / ".signalk" / "settings.json")))

# Start of an NMEA 0183 line: $TTSSS, or !TTSSS, (talker + sentence id).
_NMEA_RE = re.compile(rb"^[!$]([A-Z]{2})([A-Z]{3}),")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── result model ──────────────────────────────────────────────────────
@dataclass
class Probe:
    """Outcome of probing one channel."""
    channel: str               # "can0" | "ttyUSB0@4800" | "udp:2000" | "tcp:192.168.1.40:10110" | "signalk:host:3000"
    transport: str             # can | serial | udp | tcp | signalk
    protocol: str = ""         # NMEA2000 | NMEA0183 | SignalK | ""
    live: bool = False
    rate_hz: float = 0.0       # messages/sentences per second observed
    detail: dict = field(default_factory=dict)   # {"sentences": {...}} or {"pgns": {...}} or {"paths": {...}}
    samples: list = field(default_factory=list)   # a few raw lines for the report / format work
    error: str = ""

    def score(self) -> float:
        """Rank live sources: protocol richness × rate. N2K and fused 0183
        (TWD/MWD present) rank above bare position-only feeds."""
        if not self.live:
            return 0.0
        variety = len(self.detail.get("sentences") or self.detail.get("pgns")
                      or self.detail.get("paths") or {})
        base = variety * 10 + min(self.rate_hz, 50)
        # Prefer sources that carry wind, the thing routing most needs.
        keys = " ".join((self.detail.get("sentences") or {}).keys()) \
            + " ".join(str(p) for p in (self.detail.get("pgns") or {}).keys())
        if any(w in keys for w in ("MWV", "MWD", "MDA", "130306", "130577")):
            base += 25
        return base

    def summary(self) -> str:
        if self.error:
            return f"  {self.channel:28} {self.transport:8} ERROR: {self.error}"
        if not self.live:
            return f"  {self.channel:28} {self.transport:8} {'--':8} no data"
        d = self.detail.get("sentences") or self.detail.get("pgns") or self.detail.get("paths") or {}
        top = ",".join(list(d.keys())[:6])
        return (f"  {self.channel:28} {self.transport:8} {self.protocol:8} "
                f"{self.rate_hz:5.1f}/s  [{len(d)}] {top}")


def classify_nmea0183(lines: list[bytes]) -> dict:
    """Count NMEA 0183 sentence types (talker-agnostic) from raw lines."""
    sentences: dict[str, int] = {}
    talkers: dict[str, int] = {}
    for ln in lines:
        m = _NMEA_RE.match(ln.strip())
        if not m:
            continue
        talkers[m.group(1).decode()] = talkers.get(m.group(1).decode(), 0) + 1
        s = m.group(2).decode()
        sentences[s] = sentences.get(s, 0) + 1
    return {"sentences": dict(sorted(sentences.items(), key=lambda kv: -kv[1])),
            "talkers": dict(sorted(talkers.items(), key=lambda kv: -kv[1]))}


def pgn_from_can_id(can_id: int) -> int:
    """Extract the NMEA 2000 / J1939 PGN from a 29-bit extended CAN id."""
    dp = (can_id >> 24) & 1          # data page
    pf = (can_id >> 16) & 0xFF       # PDU format
    ps = (can_id >> 8) & 0xFF        # PDU specific
    if pf < 240:                     # PDU1: ps is a destination address
        return (dp << 16) | (pf << 8)
    return (dp << 16) | (pf << 8) | ps   # PDU2: ps is part of the PGN


# ── channel probers ───────────────────────────────────────────────────
def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return p.returncode, (p.stdout + p.stderr)
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


def probe_can(duration: float, iface: str = CAN_IFACE) -> Probe:
    pr = Probe(channel=iface, transport="can", protocol="NMEA2000")
    if not sys.platform.startswith("linux") or not hasattr(socket, "AF_CAN"):
        pr.error = "SocketCAN not available (not Linux?)"
        return pr
    # Make sure the interface exists and is up (250 kbps is the N2K standard).
    rc, out = _run(["ip", "-details", "link", "show", iface])
    if rc != 0:
        pr.error = f"{iface} not present"
        return pr
    if "state UP" not in out and "UP," not in out:
        _run(["sudo", "ip", "link", "set", iface, "up", "type", "can", "bitrate", "250000"])
    try:
        s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        s.bind((iface,))
        s.settimeout(0.5)
    except OSError as e:
        pr.error = f"bind {iface}: {e}"
        return pr
    pgns: dict[int, int] = {}
    n = 0
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        try:
            frame = s.recv(16)
        except socket.timeout:
            continue
        except OSError:
            break
        if len(frame) < 8:
            continue
        can_id = struct.unpack("<I", frame[:4])[0] & socket.CAN_EFF_MASK
        pgn = pgn_from_can_id(can_id)
        pgns[pgn] = pgns.get(pgn, 0) + 1
        n += 1
        if len(pr.samples) < 8:
            pr.samples.append(f"PGN {pgn} (id={can_id:08X})")
    s.close()
    pr.live = n > 0
    pr.rate_hz = n / max(duration, 0.1)
    pr.detail = {"pgns": dict(sorted(pgns.items(), key=lambda kv: -kv[1]))}
    return pr


def list_serial_devices() -> list[str]:
    devs: list[str] = []
    if _serial_list_ports is not None:
        devs = [p.device for p in _serial_list_ports.comports()]
    if not devs:  # fallback glob (and stable by-id symlinks first)
        devs = sorted(glob.glob("/dev/serial/by-id/*")) \
            or sorted(glob.glob("/dev/ttyUSB*")) + sorted(glob.glob("/dev/ttyACM*"))
    return devs


def probe_serial(duration: float, devices: list[str], bauds: tuple[int, ...]) -> list[Probe]:
    out: list[Probe] = []
    if serial is None:
        p = Probe(channel="serial", transport="serial")
        p.error = "pyserial not installed (pip install pyserial)"
        return [p]
    for dev in devices:
        for baud in bauds:
            pr = Probe(channel=f"{dev}@{baud}", transport="serial", protocol="NMEA0183")
            lines: list[bytes] = []
            try:
                with serial.Serial(dev, baud, timeout=0.5) as ser:
                    deadline = time.monotonic() + duration / len(bauds)
                    while time.monotonic() < deadline:
                        ln = ser.readline()
                        if ln:
                            lines.append(ln)
            except Exception as e:  # noqa: BLE001
                pr.error = str(e)
                out.append(pr)
                continue
            cls = classify_nmea0183(lines)
            pr.live = bool(cls["sentences"])
            pr.rate_hz = len([l for l in lines if _NMEA_RE.match(l.strip())]) / max(duration / len(bauds), 0.1)
            pr.detail = cls
            pr.samples = [l.decode(errors="replace").strip() for l in lines[:8]]
            out.append(pr)
            if pr.live:        # found the right baud — don't try the others
                break
    return out


def probe_udp(duration: float, ports: tuple[int, ...]) -> list[Probe]:
    out: list[Probe] = []
    socks = []
    for port in ports:
        pr = Probe(channel=f"udp:{port}", transport="udp", protocol="NMEA0183")
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", port))
            s.setblocking(False)
            socks.append((port, s, pr))
        except OSError as e:
            pr.error = str(e)
        out.append(pr)
    if not socks:
        return out
    import select
    buf: dict[int, list[bytes]] = {p: [] for p, _, _ in socks}
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        ready, _, _ = select.select([s for _, s, _ in socks], [], [], 0.5)
        for s in ready:
            try:
                data, _ = s.recvfrom(4096)
            except OSError:
                continue
            for port, sk, _ in socks:
                if sk is s:
                    buf[port].extend(data.splitlines())
    for port, s, pr in socks:
        s.close()
        cls = classify_nmea0183(buf[port])
        pr.live = bool(cls["sentences"])
        pr.rate_hz = sum(cls["sentences"].values()) / max(duration, 0.1)
        pr.detail = cls
        pr.samples = [l.decode(errors="replace").strip() for l in buf[port][:8]]
    return out


def local_ipv4_subnets() -> list[str]:
    """Return '/24' network prefixes (e.g. '192.168.1.') for each non-loopback
    IPv4 the Pi holds — wlan0, eth0, usb0, etc."""
    nets: list[str] = []
    rc, out = _run(["ip", "-4", "-o", "addr", "show"])
    if rc == 0:
        for line in out.splitlines():
            m = re.search(r"inet (\d+\.\d+\.\d+)\.\d+/", line)
            if m and not line.split()[1].startswith("lo"):
                pref = m.group(1) + "."
                if pref not in nets:
                    nets.append(pref)
    return nets


def default_gateway() -> str | None:
    rc, out = _run(["ip", "route", "show", "default"])
    if rc == 0:
        m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    return None


def _port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _read_tcp(host: str, port: int, duration: float) -> list[bytes]:
    lines: list[bytes] = []
    try:
        with socket.create_connection((host, port), timeout=1.0) as s:
            s.settimeout(0.5)
            deadline = time.monotonic() + duration
            buf = b""
            while time.monotonic() < deadline:
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                parts = buf.split(b"\n")
                buf = parts.pop()
                lines.extend(parts)
    except OSError:
        pass
    return lines


def probe_tcp_sweep(duration: float, hosts: list[str], ports: tuple[int, ...],
                    sweep: bool, extra_hosts: list[str]) -> list[Probe]:
    """Find NMEA-0183-over-TCP sources. Builds a target host list (localhost,
    gateway, hinted hosts, and — if sweep — every host on each local /24),
    fast-scans the ports, then classifies the ones that answer."""
    targets = {"127.0.0.1"}
    gw = default_gateway()
    if gw:
        targets.add(gw)
    targets.update(extra_hosts)
    if sweep:
        for pref in local_ipv4_subnets():
            targets.update(f"{pref}{i}" for i in range(1, 255))
    # Fast open-port scan across (host, port) pairs.
    pairs = [(h, p) for h in targets for p in ports]
    open_pairs: list[tuple[str, int]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        futs = {ex.submit(_port_open, h, p): (h, p) for h, p in pairs}
        for fut in concurrent.futures.as_completed(futs):
            if fut.result():
                open_pairs.append(futs[fut])
    out: list[Probe] = []
    for host, port in open_pairs:
        pr = Probe(channel=f"tcp:{host}:{port}", transport="tcp", protocol="NMEA0183")
        lines = _read_tcp(host, port, duration)
        cls = classify_nmea0183(lines)
        pr.live = bool(cls["sentences"])
        pr.rate_hz = sum(cls["sentences"].values()) / max(duration, 0.1)
        pr.detail = cls
        pr.samples = [l.decode(errors="replace").strip() for l in lines[:8]]
        if not pr.live and lines:
            pr.error = "open but not NMEA 0183"
        out.append(pr)
    return out


def probe_signalk(duration: float, hosts: list[str], extra_hosts: list[str]) -> list[Probe]:
    """Detect SignalK servers via their HTTP discovery endpoint; optionally
    read the WS delta stream to count paths."""
    targets = {"127.0.0.1", "localhost"}
    targets.update(extra_hosts)
    out: list[Probe] = []
    for host in targets:
        for port in SIGNALK_PORTS:
            url = f"http://{host}:{port}/signalk"
            try:
                import urllib.request
                with urllib.request.urlopen(url, timeout=1.0) as r:
                    info = json.loads(r.read().decode())
            except Exception:  # noqa: BLE001
                continue
            pr = Probe(channel=f"signalk:{host}:{port}", transport="signalk", protocol="SignalK")
            pr.detail = {"endpoints": list((info.get("endpoints") or {}).keys())}
            paths = _read_signalk_ws(host, port, duration) if _HAVE_WS else {}
            if paths:
                pr.detail["paths"] = paths
            pr.live = True
            pr.rate_hz = sum(paths.values()) / max(duration, 0.1) if paths else 0.0
            out.append(pr)
    return out


def _read_signalk_ws(host: str, port: int, duration: float) -> dict:
    if not _HAVE_WS:
        return {}
    paths: dict[str, int] = {}

    async def _read():
        url = f"ws://{host}:{port}/signalk/v1/stream?subscribe=self"
        try:
            async with websockets.connect(url, ping_interval=None, open_timeout=2) as ws:
                deadline = time.monotonic() + duration
                while time.monotonic() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    msg = json.loads(raw)
                    for u in msg.get("updates") or []:
                        for v in u.get("values") or []:
                            p = v.get("path", "")
                            if p:
                                paths[p] = paths.get(p, 0) + 1
        except Exception:  # noqa: BLE001
            pass

    try:
        asyncio.run(_read())
    except Exception:  # noqa: BLE001
        pass
    return dict(sorted(paths.items(), key=lambda kv: -kv[1]))


# ── orchestration ─────────────────────────────────────────────────────
def scan_all(duration: float, *, sweep: bool, extra_hosts: list[str],
             channels: set[str]) -> list[Probe]:
    results: list[Probe] = []
    if "can" in channels:
        results.append(probe_can(duration))
    if "serial" in channels:
        results.extend(probe_serial(duration, list_serial_devices(), DEFAULT_BAUDS))
    if "udp" in channels:
        results.extend(probe_udp(duration, NMEA_UDP_PORTS))
    if "tcp" in channels:
        results.extend(probe_tcp_sweep(duration, [], NMEA_TCP_PORTS, sweep, extra_hosts))
    if "signalk" in channels:
        results.extend(probe_signalk(duration, [], extra_hosts))
    results.sort(key=lambda p: p.score(), reverse=True)
    return results


def print_report(results: list[Probe]) -> None:
    live = [r for r in results if r.live]
    print(f"\n── signal discovery @ {now_iso()} ──")
    print(f"  probed {len(results)} channel(s), {len(live)} live\n")
    for r in results:
        print(r.summary())
    if live:
        best = live[0]
        print(f"\n  best live source: {best.channel}  ({best.protocol})")
        print("  apply it with:  discover_signals.py apply --source", best.channel)
    else:
        print("\n  no live sources found — check cabling, then re-run.")
    print()


# ── signalk-server config (headless apply) ────────────────────────────
def build_provider(pr: Probe) -> dict:
    """Translate a live Probe into a signalk-server pipedProviders entry.
    Uses the documented providers/simple shape."""
    pid = "sg-" + re.sub(r"[^a-zA-Z0-9]+", "-", pr.channel).strip("-")
    sub: dict
    if pr.transport == "can":
        otype, sub = "NMEA2000", {"type": "canbus-canboatjs", "interface": pr.channel}
    elif pr.transport == "serial":
        dev, _, baud = pr.channel.partition("@")
        otype, sub = "NMEA0183", {"type": "serial", "device": dev,
                                  "baudrate": int(baud or 4800), "validateChecksum": True}
    elif pr.transport == "udp":
        port = int(pr.channel.split(":")[1])
        otype, sub = "NMEA0183", {"type": "udp", "port": port}
    elif pr.transport == "tcp":
        _, host, port = pr.channel.split(":")
        otype, sub = "NMEA0183", {"type": "tcp", "host": host, "port": int(port)}
    else:
        raise ValueError(f"cannot relay transport {pr.transport} via a signalk provider")
    return {
        "id": pid,
        "enabled": True,
        "pipeElements": [{
            "type": "providers/simple",
            "options": {"logging": False, "type": otype, "subOptions": sub},
        }],
    }


def _signalk_restart() -> None:
    for unit in ("signalk.service", "signalk-server.service"):
        rc, _ = _run(["systemctl", "is-enabled", unit])
        if rc == 0:
            _run(["sudo", "systemctl", "restart", unit])
            return
    _run(["sudo", "systemctl", "restart", "signalk.service"])


def _signalk_has_data(timeout: float = 25.0) -> bool:
    """Poll the local SignalK REST API until vessels.self has any real value."""
    import urllib.request
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:3000/signalk/v1/api/vessels/self",
                                        timeout=2.0) as r:
                data = json.loads(r.read().decode())
            # any nav/environment value present → data is flowing
            if data and (data.get("navigation") or data.get("environment")):
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2.0)
    return False


def apply_source(pr: Probe) -> int:
    """Headless: back up settings.json, add the connection, restart, verify,
    roll back on failure. Returns process exit code."""
    if not SIGNALK_SETTINGS.exists():
        print(f"ERROR: {SIGNALK_SETTINGS} not found — run signalk-server-setup first.",
              file=sys.stderr)
        return 2
    provider = build_provider(pr)
    settings = json.loads(SIGNALK_SETTINGS.read_text())
    backup = SIGNALK_SETTINGS.with_suffix(".json.bak")
    shutil.copy2(SIGNALK_SETTINGS, backup)

    providers = settings.setdefault("pipedProviders", [])
    # Drop any prior connection we added, and disable others so two sources
    # can't double-feed the singleton estimators downstream.
    providers = [p for p in providers if not str(p.get("id", "")).startswith("sg-")]
    for p in providers:
        p["enabled"] = False
    providers.append(provider)
    settings["pipedProviders"] = providers
    SIGNALK_SETTINGS.write_text(json.dumps(settings, indent=2))
    print(f"wrote connection '{provider['id']}' → {SIGNALK_SETTINGS}")

    print("restarting signalk-server…")
    _signalk_restart()
    print("verifying data flows (up to 25s)…")
    if _signalk_has_data():
        print(f"✓ relay live on this source: {pr.channel}")
        print("  laptop URLs:")
        for pref in local_ipv4_subnets():
            # show the Pi's own address on each interface
            rc, out = _run(["ip", "-4", "-o", "addr", "show"])
            for line in out.splitlines():
                m = re.search(r"inet (%s\d+)/" % re.escape(pref), line)
                if m:
                    print(f"    ws://{m.group(1)}:3000/signalk/v1/stream?subscribe=self")
        return 0
    print("✗ no data after restart — rolling back config", file=sys.stderr)
    shutil.copy2(backup, SIGNALK_SETTINGS)
    _signalk_restart()
    return 1


# ── capture mode (record raw for format work) ─────────────────────────
def capture_mode(results: list[Probe], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    summary = {"captured_at": now_iso(), "channels": []}
    for r in results:
        rec = {"channel": r.channel, "transport": r.transport, "protocol": r.protocol,
               "live": r.live, "rate_hz": round(r.rate_hz, 2),
               "detail": r.detail, "samples": r.samples, "error": r.error}
        summary["channels"].append(rec)
        if r.samples:
            safe = re.sub(r"[^a-zA-Z0-9]+", "_", r.channel).strip("_")
            (out_dir / f"{stamp}_{safe}.log").write_text("\n".join(str(s) for s in r.samples) + "\n")
    sfile = out_dir / f"{stamp}_summary.json"
    sfile.write_text(json.dumps(summary, indent=2))
    print(f"capture written to {out_dir}/ ({stamp}_*) — summary: {sfile}")
    print_report(results)


# ── cli ───────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", default="scan", choices=("scan", "capture", "apply"))
    ap.add_argument("--duration", type=float, default=15.0,
                    help="seconds to listen per channel (default 15)")
    ap.add_argument("--sweep", action="store_true",
                    help="active subnet sweep for TCP NMEA sources (default off)")
    ap.add_argument("--no-sweep", dest="sweep", action="store_false")
    ap.add_argument("--host", action="append", default=[], dest="hosts",
                    help="extra host to probe for TCP/SignalK (repeatable)")
    ap.add_argument("--channels", default="can,serial,udp,tcp,signalk",
                    help="comma list to limit probing (default all)")
    ap.add_argument("--source", help="apply: channel id to wire (default = best live)")
    ap.add_argument("--auto", action="store_true", help="scan: auto-apply the best live source")
    ap.add_argument("--out-dir", type=Path, default=Path("data/discovery"),
                    help="capture: directory for raw logs + summary")
    ap.add_argument("--json", action="store_true", help="scan: emit JSON instead of a table")
    ap.set_defaults(sweep=False)
    args = ap.parse_args()

    channels = {c.strip() for c in args.channels.split(",") if c.strip()}

    if args.mode == "apply" and args.source:
        # Probe just enough to characterise the named source, then apply.
        results = scan_all(args.duration, sweep=args.sweep, extra_hosts=args.hosts,
                           channels=channels)
        match = next((r for r in results if r.channel == args.source), None)
        if match is None or not match.live:
            print(f"ERROR: source '{args.source}' not live (run `scan` to list).",
                  file=sys.stderr)
            return 2
        return apply_source(match)

    results = scan_all(args.duration, sweep=args.sweep, extra_hosts=args.hosts,
                       channels=channels)

    if args.mode == "capture":
        capture_mode(results, args.out_dir)
        return 0

    if args.json:
        print(json.dumps([{"channel": r.channel, "transport": r.transport,
                           "protocol": r.protocol, "live": r.live,
                           "rate_hz": round(r.rate_hz, 2), "detail": r.detail,
                           "score": round(r.score(), 1)} for r in results], indent=2))
    else:
        print_report(results)

    if args.mode == "apply" or args.auto:
        live = [r for r in results if r.live and r.transport != "signalk"]
        if not live:
            print("no relayable live source to apply.", file=sys.stderr)
            return 1
        return apply_source(live[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
