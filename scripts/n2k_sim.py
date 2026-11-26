#!/usr/bin/env python3
"""NMEA 2000 bench sensor simulator for a Pi's SocketCAN interface.

Sends the same scripted 120 s sensor profile as the ESP32 firmware in
SailinGrace-esp32/firmware-n2ksim, byte for byte, so either can drive the
bench test (docs/usb_bench_test.md). Typical use is on the *reader* Pi (Pi 4B
+ Waveshare RS485 CAN HAT) feeding the PiCAN-M Pi under test:

    python3 scripts/n2k_sim.py                    # can0, source address 43
    python3 scripts/n2k_sim.py --iface vcan0      # software-only test
    python3 scripts/n2k_sim.py --stdout | analyzerjs   # no CAN at all: print
                                                       # candump -L lines

    PGN 130306 apparent wind     10 Hz  AWS 10/20 kn, AWA +30/+45/-30 deg
    PGN 127250 heading (mag)     10 Hz  350 -> 10 deg ramp, wraps at t=60 s
    PGN 129026 COG/SOG rapid     10 Hz  COG = heading, SOG 7.0 kn
    PGN 129025 position rapid    10 Hz  from 41.4500 N 71.3200 W along COG
    PGN 128259 speed thru water   1 Hz  6.5 kn
    PGN 128267 water depth        1 Hz  25.0 m
    PGN 060928 ISO address claim  at start and on request

One status line per second on stderr is the "script truth" the other nodes
are compared with. Stdlib only. BENCH ONLY: never run this on a boat bus with
live instruments or an autopilot; it transmits fake heading and wind.
"""
from __future__ import annotations

import argparse
import math
import select
import socket
import struct
import sys
import time

KN = 1852.0 / 3600.0          # m/s per knot

# ── profile (must match firmware-n2ksim and docs/bench_test.md) ──────────
PERIOD_S = 120.0
BSP_KN, SOG_KN, DEPTH_M = 6.5, 7.0, 25.0
START_LAT, START_LON = 41.4500, -71.3200


def profile_at(t: float) -> tuple[float, float, float]:
    """(AWS kn, AWA deg 0..360, heading deg) at cycle time t."""
    aws = 10.0 if t < 60.0 else 20.0
    awa = 30.0 if t < 40.0 else (45.0 if t < 80.0 else 330.0)   # 330 = 30 port
    hdg = math.fmod(350.0 + t / 6.0, 360.0)
    return aws, awa, hdg


# ── N2K identity (distinct from the ESP32 sim: 424242 / address 42) ──────
UNIQUE_NUMBER = 424243
MANUFACTURER = 2046            # reserved / experimental
DEVICE_FUNCTION = 130          # PC gateway
DEVICE_CLASS = 25              # inter/intranetwork device
INDUSTRY_GROUP = 4             # marine

PGN_ISO_REQUEST = 59904
PGN_ADDRESS_CLAIM = 60928


def iso_name() -> int:
    """64-bit ISO NAME, packed like NMEA2000-library's SetN2kPGN60928."""
    b = struct.pack("<I", (UNIQUE_NUMBER & 0x1FFFFF) | ((MANUFACTURER & 0x7FF) << 21))
    b += bytes([0, DEVICE_FUNCTION, (DEVICE_CLASS & 0x7F) << 1,
                0x80 | ((INDUSTRY_GROUP & 0x7) << 4)])
    return struct.unpack("<Q", b)[0]


# ── encoders (scales / layouts as in NMEA2000-library N2kMessages.cpp) ───
def can_id(pgn: int, src: int, prio: int, dest: int = 0xFF) -> int:
    pf = (pgn >> 8) & 0xFF
    if pf < 240:                                  # PDU1: destination in PS
        pgn = (pgn & 0x3FF00) | dest
    return ((prio & 7) << 26) | (pgn << 8) | (src & 0xFF)


def u16(v: float, res: float) -> bytes:
    r = round(v / res)
    return struct.pack("<H", r if 0 <= r < 0xFFFE else 0xFFFF)


def i32(v: float, res: float) -> bytes:
    return struct.pack("<i", round(v / res))


def wind(sid, aws_ms, awa_rad):                   # 130306, apparent = 2
    return 130306, 2, bytes([sid]) + u16(aws_ms, 0.01) + u16(awa_rad, 1e-4) + b"\x02\xff\xff"


def heading_mag(sid, hdg_rad):                    # 127250, magnetic = 1, dev/var NA
    return 127250, 2, bytes([sid]) + u16(hdg_rad, 1e-4) + b"\xff\x7f\xff\x7f" + bytes([0xFC | 1])


def cog_sog(sid, cog_rad, sog_ms):                # 129026, true = 0
    return 129026, 2, bytes([sid, 0xFC]) + u16(cog_rad, 1e-4) + u16(sog_ms, 0.01) + b"\xff\xff"


def position(lat, lon):                           # 129025
    return 129025, 2, i32(lat, 1e-7) + i32(lon, 1e-7)


def stw(sid, bsp_ms):                             # 128259, paddle wheel = 0, SOG NA
    return 128259, 2, bytes([sid]) + u16(bsp_ms, 0.01) + b"\xff\xff" + b"\x00\xff\xff"


def depth(sid, d_m):                              # 128267, offset 0, range NA
    return 128267, 3, bytes([sid]) + struct.pack("<I", round(d_m / 0.01)) + b"\x00\x00\xff"


