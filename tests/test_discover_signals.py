"""discover_signals.py: parsing, provider building, source probing, apply."""
import json
import socket
import threading
import time

import pytest

import discover_signals as d


# ── pure helpers ──────────────────────────────────────────────────────
@pytest.mark.parametrize("can_id,pgn", [
    (0x09FD022A, 130306),   # wind data (PDU2, sim at source 42)
    (0x09F1122A, 127250),   # vessel heading
    (0x09F8022A, 129026),   # COG/SOG rapid
    (0x09F8012A, 129025),   # position rapid
    (0x18EAFF00, 59904),    # ISO request (PDU1: PS is a destination)
    (0x18EEFF2A, 60928),    # ISO address claim (PDU1)
])
def test_pgn_from_can_id(can_id, pgn):
    assert d.pgn_from_can_id(can_id) == pgn


def test_classify_nmea0183_counts_and_ignores_junk():
    lines = [b"$IIMWV,45.0,R,10.0,N,A*00\r\n", b"$GPRMC,1,A*00", b"$IIMWV,46,R*00",
             b"garbage", b"!AIVDM,1,1,,A,xyz*00", b""]
    cls = d.classify_nmea0183(lines)
    assert cls["sentences"] == {"MWV": 2, "RMC": 1, "VDM": 1}
    assert list(cls["sentences"])[0] == "MWV"          # sorted by count
    assert cls["talkers"] == {"II": 2, "GP": 1, "AI": 1}


def test_score_prefers_wind():
    plain = d.Probe("a", "udp", live=True, rate_hz=5, detail={"sentences": {"RMC": 1, "GGA": 1}})
    windy = d.Probe("b", "udp", live=True, rate_hz=5, detail={"sentences": {"RMC": 1, "MWV": 1}})
    dead = d.Probe("c", "udp", live=False, detail={"sentences": {"MWV": 1}})
    assert windy.score() > plain.score() > dead.score() == 0


# ── build_provider ────────────────────────────────────────────────────
def _sub(provider):
    return provider["pipeElements"][0]["options"]


def test_build_provider_each_transport():
    can = d.build_provider(d.Probe("can0", "can"))
    assert can["id"] == "sg-can0"
    assert _sub(can) == {"logging": False, "type": "NMEA2000",
                         "subOptions": {"type": "canbus-canboatjs", "interface": "can0"}}

    ser = d.build_provider(d.Probe("/dev/serial/by-id/usb-x@y@38400", "serial"))
    assert _sub(ser)["subOptions"]["device"] == "/dev/serial/by-id/usb-x@y"   # rpartition on '@'
    assert _sub(ser)["subOptions"]["baudrate"] == 38400

    udp = d.build_provider(d.Probe("udp:2000", "udp"))
    assert _sub(udp)["subOptions"] == {"type": "udp", "port": 2000}

    tcp = d.build_provider(d.Probe("tcp:192.168.1.40:10110", "tcp"))
    assert tcp["id"] == "sg-tcp-192-168-1-40-10110"
    assert _sub(tcp)["subOptions"] == {"type": "tcp", "host": "192.168.1.40", "port": 10110}

    with pytest.raises(ValueError):
        d.build_provider(d.Probe("signalk:h:3000", "signalk"))


# ── settings-driven behaviour ─────────────────────────────────────────
def _provider(pid, sub, enabled=True):
    return {"id": pid, "enabled": enabled, "pipeElements": [
        {"type": "providers/simple", "options": {"type": "NMEA0183", "subOptions": sub}}]}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"pipedProviders": [
        _provider("hand-udp", {"type": "udp", "port": 2000}),
        _provider("hand-serial", {"type": "serial", "device": "/dev/ttyUSB7", "baudrate": 38400}),
        _provider("off-udp", {"type": "udp", "port": 50000}, enabled=False),
    ]}))
    monkeypatch.setattr(d, "SIGNALK_SETTINGS", path)
    return path


def test_signalk_claimed(settings):
    claimed = d._signalk_claimed()
    assert claimed["udp"] == {2000}                       # disabled 50000 not claimed
    assert claimed["serial"] == {"/dev/ttyUSB7": 38400}


def test_signalk_claimed_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "SIGNALK_SETTINGS", tmp_path / "nope.json")
    assert d._signalk_claimed() == {"serial": {}, "udp": set()}


def test_probe_udp_skips_claimed_port(settings):
    results = {p.channel: p for p in d.probe_udp(0.2, (2000,))}
    assert results["udp:2000"].error == "in use by signalk-server (already relayed)"
    assert not results["udp:2000"].live


def test_probe_source_claimed_udp_counts_as_live(settings):
    pr = d.probe_source("udp:2000", 0.1)
    assert pr.live and pr.transport == "udp"


