# Bench materials — NMEA 2000 test rig

Parts list for the bench rig in [bench_test.md](bench_test.md): a powered
NMEA 2000 backbone with three nodes on it — the **Pi + PiCAN-M** (device
under test), the **ESP32** (sensor generator) and the **laptop** (independent
sniffer / oracle). Covers both laptop operating systems.

Prices are rough street prices (USD) as a sanity check, not quotes. Part
numbers are examples — any N2K-standard (DeviceNet Micro, M12 5-pin A-coded)
equivalent works. Verify part numbers before ordering.

## 1. Backbone (shared, needed regardless of laptop OS)

| Qty | Item | Example | ~$ | Notes |
|---|---|---|---|---|
| 1 | Terminator, **male** | Garmin / Maretron / generic N2K terminator | 10 | 120 Ω. Exactly two on the bus — one each end. |
| 1 | Terminator, **female** | same | 10 | Many kits sell the M+F pair together. |
| 4 | T-connector | generic N2K tee | 10 ea | One per node (Pi, ESP32, laptop) + one for power. Tees plug directly into each other on a bench. |
| 1 | Power tap cable (bare-wire end) | Garmin 010-11079-00 or generic N2K power cable | 20 | Red→+12 V, black→GND. Plugs into one tee. |
| 1 | Inline fuse holder + 3 A fuse | ATC blade | 5 | On the +12 V lead. |
| 1 | 12 V supply | 12 V / 2 A wall adapter → barrel-to-screw-terminal adapter, or a bench PSU | 15 | Bus spec is 9–16 V. A bench PSU lets you test low-voltage behavior. |
| 3 | Drop cable, 0.5–1 m, M12 male↔female | generic N2K drop cable | 15 ea | Tee → device. Optional on a bench (devices can plug into tees directly) but saves strain on the PiCAN-M connector. |
| 1 | Multimeter | any | — | 60 Ω check, 12 V check, CAN idle voltages. |

N2K M12 5-pin pinout (used for every DIY pigtail below):

| Pin | Signal | Wire color |
|---|---|---|
| 1 | Shield / drain | bare |
| 2 | NET-S (+12 V) | red |
| 3 | NET-C (GND) | black |
| 4 | CAN_H | white |
| 5 | CAN_L | blue |

## 2. Pi node

You already have the Pi + PiCAN-M. The PiCAN-M has an M12 connector on
board, so it only needs a drop cable (above).

| Qty | Item | Notes |
|---|---|---|
| — | PiCAN-M termination jumper | **OFF.** The backbone already has two terminators. |
| 1 | Pi power (USB-C supply, or UPS HAT) | On the boat the PiCAN-M can power the Pi from NET-S; on the bench a USB-C supply keeps power faults separate from bus faults. |

## 3. ESP32 node (sensor generator)

The original ESP32 (your Feather HUZZAH32) has a built-in CAN controller
(TWAI). It only needs a 3.3 V-logic transceiver chip.