def address_claim():
    return PGN_ADDRESS_CLAIM, 6, struct.pack("<Q", iso_name())


# ── transports ───────────────────────────────────────────────────────────
class CanBus:
    def __init__(self, iface: str):
        self.s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        self.s.bind((iface,))
        self.iface = iface

    def send(self, cid: int, data: bytes) -> bool:
        frame = struct.pack("=IB3x8s", cid | socket.CAN_EFF_FLAG, len(data), data.ljust(8, b"\xff"))
        try:
            self.s.send(frame)
            return True
        except OSError:            # ENOBUFS when nothing ACKs and the queue fills
            return False

    def recv(self, timeout: float):
        r, _, _ = select.select([self.s], [], [], max(timeout, 0))
        if not r:
            return None
        frame = self.s.recv(16)
        cid, dlc = struct.unpack("=IB", frame[:5])
        if not cid & socket.CAN_EFF_FLAG:
            return None
        return cid & socket.CAN_EFF_MASK, frame[8:8 + dlc]


class StdoutBus:
    """candump -L lines, so the encoder can be checked with analyzerjs."""
    iface = "can0"

    def send(self, cid: int, data: bytes) -> bool:
        print(f"({time.time():.6f}) {self.iface} {cid:08X}#{data.hex().upper()}", flush=True)
        return True

    def recv(self, timeout: float):
        time.sleep(max(timeout, 0))
        return None


# ── main loop ────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--iface", default="can0", help="SocketCAN interface (default can0)")
    ap.add_argument("--src", type=int, default=43, help="preferred N2K source address (default 43; the ESP32 sim uses 42)")
    ap.add_argument("--duration", type=float, default=0, help="stop after N seconds (default: run forever)")
    ap.add_argument("--stdout", action="store_true", help="print candump -L lines instead of sending")
    ap.add_argument("--quiet", action="store_true", help="no per-second status line")
    a = ap.parse_args()

    if a.stdout:
        bus = StdoutBus()
    else:
        if not hasattr(socket, "AF_CAN"):
            print("SocketCAN not available (Linux only)", file=sys.stderr)
            return 1
        try:
            bus = CanBus(a.iface)
        except OSError as e:
            print(f"can't open {a.iface}: {e} (is it up? ip link set {a.iface} up type can bitrate 250000)", file=sys.stderr)
            return 1

    src = a.src & 0xFF
    my_name = iso_name()

    def send(msg) -> bool:
        pgn, prio, data = msg
        return bus.send(can_id(pgn, src, prio), data)

    def log(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    log(f"SailinGrace N2K bench simulator on {bus.iface}, address {src} - BENCH ONLY")
    send(address_claim())

    lat, lon = START_LAT, START_LON
    sid = 0
    t0 = last = time.monotonic()
    next10 = next1 = t0
    fails = 0
    last_warn = 0.0

    while True:
        now = time.monotonic()
        if a.duration and now - t0 >= a.duration:
            return 0

        # Answer ISO requests for our address claim, and defend our address.
        got = bus.recv(min(next10, next1) - now)
        if got:
            rid, data = got
            r_src, r_pf, r_ps = rid & 0xFF, (rid >> 16) & 0xFF, (rid >> 8) & 0xFF
            if r_pf == 0xEA and len(data) >= 3 and r_ps in (src, 0xFF):        # 59904 ISO request
                if int.from_bytes(data[:3], "little") == PGN_ADDRESS_CLAIM:
                    send(address_claim())
            elif r_pf == 0xEE and r_src == src and len(data) == 8:             # 60928 claim on our address
                their_name = struct.unpack("<Q", data)[0]
                if their_name < my_name:                                       # lower NAME wins
                    src = (src + 1) & 0xFF if src < 251 else 0
                    log(f"address lost to another device; moving to {src}")
                send(address_claim())
            continue

        t = math.fmod(now - t0, PERIOD_S)
        aws, awa, hdg = profile_at(t)
        ok = True

        if now >= next10:
            next10 += 0.1
            if next10 < now:                       # don't burst after a stall
                next10 = now + 0.1
            sid = (sid + 1) % 253
            dt, last = now - last, now
            d = SOG_KN * KN * dt
            lat += d * math.cos(math.radians(hdg)) / 111320.0
            lon += d * math.sin(math.radians(hdg)) / (111320.0 * math.cos(math.radians(lat)))
            ok &= send(wind(sid, aws * KN, math.radians(awa)))
            ok &= send(heading_mag(sid, math.radians(hdg)))
            ok &= send(cog_sog(sid, math.radians(hdg), SOG_KN * KN))
            ok &= send(position(lat, lon))

        if now >= next1:
            next1 += 1.0
            if next1 < now:
                next1 = now + 1.0
            ok &= send(stw(sid, BSP_KN * KN))
            ok &= send(depth(sid, DEPTH_M))
            if not a.quiet:
                log(f"t={t:5.1f} src={src} aws={aws:.0f}kn awa={awa:.0f} hdg={hdg:.1f} "
                    f"bsp={BSP_KN:.1f} sog={SOG_KN:.1f} depth={DEPTH_M:.1f} lat={lat:.5f} lon={lon:.5f}")

        if not ok:
            fails += 1
            if now - last_warn > 5:
                last_warn = now
                log("WARN: send failed (no other node on the bus to ACK? can0 bus-off?)")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
