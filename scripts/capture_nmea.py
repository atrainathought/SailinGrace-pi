#!/usr/bin/env python3
"""Capture raw NMEA 0183 from Expedition / MFD WiFi / raw 0183 bus.

The point of this tool is to find out what the source actually emits
before we build parsing/UI on top of it. Stdlib + pyserial-asyncio
(only for the serial:// path) — runs on any laptop with python3.

Usage:
    # Expedition on this machine
    python scripts/capture_nmea.py tcp://127.0.0.1:10110

    # Expedition on the boat network at 192.168.1.40
    python scripts/capture_nmea.py tcp://192.168.1.40:10110

    # B&G GoFree / Garmin / Raymarine WiFi MFD broadcast
    python scripts/capture_nmea.py udp://0.0.0.0:2000

    # Bare NMEA 0183 bus via a USB-RS422 adapter (Actisense USG-2 etc.)
    python scripts/capture_nmea.py serial:///dev/ttyUSB0
    python scripts/capture_nmea.py serial:///dev/ttyUSB0?baud=38400

    # Custom output path and longer histogram interval
    python scripts/capture_nmea.py tcp://192.168.1.40:10110 \\
        --out data/expedition_capture_2026-06-19.log \\
        --histogram-interval 60

Press Ctrl-C to stop. The log file contains one timestamped line per
sentence; the histogram printed periodically shows what sentence types
and talker IDs Expedition is actually producing and at what rate.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import signal
import socket
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse


# Match the address+sentence id at the start of an NMEA line: $TTSSS or !TTSSS
# where TT is the 2-char talker (II, GP, WI, ...) and SSS is the 3-char sentence.
_ADDR_RE = re.compile(rb"^[!$]([A-Z][A-Z])([A-Z]{3}),")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class CaptureStats:
    """Running tally of what we've seen, for the periodic histogram print."""

    def __init__(self) -> None:
        self.start = datetime.now(timezone.utc)
        self.lines = 0
        self.bytes = 0
        self.by_sentence: Counter[bytes] = Counter()
        self.by_talker: Counter[bytes] = Counter()
        self.unparseable = 0

    def record(self, raw: bytes) -> None:
        self.lines += 1
        self.bytes += len(raw)
        m = _ADDR_RE.match(raw)
        if m:
            self.by_talker[m.group(1)] += 1
            self.by_sentence[m.group(2)] += 1
        else:
            self.unparseable += 1

    def render(self) -> str:
        elapsed = (datetime.now(timezone.utc) - self.start).total_seconds()
        elapsed = max(elapsed, 1.0)
        out = [
            f"\n── capture stats @ {_now_iso()} ──",
            f"  elapsed     : {elapsed:.0f} s",
            f"  lines       : {self.lines}  ({self.lines / elapsed:.1f}/s)",
            f"  bytes       : {self.bytes}  ({self.bytes / elapsed / 1024:.1f} KiB/s)",
            f"  unparseable : {self.unparseable}",
            f"  talkers     : "
            + ", ".join(
                f"{t.decode():2s}={c}" for t, c in self.by_talker.most_common()
            ),
            "  sentences   :",
        ]
        for sent, count in self.by_sentence.most_common():
            rate = count / elapsed
            out.append(f"    {sent.decode():3s}  {count:6d}  ({rate:5.2f}/s)")
        return "\n".join(out)


async def consume_tcp(host: str, port: int, write_line) -> None:
    while True:
        try:
            print(f"[{_now_iso()}] connecting to tcp://{host}:{port}", file=sys.stderr)
            reader, writer = await asyncio.open_connection(host, port)
            print(f"[{_now_iso()}] connected", file=sys.stderr)
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        print(f"[{_now_iso()}] peer closed connection", file=sys.stderr)
                        break
                    write_line(line)
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
        except (ConnectionRefusedError, OSError) as e:
            print(f"[{_now_iso()}] connect failed: {e}; retry in 2s", file=sys.stderr)
        await asyncio.sleep(2)