| Qty | Item | Example | ~$ | Notes |
|---|---|---|---|---|
| 1 | CAN transceiver breakout | **Adafruit CAN Pal** (TJA1051T/3, PID 5708) or **Waveshare SN65HVD230 CAN Board** | 5–10 | 3.3 V logic. Do **not** use the MCP2515 FeatherWing — it duplicates the on-chip controller. Leave any on-board 120 Ω termination **off/cut** (it's a drop, not a bus end). |
| 1 | Field-attachable M12 **male** connector | generic N2K field-installable connector | 12 | Screw terminals inside; wire to the transceiver. Alternative: a drop cable cut in half (one end becomes a pigtail). |
| ~0.5 m | 4-conductor + shield cable | N2K "micro" cable or any twisted-pair | 5 | Twist CAN_H/CAN_L together. |
| 1 | 12 V → 5 V buck converter (boat use) | Pololu D24V10F5 (or any buck rated ≥ 20 V input) | 10 | Powers the ESP32 from NET-S. On the bench, plain USB is fine. |
| — | Headers, jumper wire, small perfboard | | | |

Wiring (Feather HUZZAH32 — avoids GPIO 33 = SD CS, 5/18/19 = SPI, 13 = LED):

| ESP32 | Transceiver | M12 pin |
|---|---|---|
| GPIO 32 | TX / CTX / D | — |
| GPIO 27 | RX / CRX / R | — |
| 3V3 | VCC (or VIO, per board) | — |
| GND | GND | 3 (NET-C) |
| — | CANH | 4 (white) |
| — | CANL | 5 (blue) |

Firmware libraries (PlatformIO `lib_deps`):
`ttlappalainen/NMEA2000-library` + `ttlappalainen/NMEA2000_esp32`
(original ESP32). For an ESP32-S3 board use an `NMEA2000_esp32xx`-style
TWAI-driver backend instead.

## 4. Laptop node — pick by operating system

The laptop's job is to read the copper bus **independently of the Pi**, so
a disagreement between them points at the Pi.

### Linux (native)

| Qty | Item | Example | ~$ | Notes |
|---|---|---|---|---|
| 1 | USB-CAN adapter, **candleLight firmware** | **CANable 2.0** (Openlight Labs / Protofusion) or a candleLight-compatible clone | 15–45 | Shows up as a SocketCAN `can0` via the in-kernel `gs_usb` driver — same tools as the Pi. If it ships with slcan firmware, reflash candleLight (web flasher). Termination jumper **OFF**. |
| 1 | Field-attachable M12 male connector + short cable | as ESP32 | 15 | Screw terminals → adapter's CANH/CANL/GND. Leave NET-S unconnected (adapter is USB-powered). |

Software: `sudo apt install can-utils`; canboat built from source
(`git clone https://github.com/canboat/canboat && make && sudo make install`)
for `analyzer` / `candump2analyzer`; optionally `npm i -g @canboat/canboatjs`
for `analyzerjs`.

### Windows (incl. SailinGrace in Docker / WSL2)

SocketCAN inside WSL2 is painful (the stock WSL kernel generally lacks the
CAN modules, and USB has to be forwarded with usbipd-win). Use a gateway that
speaks the network or a vendor-supported USB driver instead.

| Qty | Item | Example | ~$ | Notes |
|---|---|---|---|---|
| 1 | **WiFi N2K gateway** (recommended) | **Yacht Devices YDWG-02** | 200 | Plugs straight into a tee (M12 on the unit), powered by NET-S. Set one server to **RAW** mode (TCP/UDP). Reachable from Windows *and* from WSL2/Docker as plain TCP — no drivers. Also works from Linux, so **one YDWG-02 covers both OSes.** |
| or 1 | USB N2K gateway | Yacht Devices YDNU-02, or Actisense NGT-1-USB | 200–350 | Wired, appears as a COM port. Actisense's free *NMEA Reader* decodes NGT-1 traffic on Windows. More robust than WiFi; can't be read from WSL2 without usbipd. |

Software: the YDWG-02's built-in web UI; Yacht Devices' free *CAN Log
Viewer* (Windows) for decoding; or in WSL2, `nc <ydwg-ip> <raw-port> |
analyzerjs` (canboatjs). Decoding with canboat inside WSL2 needs no kernel
CAN support because it's reading TCP.

### One adapter for both?

If the laptop dual-boots, or you want one purchase: **YDWG-02**. If it's
Linux-only and budget matters: **CANable 2.0**. If you want the gold-standard
wired tap on either OS: **NGT-1** (canboat supports it on Linux via
`actisense-serial`).

## 5. Shopping list summary

Minimum to run the full bench test:

- [ ] 2× terminators (M + F)
- [ ] 4× tees
- [ ] 1× power tap cable + fuse + 12 V supply
- [ ] 1–3× drop cables
- [ ] ESP32: 1× CAN transceiver breakout, 1× M12 field connector, cable
- [ ] Laptop (Linux): CANable 2.0 + 1× M12 field connector — **or**
- [ ] Laptop (Windows / both): YDWG-02
- [ ] Multimeter
- [ ] (Boat only) 12 V→5 V buck for the ESP32
