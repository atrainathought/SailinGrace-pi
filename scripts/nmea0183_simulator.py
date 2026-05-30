"""Synthetic NMEA 0183 instrument source — a fake boat on a Raspberry Pi.

Emits a realistic NMEA 0183 sentence stream (the same data a real
instrument bus / MFD WiFi puts out) so the full ingest path can be
exercised end-to-end without any boat hardware:

    Pi (this script)  ──NMEA 0183──▶  SailinGrace nmea0183_client
                                      → coalescer → UKF / estimators
                                      → instrument_samples → /api/wind/current

It models the same deterministic boat as ``signalk_simulator.py`` —
Newport → Bermuda at 6.5 kt, bearing ~158°T, with sinusoidal wind — so
the two sources are interchangeable for testing. The difference is the
wire format: this speaks NMEA 0183 (the instrument-bus path), the other
speaks SignalK deltas (the SignalK-server path).

Sentences emitted each tick (1 Hz default):
    RMC  GPS time + position + SOG + COG          (talker GP)
    VHW  speed through water + heading (true)      (talker II)
    HDG  magnetic heading + variation              (talker II)
    MWV  apparent wind angle + speed (R)           (talker II)
    MWD  true wind direction + speed               (talker II)
    XDR  heel + pitch (IMU)                         (talker II)
    ROT  rate of turn                              (talker II)

VHW+HDG+MWV satisfy the coalescer's BSP+HDG+wind requirement; XDR/ROT
unlock the IMU-heel / gyro-yaw sensor capabilities so heading fusion and
maneuver detection light up too.

── Run it ──────────────────────────────────────────────────────────
On the Pi (or any machine), pick a transport:

    # TCP server (Expedition / OpenCPN style). App connects to the Pi.
    python scripts/nmea0183_simulator.py --transport tcp --port 10110

    # UDP broadcast (MFD-WiFi style). App listens on the port.
    python scripts/nmea0183_simulator.py --transport udp --port 10110

    # Raw serial bus (RS-422/USB adapter, e.g. a null-modem pair).
    python scripts/nmea0183_simulator.py --transport serial \
        --serial-device /dev/ttyUSB0 --baud 4800

Then point the backend at it (laptop side). Tag the run synthetic so the
fake samples stay distinguishable from real instrument data in the DB:

    INGEST_SOURCE=synthetic NMEA0183_URL=tcp://<pi-ip>:10110 \
        python -m uvicorn backend.main:app
    INGEST_SOURCE=synthetic NMEA0183_URL=udp://0.0.0.0:10110 \
        python -m uvicorn backend.main:app

Want the full boat topology instead (Pi → signalk-server → app)? Feed
this TCP/UDP stream into signalk-server's "NMEA 0183" connection and
leave the backend on SIGNALK_URL — same sentences, real relay path.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import socket
import time
from datetime import datetime, timezone

# Newport, RI → St. David's Head, Bermuda — initial bearing ~158°T.
START_LAT = 41.4901
START_LON = -71.3128
INITIAL_BEARING_DEG = 158.0
BSP_KT = 6.5
# Magnetic variation for the race area (NOAA WMM ~14°W). Used so HDG
# carries a realistic mag heading + variation the parser converts back.
MAGVAR_W_DEG = 14.0

KT_TO_MS = 0.514444
DEG_TO_RAD = math.pi / 180.0
EARTH_R_NM = 3440.065


# ── boat physics (mirrors signalk_simulator.py) ─────────────────────

def _advance_position(lat: float, lon: float, bearing_deg: float, distance_nm: float
                      ) -> tuple[float, float]:
    br = bearing_deg * DEG_TO_RAD
    lat_r, lon_r = lat * DEG_TO_RAD, lon * DEG_TO_RAD
    d = distance_nm / EARTH_R_NM
    new_lat = math.asin(
        math.sin(lat_r) * math.cos(d) + math.cos(lat_r) * math.sin(d) * math.cos(br)
    )
    new_lon = lon_r + math.atan2(
        math.sin(br) * math.sin(d) * math.cos(lat_r),
        math.cos(d) - math.sin(lat_r) * math.sin(new_lat),
    )
    return new_lat / DEG_TO_RAD, ((new_lon / DEG_TO_RAD) + 540.0) % 360.0 - 180.0


def _apparent_wind(hdg_deg: float, bsp_kt: float, tws_kt: float, twd_deg: float
                   ) -> tuple[float, float]:
    """True wind + boat motion → (apparent angle 0-359 clockwise from bow,
    apparent speed kt). Same convention as wind_ukf._predict_apparent."""
    twd_r, hdg_r = twd_deg * DEG_TO_RAD, hdg_deg * DEG_TO_RAD
    tw_e = -tws_kt * math.sin(twd_r)
    tw_n = -tws_kt * math.cos(twd_r)
    bv_e = bsp_kt * math.sin(hdg_r)
    bv_n = bsp_kt * math.cos(hdg_r)
    aw_e, aw_n = tw_e - bv_e, tw_n - bv_n
    s, c = math.sin(hdg_r), math.cos(hdg_r)
    aw_bow = aw_e * s + aw_n * c
    aw_stbd = aw_e * c - aw_n * s
    aws = math.hypot(aw_bow, aw_stbd)
    awa_signed = math.degrees(math.atan2(-aw_stbd, -aw_bow))  # + = starboard
    return (awa_signed + 360.0) % 360.0, aws


# ── NMEA 0183 formatting ────────────────────────────────────────────

def _nmea(body: str) -> str:
    """Wrap a sentence body (talker+type+fields, no '$' or checksum) with a
    leading '$', XOR checksum, and CRLF."""
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"${body}*{cs:02X}\r\n"


def _lat_nmea(lat: float) -> tuple[str, str]:
    hemi = "N" if lat >= 0 else "S"
    a = abs(lat)
    deg = int(a)
    minutes = (a - deg) * 60.0
    return f"{deg:02d}{minutes:06.3f}", hemi


def _lon_nmea(lon: float) -> tuple[str, str]:
    hemi = "E" if lon >= 0 else "W"
    a = abs(lon)
    deg = int(a)
    minutes = (a - deg) * 60.0
    return f"{deg:03d}{minutes:06.3f}", hemi


def build_sentences(t: datetime, lat: float, lon: float, hdg_deg: float,
                    bsp_kt: float, tws_kt: float, twd_deg: float,
                    heel_deg: float, pitch_deg: float, yaw_dps: float,
                    emit_mwd: bool, emit_imu: bool) -> list[str]:
    awa, aws = _apparent_wind(hdg_deg, bsp_kt, tws_kt, twd_deg)
    hdg_mag = (hdg_deg + MAGVAR_W_DEG) % 360.0   # true = mag - varW
    twd_mag = (twd_deg + MAGVAR_W_DEG) % 360.0
    lat_s, ns = _lat_nmea(lat)
    lon_s, ew = _lon_nmea(lon)
    hhmmss = t.strftime("%H%M%S.00")
    ddmmyy = t.strftime("%d%m%y")

    # Order matters: the coalescer emits a sample as soon as BSP + HDG +
    # a wind field are all seen, so we put the wind sentence (MWV) LAST —
    # that way heel/pitch/yaw (XDR/ROT) are already latched when the very
    # first sample is built, not just from the second tick onward.
    out = [
        # GPS: position, SOG (=BSP here), COG (=HDG here), magvar.
        _nmea(f"GPRMC,{hhmmss},A,{lat_s},{ns},{lon_s},{ew},"
              f"{bsp_kt:.1f},{hdg_deg:.1f},{ddmmyy},{MAGVAR_W_DEG:.1f},W"),
        # Speed through water + true/mag heading.
        _nmea(f"IIVHW,{hdg_deg:.1f},T,{hdg_mag:.1f},M,{bsp_kt:.2f},N,"
              f"{bsp_kt * 1.852:.2f},K"),
        # Magnetic heading + variation (parser converts to true).
        _nmea(f"IIHDG,{hdg_mag:.1f},,,{MAGVAR_W_DEG:.1f},W"),
    ]
    if emit_imu:
        # Heel + pitch via XDR (angle/degrees), yaw via ROT (deg/min).
        out.append(
            _nmea(f"IIXDR,A,{heel_deg:.1f},D,ROLL,A,{pitch_deg:.1f},D,PITCH")
        )
        out.append(_nmea(f"IIROT,{yaw_dps * 60.0:.1f},A"))
    if emit_mwd:
        out.append(
            _nmea(f"IIMWD,{twd_deg:.1f},T,{twd_mag:.1f},M,{tws_kt:.1f},N,"
                  f"{tws_kt * KT_TO_MS:.1f},M")
        )
    # Apparent wind LAST — it completes the required set and triggers emit.
    out.append(_nmea(f"IIMWV,{awa:.1f},R,{aws:.1f},N,A"))
    return out


# ── boat state over time ────────────────────────────────────────────

class Boat:
    """Advances the deterministic track once per tick; produces sentences."""

    def __init__(self, args: argparse.Namespace):
        self.lat = START_LAT
        self.lon = START_LON
        self.bearing = args.bearing
        self.bsp = args.bsp
        self.tws0 = args.tws
        self.twd0 = args.twd
        self.dt = 1.0 / args.hz
        self.emit_mwd = not args.no_mwd
        self.emit_imu = not args.no_imu
        self.t0 = time.time()

    def tick(self) -> list[str]:
        elapsed_h = (time.time() - self.t0) / 3600.0
        tws = self.tws0 + 3.0 * math.sin(elapsed_h * 0.4)
        twd = self.twd0 + 8.0 * math.sin(elapsed_h * 0.25)
        heel = 8.0 + 2.0 * math.sin(elapsed_h * 0.6)
        yaw = 0.5 * math.sin(elapsed_h * 1.5)
        sentences = build_sentences(
            datetime.now(timezone.utc), self.lat, self.lon, self.bearing,
            self.bsp, tws, twd, heel, 0.0, yaw,
            self.emit_mwd, self.emit_imu,
        )
        # Advance one tick of travel.
        self.lat, self.lon = _advance_position(
            self.lat, self.lon, self.bearing, self.bsp * self.dt / 3600.0
        )
        return sentences


# ── transports ──────────────────────────────────────────────────────

async def run_tcp(boat: Boat, host: str, port: int) -> None:
    clients: set[asyncio.StreamWriter] = set()

    async def on_client(_reader, writer):
        peer = writer.get_extra_info("peername")
        clients.add(writer)
        print(f"nmea0183_simulator: client connected {peer}")
        try:
            await _reader.read()  # wait until the client closes
        except Exception:
            pass
        finally:
            clients.discard(writer)
            print(f"nmea0183_simulator: client disconnected {peer}")

    server = await asyncio.start_server(on_client, host, port)
    print(f"nmea0183_simulator: TCP server on {host}:{port} "
          f"(connect with NMEA0183_URL=tcp://<host>:{port})")
    async with server:
        while True:
            batch = "".join(boat.tick()).encode("ascii", "replace")
            for w in list(clients):
                try:
                    w.write(batch)
                    await w.drain()
                except Exception:
                    clients.discard(w)
            await asyncio.sleep(boat.dt)


async def run_udp(boat: Boat, host: str, port: int) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    dest = (host, port)
    print(f"nmea0183_simulator: UDP broadcast to {host}:{port} "
          f"(listen with NMEA0183_URL=udp://0.0.0.0:{port})")
    while True:
        for s in boat.tick():
            sock.sendto(s.encode("ascii", "replace"), dest)
        await asyncio.sleep(boat.dt)


async def run_serial(boat: Boat, device: str, baud: int) -> None:
    import serial_asyncio  # from pyserial-asyncio (a backend dep)
    _, writer = await serial_asyncio.open_serial_connection(url=device, baudrate=baud)
    print(f"nmea0183_simulator: writing to {device} @ {baud} "
          f"(read with NMEA0183_URL=serial://{device}?baud={baud})")
    while True:
        writer.write("".join(boat.tick()).encode("ascii", "replace"))
        await writer.drain()
        await asyncio.sleep(boat.dt)


def main() -> None:
    p = argparse.ArgumentParser(description="Synthetic NMEA 0183 instrument source.")
    p.add_argument("--transport", choices=["tcp", "udp", "serial"], default="tcp")
    p.add_argument("--host", default="0.0.0.0",
                   help="TCP bind host, or UDP broadcast address (e.g. 255.255.255.255)")
    p.add_argument("--port", type=int, default=10110)
    p.add_argument("--serial-device", default="/dev/ttyUSB0")
    p.add_argument("--baud", type=int, default=4800)
    p.add_argument("--hz", type=float, default=1.0, help="Sentence batch rate (Hz)")
    p.add_argument("--bsp", type=float, default=BSP_KT, help="Boat speed (kt)")
    p.add_argument("--tws", type=float, default=14.0, help="Mean true wind speed (kt)")
    p.add_argument("--twd", type=float, default=220.0, help="Mean true wind dir (°T)")
    p.add_argument("--bearing", type=float, default=INITIAL_BEARING_DEG,
                   help="Course / heading (°T)")
    p.add_argument("--no-mwd", action="store_true", help="Skip true-wind MWD sentence")
    p.add_argument("--no-imu", action="store_true", help="Skip XDR/ROT (heel/pitch/yaw)")
    args = p.parse_args()

    boat = Boat(args)
    if args.transport == "tcp":
        runner = run_tcp(boat, args.host, args.port)
    elif args.transport == "udp":
        host = args.host if args.host != "0.0.0.0" else "255.255.255.255"
        runner = run_udp(boat, host, args.port)
    else:
        runner = run_serial(boat, args.serial_device, args.baud)

    try:
        asyncio.run(runner)
    except KeyboardInterrupt:
        print("\nnmea0183_simulator: bye")


if __name__ == "__main__":
    main()