async def consume_udp(host: str, port: int, write_line) -> None:
    # Plain socket — asyncio UDP is more ceremony than this needs.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.setblocking(False)
    print(f"[{_now_iso()}] listening on udp://{host}:{port}", file=sys.stderr)
    loop = asyncio.get_running_loop()
    while True:
        data = await loop.sock_recv(sock, 4096)
        # UDP datagrams can pack multiple sentences; split on \n so the
        # downstream stats see one line per sentence.
        for line in data.splitlines(keepends=True):
            if line.strip():
                write_line(line if line.endswith(b"\n") else line + b"\n")


async def consume_serial(device: str, baud: int, write_line) -> None:
    """Read raw NMEA 0183 from a serial port (RS-422 / RS-232).

    Imports pyserial-asyncio lazily so the TCP / UDP paths still work
    on machines that don't have it installed.
    """
    try:
        import serial_asyncio
    except ImportError:
        print(
            f"[{_now_iso()}] serial:// needs pyserial-asyncio (pip install pyserial-asyncio)",
            file=sys.stderr,
        )
        return
    reader, writer = await serial_asyncio.open_serial_connection(
        url=device, baudrate=baud,
    )
    print(f"[{_now_iso()}] opened serial://{device} at {baud} baud", file=sys.stderr)
    try:
        buf = bytearray()
        while True:
            chunk = await reader.read(256)
            if not chunk:
                return
            buf.extend(chunk)
            # Split on any of CRLF / LF / CR; permissive against
            # talker quirks. Emit each line newline-terminated so
            # the file format stays consistent with TCP / UDP.
            while True:
                idx = -1
                sep_len = 0
                for sep in (b"\r\n", b"\n", b"\r"):
                    i = buf.find(sep)
                    if i != -1 and (idx == -1 or i < idx):
                        idx, sep_len = i, len(sep)
                if idx == -1:
                    break
                line = bytes(buf[:idx])
                del buf[: idx + sep_len]
                if line.strip():
                    write_line(line + b"\n")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("url", help="tcp://host:port | udp://host:port | serial:///dev/ttyUSB0[?baud=N]")
    p.add_argument("--out", type=Path, default=None,
                   help="Output log path (default: data/nmea_capture_<ts>.log)")
    p.add_argument("--histogram-interval", type=int, default=30,
                   help="Seconds between stats prints (default: 30)")
    args = p.parse_args()

    u = urlparse(args.url)
    scheme = (u.scheme or "").lower()
    if scheme in ("tcp", "udp"):
        if not u.hostname or not u.port:
            p.error(f"{scheme}:// URL must include host and port (got {args.url!r})")
    elif scheme == "serial":
        # Validation handled inside consume_serial; just sanity-check
        # that a device path is present.
        if not (u.path or u.netloc):
            p.error(f"serial:// URL must include a device path (got {args.url!r})")
    else:
        p.error(f"URL scheme must be tcp / udp / serial (got {args.url!r})")

    out = args.out or Path("data") / f"nmea_capture_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.log"
    out.parent.mkdir(parents=True, exist_ok=True)

    stats = CaptureStats()
    print(f"[{_now_iso()}] writing to {out}", file=sys.stderr)
    fh = out.open("ab", buffering=0)  # unbuffered: don't lose data on Ctrl-C

    def write_line(raw: bytes) -> None:
        ts = _now_iso().encode()
        fh.write(ts + b" " + raw if raw.endswith(b"\n") else ts + b" " + raw + b"\n")
        stats.record(raw)

    async def histogram_loop() -> None:
        while True:
            await asyncio.sleep(args.histogram_interval)
            print(stats.render(), file=sys.stderr)

    if scheme == "tcp":
        consumer_task = consume_tcp(u.hostname, u.port, write_line)
    elif scheme == "udp":
        consumer_task = consume_udp(u.hostname, u.port, write_line)
    else:  # serial
        device = u.path or u.netloc
        baud = int(parse_qs(u.query).get("baud", ["4800"])[0])
        consumer_task = consume_serial(device, baud, write_line)
    tasks = [
        asyncio.create_task(consumer_task),
        asyncio.create_task(histogram_loop()),
    ]

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    try:
        await stop.wait()
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        fh.close()
        print(stats.render(), file=sys.stderr)
        print(f"[{_now_iso()}] capture saved to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
