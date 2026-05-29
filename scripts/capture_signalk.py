#!/usr/bin/env python3
"""Capture SignalK delta stream from the Pi (NMEA 2000 via PiCAN-M).

Parallel to scripts/capture_nmea.py. Connects to a SignalK server's WS
delta stream, writes one JSON-line per delta with UTC timestamp, and
prints a periodic histogram of paths so you can see what data the bus
is actually producing through SignalK.

Usage:
    # Pi on the boat network at pi.local (mDNS) or its IP
    python scripts/capture_signalk.py ws://pi.local:3000/signalk/v1/stream?subscribe=self

    # SignalK on the same machine
    python scripts/capture_signalk.py

    # Longer histogram interval, custom output
    python scripts/capture_signalk.py ws://192.168.42.1:3000/signalk/v1/stream?subscribe=self \\
        --out data/sk_capture_2026-06-19.log --histogram-interval 60

Press Ctrl-C to stop. The log file has one JSON delta per line (newline-
delimited JSON, easy to grep / jq / analyze).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

try:
    import websockets
except ImportError:
    print("ERROR: pip install websockets", file=sys.stderr)
    sys.exit(2)


DEFAULT_URL = "ws://localhost:3000/signalk/v1/stream?subscribe=self"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class CaptureStats:
    """Running tally for the periodic histogram print."""

    def __init__(self) -> None:
        self.start = datetime.now(timezone.utc)
        self.deltas = 0
        self.updates = 0
        self.values = 0
        self.bytes = 0
        self.by_path: Counter[str] = Counter()
        self.by_source: Counter[str] = Counter()
        self.unparseable = 0

    def record(self, raw: bytes) -> None:
        self.deltas += 1
        self.bytes += len(raw)
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            self.unparseable += 1
            return
        # SignalK delta: {"updates": [{"source":..., "values":[{"path":..., "value":...}, ...]}, ...]}
        updates = msg.get("updates") or []
        for u in updates:
            self.updates += 1
            src = (u.get("$source") or u.get("source", {}).get("label", "?"))
            self.by_source[src] += 1
            for v in u.get("values") or []:
                self.values += 1
                p = v.get("path", "")
                if p:
                    self.by_path[p] += 1

    def render(self) -> str:
        elapsed = (datetime.now(timezone.utc) - self.start).total_seconds()
        elapsed = max(elapsed, 1.0)
        out = [
            f"\n── signalk capture stats @ {_now_iso()} ──",
            f"  elapsed     : {elapsed:.0f} s",
            f"  deltas      : {self.deltas}  ({self.deltas / elapsed:.1f}/s)",
            f"  updates     : {self.updates}",
            f"  values      : {self.values}",
            f"  bytes       : {self.bytes}  ({self.bytes / elapsed / 1024:.1f} KiB/s)",
            f"  unparseable : {self.unparseable}",
            f"  sources     : "
            + ", ".join(f"{s}={c}" for s, c in self.by_source.most_common(8)),
            "  paths       :",
        ]
        for path, count in self.by_path.most_common(40):
            out.append(f"    {count:6d}  ({count / elapsed:5.2f}/s)  {path}")
        if len(self.by_path) > 40:
            out.append(f"    … {len(self.by_path) - 40} more paths")
        return "\n".join(out)


async def consume(url: str, write_line) -> None:
    while True:
        try:
            print(f"[{_now_iso()}] connecting to {url}", file=sys.stderr)
            async with websockets.connect(
                url, ping_interval=20, ping_timeout=20, max_size=2**22
            ) as ws:
                print(f"[{_now_iso()}] connected", file=sys.stderr)
                async for msg in ws:
                    raw = msg if isinstance(msg, bytes) else msg.encode()
                    write_line(raw)
        except Exception as e:
            print(f"[{_now_iso()}] disconnected: {e}; retry in 2s", file=sys.stderr)
        await asyncio.sleep(2)


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("url", nargs="?", default=DEFAULT_URL,
                   help=f"SignalK WS URL (default: {DEFAULT_URL})")
    p.add_argument("--out", type=Path, default=None,
                   help="Output log path (default: data/sk_capture_<ts>.log)")
    p.add_argument("--histogram-interval", type=int, default=30,
                   help="Seconds between stats prints (default: 30)")
    args = p.parse_args()

    out = args.out or Path("data") / f"sk_capture_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.log"
    out.parent.mkdir(parents=True, exist_ok=True)

    stats = CaptureStats()
    print(f"[{_now_iso()}] writing to {out}", file=sys.stderr)
    fh = out.open("ab", buffering=0)

    def write_line(raw: bytes) -> None:
        ts = _now_iso().encode()
        fh.write(ts + b" " + raw + (b"" if raw.endswith(b"\n") else b"\n"))
        stats.record(raw)

    async def histogram_loop() -> None:
        while True:
            await asyncio.sleep(args.histogram_interval)
            print(stats.render(), file=sys.stderr)

    tasks = [
        asyncio.create_task(consume(args.url, write_line)),
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
