"""config_server.py: who may use the API, request limits, one job at a time."""
import http.client
import threading
from types import SimpleNamespace

import pytest

import config_server as cs


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(cs, "TOKEN", "sekret")
    srv = cs.ThreadingHTTPServer(("127.0.0.1", 0), cs.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def req(port, method, path, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = dict(headers or {})
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    c.putheader("Host", headers.pop("Host", f"127.0.0.1:{port}"))
    for k, v in headers.items():
        c.putheader(k, v)
    if body is not None and "Content-Length" not in headers:
        c.putheader("Content-Length", str(len(body)))
    c.endheaders(body)
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


def test_page_served_without_token(server):
    assert req(server, "GET", "/") == 200


def test_api_needs_header_even_on_loopback(server):
    assert req(server, "GET", "/api/status") == 401
    assert req(server, "GET", "/api/status", {"X-Config-Token": ""}) == 200


def test_bad_host_refused(server):
    assert req(server, "GET", "/api/status", {"X-Config-Token": "", "Host": "evil.example"}) == 403
    assert req(server, "GET", "/", {"Host": "evil.example"}) == 403


@pytest.mark.parametrize("length,code", [("999999", 413), ("-5", 413), ("abc", 400)])
def test_content_length_limits(server, length, code):
    assert req(server, "POST", "/api/apply",
               {"X-Config-Token": "", "Content-Length": length}) == code


def test_non_object_body_ignored(server):
    assert req(server, "POST", "/api/apply", {"X-Config-Token": ""}, b"[1]") == 400


def test_unknown_post_path(server):
    assert req(server, "POST", "/api/nope", {"X-Config-Token": ""}, b"{}") == 404


def test_one_job_at_a_time(server):
    assert cs.JOB_LOCK.acquire(blocking=False)
    try:
        assert req(server, "POST", "/api/apply", {"X-Config-Token": ""}, b'{"source":"can0"}') == 409
        assert req(server, "GET", "/api/discover", {"X-Config-Token": ""}) == 409
    finally:
        cs.JOB_LOCK.release()


# ── _trusted_peer: the USB-C rule ─────────────────────────────────────
def _handler(peer, local):
    h = cs.Handler.__new__(cs.Handler)
    h.client_address = (peer, 50000)
    h.connection = SimpleNamespace(getsockname=lambda: (local, 8080))
    return h


@pytest.mark.parametrize("peer,local,trusted", [
    ("127.0.0.1", "127.0.0.1", True),               # the Pi itself
    ("::1", "::1", True),
    ("10.55.0.23", "10.55.0.1", True),              # on the USB-C cable
    ("::ffff:10.55.0.23", "::ffff:10.55.0.1", True),
    ("10.55.0.23", "192.168.1.57", False),          # spoofed 10.55 sender over WiFi
    ("192.168.1.20", "10.55.0.1", False),           # odd routing: not from the cable subnet
    ("192.168.1.20", "192.168.1.57", False),        # ordinary WiFi client
])
def test_trusted_peer(peer, local, trusted):
    assert _handler(peer, local)._trusted_peer() is trusted


def test_token_compare(server):  # the server fixture sets TOKEN
    h = _handler("192.168.1.20", "192.168.1.57")
    h.headers = {"X-Config-Token": "sekret", "Host": "192.168.1.57:8080"}
    assert h._auth() == 0
    h.headers = {"X-Config-Token": "wrong", "Host": "192.168.1.57:8080"}
    assert h._auth() == 401