@pytest.mark.parametrize("source,needle", [
    ("bogus", "unrecognised source id"),
    ("signalk:127.0.0.1:3000", "can't be relayed"),
])
def test_probe_source_rejects(source, needle):
    pr = d.probe_source(source, 0.1)
    assert not pr.live and needle in pr.error


def test_probe_source_tcp_reaches_unswept_host():
    """apply --source tcp:… must probe that host directly (the sweep bug)."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        for _ in range(10):
            conn.sendall(b"$IIMWV,45.0,R,10.0,N,A*00\r\n")
            time.sleep(0.02)
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    pr = d.probe_source(f"tcp:127.0.0.1:{port}", 1.0)
    srv.close()
    assert pr.live
    assert pr.detail["sentences"] == {"MWV": 10}


def test_tcp_sweep_never_probes_own_nmea_output(monkeypatch):
    probed = []
    monkeypatch.setattr(d, "_port_open", lambda h, p, timeout=0.3: probed.append((h, p)) or False)
    monkeypatch.setattr(d, "default_gateway", lambda: None)
    monkeypatch.setattr(d, "local_ipv4_addrs", lambda: ["10.55.0.1"])
    d.probe_tcp_sweep(0.1, [], (10110, 10111), False, ["10.55.0.1", "192.168.1.40"])
    assert ("127.0.0.1", 10110) not in probed
    assert ("10.55.0.1", 10110) not in probed
    assert ("10.55.0.1", 10111) in probed          # other local ports still checked
    assert ("192.168.1.40", 10110) in probed       # remote 10110 still checked


# ── apply_source ──────────────────────────────────────────────────────
def test_apply_source_success(settings, monkeypatch, capsys):
    monkeypatch.setattr(d, "_signalk_restart", lambda: None)
    monkeypatch.setattr(d, "_signalk_has_data", lambda timeout=25.0: True)
    monkeypatch.setattr(d, "local_ipv4_addrs", lambda: ["10.55.0.1"])
    settings.chmod(0o640)

    assert d.apply_source(d.Probe("can0", "can", live=True)) == 0

    data = json.loads(settings.read_text())
    ids = {p["id"]: p["enabled"] for p in data["pipedProviders"]}
    assert ids == {"hand-udp": False, "hand-serial": False, "off-udp": False, "sg-can0": True}
    assert (settings.stat().st_mode & 0o777) == 0o640      # mode kept by the atomic write
    assert settings.with_suffix(".json.bak").exists()
    out = capsys.readouterr().out
    assert "disabled existing connection(s)" in out and "hand-udp" in out
    assert "off-udp" not in out.split("disabled existing")[1].splitlines()[0]
    assert "ws://10.55.0.1:3000" in out


def test_apply_source_replaces_previous_sg_provider(settings, monkeypatch):
    monkeypatch.setattr(d, "_signalk_restart", lambda: None)
    monkeypatch.setattr(d, "_signalk_has_data", lambda timeout=25.0: True)
    monkeypatch.setattr(d, "local_ipv4_addrs", lambda: [])
    d.apply_source(d.Probe("udp:2000", "udp", live=True))
    d.apply_source(d.Probe("can0", "can", live=True))
    ids = [p["id"] for p in json.loads(settings.read_text())["pipedProviders"]]
    assert ids.count("sg-can0") == 1 and "sg-udp-2000" not in ids


def test_apply_source_rolls_back_without_data(settings, monkeypatch):
    before = settings.read_text()
    restarts = []
    monkeypatch.setattr(d, "_signalk_restart", lambda: restarts.append(1))
    monkeypatch.setattr(d, "_signalk_has_data", lambda timeout=25.0: False)
    assert d.apply_source(d.Probe("can0", "can", live=True)) == 1
    assert settings.read_text() == before
    assert len(restarts) == 2                      # apply + rollback


def test_apply_source_needs_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "SIGNALK_SETTINGS", tmp_path / "missing.json")
    assert d.apply_source(d.Probe("can0", "can", live=True)) == 2


# ── capture ───────────────────────────────────────────────────────────
def test_capture_writes_every_raw_line(tmp_path, capsys):
    pr = d.Probe("udp:2000", "udp", "NMEA0183", live=True, rate_hz=7,
                 detail={"sentences": {"MWV": 140}},
                 samples=["$IIMWV"] * 8, raw=[f"$IIMWV,{i}" for i in range(140)])
    d.capture_mode([pr], tmp_path)
    logs = list(tmp_path.glob("*_udp_2000.log"))
    assert len(logs) == 1
    assert len(logs[0].read_text().splitlines()) == 140
    summary = json.loads(next(tmp_path.glob("*_summary.json")).read_text())
    assert summary["channels"][0]["raw_lines"] == 140
    assert "raw" not in summary["channels"][0]
