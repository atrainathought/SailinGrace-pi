#!/usr/bin/env python3
"""Waveshare UPS HAT (D) battery monitor for the SailinGrace Pi kit.

Polls the on-board INA219 over I2C and triggers a clean shutdown when
the 2× 18650 pack drops below a configurable cutoff. The whole point of
the UPS is *graceful* shutdown when boat power is yanked — without this
script the Pi runs until the cells flatline, which corrupts the SD
card's SQLite WAL as reliably as just pulling the plug.

Why we don't just trust the UPS hardware: the HAT (D) keeps the Pi
running until the cells are exhausted, then drops the rail. That cold-
power-off mid-write is the failure mode we paid for the UPS to prevent.
Polling the bus voltage and calling `systemctl poweroff` while there's
still 1–2 minutes of headroom converts a hard yank into a normal
shutdown.

I2C wiring on Pi 40-pin header:
    Pin 3 (SDA) / Pin 5 (SCL)  — UPS HAT (D)'s INA219 lives at 0x42

Defaults are sized for the HAT (D)'s 2× 18650 series pack:
    Full:    ~8.4 V   (4.2 V/cell × 2)
    Nominal: ~7.4 V
    Knee:    ~6.8 V   — discharge starts falling fast below this
    Empty:   ~6.0 V   (3.0 V/cell × 2, manufacturer cutoff)

The default LOW_V of 6.4 V triggers shutdown at ~10 % remaining,
leaving ~2 minutes of buffer for systemd to stop services, flush the
SQLite WAL, and unmount cleanly.

Override via env vars:
    UPS_LOW_V        — voltage threshold for shutdown (default 6.4)
    UPS_POLL_S       — poll interval seconds (default 30)
    UPS_I2C_ADDR     — INA219 address (default 0x42; some Waveshare
                       boards ship at 0x40 or 0x43 — check
                       `i2cdetect -y 1` after install)
    UPS_DRY_RUN      — if set, log instead of shutting down (for
                       bench-testing the discharge curve)

Usage:
    python scripts/ups_monitor.py
or as a systemd unit — see scripts/systemd/ups-monitor.service.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

try:
    from smbus2 import SMBus
except ImportError:
    print("ERROR: smbus2 not installed. `pip install smbus2`", file=sys.stderr)
    sys.exit(2)

logger = logging.getLogger("ups_monitor")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

ADDR = int(os.environ.get("UPS_I2C_ADDR", "0x42"), 16)
LOW_V = float(os.environ.get("UPS_LOW_V", "6.4"))
POLL_S = float(os.environ.get("UPS_POLL_S", "30"))
DRY_RUN = bool(os.environ.get("UPS_DRY_RUN"))

# Minimum consecutive low readings before triggering shutdown.
# Guards against single noisy reads under transient load (e.g. a
# starter-motor brownout where bus voltage dips and recovers).
LOW_STREAK_REQUIRED = 3

# INA219 register layout (datasheet, Texas Instruments):
#   0x02 = Bus Voltage. LSB = 4 mV, value is shifted right by 3 bits.
INA219_REG_BUS_VOLTAGE = 0x02


def read_bus_voltage(bus: SMBus) -> float:
    """Read the INA219 bus voltage register and convert to volts.

    Returns the voltage on the UPS HAT's main rail, which tracks the
    2× 18650 series pack's terminal voltage when on battery.
    """
    raw = bus.read_word_data(ADDR, INA219_REG_BUS_VOLTAGE)
    # smbus returns little-endian; INA219 is big-endian → byteswap.
    swapped = ((raw & 0xFF) << 8) | ((raw >> 8) & 0xFF)
    # Low 3 bits are status flags; voltage starts at bit 3.
    return (swapped >> 3) * 0.004


def shutdown() -> None:
    """Initiate a clean systemd-managed shutdown."""
    if DRY_RUN:
        logger.warning("DRY_RUN set — would have called systemctl poweroff")
        return
    logger.warning("battery low; calling systemctl poweroff")
    subprocess.run(["sudo", "systemctl", "poweroff"], check=False)


def main() -> int:
    logger.info(
        "ups_monitor starting: addr=0x%02x low_v=%.2f poll_s=%.0f%s",
        ADDR, LOW_V, POLL_S, " (dry-run)" if DRY_RUN else "",
    )
    streak = 0
    try:
        with SMBus(1) as bus:
            while True:
                try:
                    v = read_bus_voltage(bus)
                except OSError as e:
                    # I2C transient — UPS may be in the middle of
                    # switching rails. Don't count toward streak.
                    logger.debug("i2c read failed: %s", e)
                    time.sleep(POLL_S)
                    continue

                if v < LOW_V:
                    streak += 1
                    logger.info(
                        "bus voltage %.2f V below %.2f V threshold "
                        "(streak %d/%d)",
                        v, LOW_V, streak, LOW_STREAK_REQUIRED,
                    )
                    if streak >= LOW_STREAK_REQUIRED:
                        shutdown()
                        return 0
                else:
                    if streak > 0:
                        logger.info(
                            "bus voltage %.2f V recovered — resetting streak", v,
                        )
                    streak = 0

                time.sleep(POLL_S)
    except KeyboardInterrupt:
        logger.info("ups_monitor stopping (SIGINT)")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
