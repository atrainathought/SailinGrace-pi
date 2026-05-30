#!/usr/bin/env python3
"""SailinGrace Pi — config interface for the discovery / bring-up phase.

A tiny stdlib-only web app you open from a laptop to set the Pi up when
you don't yet know how it connects to the boat: scan + join a WiFi, run
signal discovery, see what's live, and wire the relay to it. Once it's
configured the Pi runs headless; this is only for setup.

It serves on 0.0.0.0:8080 so it's reachable however you plug in — over
the Pi's own WiFi AP, over ethernet, or over a USB link — and it just
shells out to the tools that already exist:

    scripts/discover_signals.py   scan / capture / apply
    scripts/pi/join_wifi.sh       --scan / join

No routing/weather code, no extra dependencies (Python stdlib only).
Run by `sailingrace-config.service` as root so it can drive nmcli,
systemctl, and can0. LAN-only; on a private boat network it is
unauthenticated unless CONFIG_TOKEN is set (then send ?token=… ).

    sudo python3 scripts/pi/config_server.py            # :8080
    CONFIG_PORT=9000 python3 scripts/pi/config_server.py
"""

from __future__ import annotations

import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
VENV_PY = REPO_ROOT / ".venv" / "bin" / "python"
PY = str(VENV_PY) if VENV_PY.exists() else "python3"
DISCOVER = str(REPO_ROOT / "scripts" / "discover_signals.py")
JOIN_WIFI = str(REPO_ROOT / "scripts" / "pi" / "join_wifi.sh")
PORT = int(os.environ.get("CONFIG_PORT", "8080"))
TOKEN = os.environ.get("CONFIG_TOKEN", "")
# Where signalk-server keeps its settings — so `apply` writes the right file
# even though this server runs as root (whose $HOME is /root).
SIGNALK_SETTINGS = os.environ.get(
    "SIGNALK_NODE_SETTINGS", "/home/pi/.signalk/settings.json")

PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>SailinGrace Pi — setup</title>
<style>
 body{font:15px/1.5 system-ui,sans-serif;max-width:720px;margin:1.5rem auto;padding:0 1rem;color:#10243e}
 h1{font-size:1.3rem} h2{font-size:1.05rem;margin-top:1.6rem;border-bottom:1px solid #d8e0ea;padding-bottom:.3rem}
 button{font:inherit;padding:.45rem .9rem;border:1px solid #2a6;border-radius:7px;background:#2a6;color:#fff;cursor:pointer}
 button.sec{background:#fff;color:#2a6}
 input,select{font:inherit;padding:.4rem;border:1px solid #b8c4d2;border-radius:6px}
 pre{background:#0d1b2a;color:#cfe;padding:.7rem;border-radius:7px;overflow:auto;white-space:pre-wrap;font-size:13px}
 .row{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin:.5rem 0}
 .ok{color:#1a7} .bad{color:#c33} small{color:#5a6b80}
</style></head><body>
<h1>⛵ SailinGrace Pi — setup</h1>
<div id=status><em>loading status…</em></div>

<h2>1 · Join the boat WiFi</h2>
<small>Do this over an ethernet/USB link so the WiFi radio is free to join the boat network.</small>
<div class=row><button class=sec onclick=scanWifi()>Scan networks</button></div>
<div id=wifilist></div>
<div class=row>
  <input id=ssid placeholder="SSID"> <input id=pw type=password placeholder="password">
  <button onclick=joinWifi()>Join</button>
</div>

<h2>2 · Discover live signals</h2>
<div class=row>
  <label><input type=checkbox id=sweep> sweep subnet (find Expedition TCP)</label>
  <button onclick=discover()>Scan channels</button>
  <button class=sec onclick=capture()>Capture for parsing</button>
</div>
<div id=sources></div>

<h2>3 · Wire the relay</h2>
<div class=row>
  <select id=source></select>
  <button onclick=apply_()>Apply &amp; verify</button>
</div>
<pre id=log>—</pre>

<script>
const tok = new URLSearchParams(location.search).get('token');
const q = p => tok ? p+(p.includes('?')?'&':'?')+'token='+encodeURIComponent(tok) : p;
const log = (t)=>document.getElementById('log').textContent = (typeof t==='string'?t:JSON.stringify(t,null,2));
async function jget(p){const r=await fetch(q(p));return r.json()}
async function jpost(p,b){const r=await fetch(q(p),{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(b||{})});return r.json()}

async function status(){
  try{const s=await jget('/api/status');
    document.getElementById('status').innerHTML =
      `<div>WiFi: <b>${s.wifi||'—'}</b> · relay source: <b class="${s.relay?'ok':'bad'}">${s.relay||'none'}</b></div>`+
      `<div><small>reach this Pi at: ${(s.urls||[]).join(' · ')||'—'}</small></div>`;
  }catch(e){document.getElementById('status').textContent='status error'}
}
async function scanWifi(){log('scanning WiFi…');const r=await jget('/api/wifi/scan');
  document.getElementById('wifilist').innerHTML =
    (r.networks||[]).map(n=>`<div class=row><button class=sec onclick="document.getElementById('ssid').value='${n.ssid.replace(/'/g,"")}'">${n.ssid}</button> <small>${n.signal}% ${n.security}</small></div>`).join('');
  log(r);}
async function joinWifi(){log('joining…');
  const r=await jpost('/api/wifi/join',{ssid:ssid.value,password:pw.value});log(r);status();}
async function discover(){log('scanning channels (can take ~20s)…');
  const r=await jget('/api/discover?sweep='+(sweep.checked?1:0));
  const sel=document.getElementById('source');sel.innerHTML='';
  (r.results||[]).filter(x=>x.live&&x.transport!=='signalk').forEach(x=>{
    const o=document.createElement('option');o.value=x.channel;o.textContent=`${x.channel} (${x.protocol}, ${x.rate_hz}/s)`;sel.appendChild(o);});
  document.getElementById('sources').innerHTML =
    (r.results||[]).map(x=>`<div class="${x.live?'ok':''}">${x.live?'●':'○'} ${x.channel} — ${x.protocol||'—'} ${x.rate_hz?x.rate_hz+'/s':''}</div>`).join('');
  log(r);}
async function capture(){log('capturing…');const r=await jpost('/api/capture',{});log(r);}
async function apply_(){const s=document.getElementById('source').value;
  if(!s){log('pick a source first');return}
  log('applying '+s+' and verifying (up to ~30s)…');
  const r=await jpost('/api/apply',{source:s});log(r);status();}
status();
</script></body></html>"""


def _run(cmd, env_extra=None, timeout=90):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


def _wifi_scan():
    rc, out, _ = _run(["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"], timeout=20)
    nets, seen = [], set()
    for line in out.splitlines():
        parts = line.split(":")
        ssid = parts[0].strip()
        if not ssid or ssid in seen:
            continue
        seen.add(ssid)
        nets.append({"ssid": ssid,
                     "signal": parts[1] if len(parts) > 1 else "",
                     "security": parts[2] if len(parts) > 2 else ""})
    return nets


def _status():
    rc, ssid, _ = _run(["iwgetid", "-r"], timeout=5)
    relay = None
    try:
        s = json.loads(Path(SIGNALK_SETTINGS).read_text())
        for p in s.get("pipedProviders", []):
            if str(p.get("id", "")).startswith("sg-") and p.get("enabled"):
                relay = p["id"]
    except Exception:  # noqa: BLE001
        pass
    rc, out, _ = _run(["ip", "-4", "-o", "addr", "show"], timeout=5)
    urls = []
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 4 and not f[1].startswith("lo"):
            ip = f[3].split("/")[0]
            urls.append(f"http://{ip}:{PORT}")
    return {"wifi": ssid.strip(), "relay": relay, "urls": urls}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _auth_ok(self):
        if not TOKEN:
            return True
        return parse_qs(urlparse(self.path).query).get("token", [""])[0] == TOKEN

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (
            json.dumps(body).encode() if ctype == "application/json" else body.encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        qs = parse_qs(urlparse(self.path).query)
        if path == "/":
            return self._send(200, PAGE, "text/html")
        if not self._auth_ok():
            return self._send(403, {"error": "bad token"})
        if path == "/api/status":
            return self._send(200, _status())
        if path == "/api/wifi/scan":
            return self._send(200, {"networks": _wifi_scan()})
        if path == "/api/discover":
            sweep = qs.get("sweep", ["0"])[0] == "1"
            cmd = [PY, DISCOVER, "scan", "--json", "--duration", "8"] + (["--sweep"] if sweep else [])
            rc, out, err = _run(cmd, timeout=120)
            try:
                return self._send(200, {"results": json.loads(out)})
            except json.JSONDecodeError:
                return self._send(500, {"error": err or out})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._auth_ok():
            return self._send(403, {"error": "bad token"})
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            body = {}
        if path == "/api/wifi/join":
            ssid, pw = body.get("ssid", ""), body.get("password", "")
            if not ssid:
                return self._send(400, {"error": "ssid required"})
            rc, out, err = _run(["bash", JOIN_WIFI, ssid, pw], timeout=60)
            return self._send(200, {"ok": rc == 0, "output": (out + err).strip()})
        if path == "/api/apply":
            src = body.get("source", "")
            if not src:
                return self._send(400, {"error": "source required"})
            rc, out, err = _run([PY, DISCOVER, "apply", "--source", src, "--duration", "8"],
                                env_extra={"SIGNALK_NODE_SETTINGS": SIGNALK_SETTINGS}, timeout=120)
            return self._send(200, {"ok": rc == 0, "output": (out + err).strip()})
        if path == "/api/capture":
            rc, out, err = _run([PY, DISCOVER, "capture", "--duration", "20",
                                 "--out-dir", "/home/pi/sailingrace-logs/discovery"], timeout=120)
            return self._send(200, {"ok": rc == 0, "output": (out + err).strip()})
        return self._send(404, {"error": "not found"})


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"SailinGrace config UI on http://0.0.0.0:{PORT}  (token {'set' if TOKEN else 'off'})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
