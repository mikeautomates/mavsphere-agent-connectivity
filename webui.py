"""Connectivity web page (default port 8091): link mode, LTE speed test + bitrate hint,
setup hotspot and WiFi settings. Runs inside the controller; needs the password from
`sudo mavsphere-conn ui-password`. Plain HTTP, so keep it to networks you trust.

GET /api/lte-hint is answered only to the Pi itself (no login) so the agent can show the
recommended video bitrate.
"""
import hmac
import html
import json
import os
import secrets
import string
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import conn_common as cc
from portal import STYLE, validate

SESSION_SECONDS = 12 * 3600
MAX_FAILS, LOCKOUT = 5, 60
MODES = ("auto", "wifi", "lte")
MODE_HELP = {"auto": "WiFi preferred, LTE as fallback",
             "wifi": "WiFi only - LTE never carries traffic",
             "lte": "LTE even when WiFi is up"}
MESSAGES = {"mode": "Mode changed. It applies within about 10 seconds.",
            "speedtest": "Speed test requested. It takes up to 30 seconds and uses about 3 MB of LTE data.",
            "hotspot": "Setup hotspot toggled. WiFi may drop while it is on.",
            "badmode": "Unknown mode."}
EXTRA = """
nav a{margin-right:14px}.card{background:#1b2128;border:1px solid #334;border-radius:10px;padding:12px 14px;margin:12px 0}
.row{display:flex;gap:8px;flex-wrap:wrap}.row form{flex:1;min-width:90px}
button.sel{background:#1f8f4e}button.sec{background:#34404f}table{width:100%;border-collapse:collapse}
td{padding:3px 6px 3px 0;vertical-align:top}.k{color:#889}.big{font-size:1.4em;font-weight:700}
.flash{background:#1d3a24;border:1px solid #3a8;padding:10px;border-radius:8px;margin:10px 0}
body{max-width:640px}
"""


def page(title, body, refresh=0):
    meta = f"<meta http-equiv=refresh content={refresh}>" if refresh else ""
    return ("<!doctype html><html><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>" + meta +
            f"<title>{html.escape(title)}</title><style>{STYLE}{EXTRA}</style></head><body>{body}</body></html>")


def esc(v):
    return html.escape(str(v))


def new_password():
    alphabet = (string.ascii_letters + string.digits).translate({ord(c): None for c in "0Oo1lI"})
    return "".join(secrets.choice(alphabet) for _ in range(12))


def ensure_password(path=None, reset=False):
    path = path or cc.UI_PASSWORD_FILE
    if not reset:
        try:
            with open(path) as f:
                pw = f.read().strip()
            if pw:
                return pw
        except OSError:
            pass
    pw = new_password()
    cc.write_atomic(path, pw + "\n", 0o600)
    return pw


def bars_text(sig):
    if not sig:
        return "unknown"
    return f"{sig['bars']}/5" + (f" ({sig['dbm']} dBm)" if sig.get("dbm") is not None else "")


def fmt_kbps(v):
    return f"{v / 1000:.1f} Mbit/s" if v >= 1000 else f"{v} kbit/s"


def lte_hint(state):
    """Small public summary for the agent's UI."""
    lte = (state or {}).get("lte") or {}
    return {"egress": ((state or {}).get("egress") or {}).get("label"),
            "mode": (state or {}).get("link_mode", "auto"),
            "upload_kbps": lte.get("upload_kbps"),
            "recommended_video_kbps": lte.get("recommended_video_kbps"),
            "measured_at": lte.get("measured_at")}


class WebUI:
    def __init__(self, cfg, hotspot, get_state):
        self.cfg, self.hotspot, self.get_state = cfg, hotspot, get_state
        self.u = cfg["ui"]
        self.password = ensure_password()
        self.sessions = {}
        self.fails = {}
        self.lock = threading.Lock()
        self.server = None

    # ---- auth --------------------------------------------------------
    def check_login(self, ip, password):
        now = time.time()
        with self.lock:
            count, until = self.fails.get(ip, (0, 0))
            if until > now:
                return None
            self.password = ensure_password()          # pick up a reset done with the CLI
            if hmac.compare_digest(password.encode(), self.password.encode()):
                self.fails.pop(ip, None)
                token = secrets.token_urlsafe(24)
                self.sessions[token] = now + SESSION_SECONDS
                return token
            count += 1
            self.fails[ip] = (count, now + LOCKOUT if count >= MAX_FAILS else 0)
            if count >= MAX_FAILS:
                self.fails[ip] = (0, now + LOCKOUT)
            return None

    def locked(self, ip):
        with self.lock:
            return self.fails.get(ip, (0, 0))[1] > time.time()

    def valid(self, token):
        with self.lock:
            exp = self.sessions.get(token or "")
            if exp and exp > time.time():
                return True
            self.sessions.pop(token or "", None)
            return False

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token or "", None)

    # ---- pages -------------------------------------------------------
    def login_page(self, error=""):
        err = f"<div class=err>{esc(error)}</div>" if error else ""
        return page("Sign in", f"""<h1>{esc(os.uname().nodename)}</h1><p>Connectivity settings</p>{err}
<form method=post action=/login><label>Password</label><input type=password name=password autofocus>
<button type=submit>Sign in</button></form>
<p><small>The password is shown by <code>sudo mavsphere-conn ui-password</code> on the device.</small></p>""")

    def status_page(self, host, flash=""):
        s = self.get_state() or {}
        lte = s.get("lte") or {}
        mode = s.get("link_mode", "auto")
        label = (s.get("egress") or {}).get("label") or "NO NETWORK"
        hotspot_on = (s.get("hotspot") or {}).get("active")
        fl = f"<div class=flash>{esc(MESSAGES[flash])}</div>" if flash in MESSAGES else ""
        modes = "".join(
            f"<form method=post action=/mode><input type=hidden name=mode value={m}>"
            f"<button class={'sel' if m == mode else 'sec'}>{m.upper()}</button></form>" for m in MODES)
        links = "".join(
            f"<tr><td>{'<b>*</b>' if l.get('default') else ''}</td><td>{esc(l['label'])}</td>"
            f"<td>{esc(l['ip'])}</td><td class=k>{esc(l.get('ssid', ''))}</td></tr>" for l in s.get("links", []))
        if lte.get("present") or lte.get("signal"):
            up = lte.get("upload_kbps")
            age = ""
            if lte.get("measured_at"):
                age = f" ({int((time.time() - lte['measured_at']) / 60)} min ago)"
            sh = lte.get("shaping") or {}
            usage = lte.get("usage") or {}
            rec = lte.get("recommended_video_kbps")
            lte_html = f"""<div class=card><b>LTE</b><table>
<tr><td class=k>Signal</td><td>{esc(bars_text(lte.get('signal')))}</td></tr>
<tr><td class=k>Upload speed</td><td>{esc(fmt_kbps(up) + age) if up else 'not measured yet'}</td></tr>
<tr><td class=k>Traffic shaping</td><td>{esc(sh.get('status', 'off'))}{(' at ' + fmt_kbps(sh['kbit'])) if sh.get('kbit') else ''}</td></tr>
<tr><td class=k>Today's data</td><td>{esc(usage.get('total_mb', 0))} MB (up {esc(usage.get('tx_mb', 0))}, down {esc(usage.get('rx_mb', 0))})</td></tr>
<tr><td class=k>Right now</td><td>up {esc(fmt_kbps(lte.get('tx_kbps', 0)))}, down {esc(fmt_kbps(lte.get('rx_kbps', 0)))}</td></tr>
</table></div>
<div class=card><b>Video bitrate on LTE</b><br>{
    f"<span class=big>{esc(fmt_kbps(rec))}</span><br><small>Set the agent's maximum video bitrate to about {rec * 1000} bps "
    f"(<code>webrtcMaxBitrateBps</code>) when using LTE. It is {int(self.cfg['lte']['video_share'] * 100)}% of the measured upload, "
    "leaving room for control and telemetry.</small>" if rec else "Run a speed test to get a recommendation."}</div>"""
        else:
            lte_html = "<div class=card><b>LTE</b><br>No LTE modem or link found.</div>"
        agent = s.get("agent") or {}
        restart = ""
        if agent.get("last_restart"):
            restart = (f"<p><small>Agent last restarted {int((time.time() - agent['last_restart']) / 60)} min ago: "
                       f"{esc(agent.get('reason', ''))}</small></p>")
        agent_url = f"http://{esc(host)}:{self.cfg['agent_ui_port']}/"
        body = f"""<h1>{esc(s.get('hostname', os.uname().nodename))}</h1>
<nav><a href=/>Status</a><a href=/wifi>WiFi</a><a href='{agent_url}'>Agent settings</a>
<form method=post action=/logout style='display:inline'><button class=sec style='width:auto;padding:4px 10px;margin:0'>Sign out</button></form></nav>
{fl}<div class=card><span class=k>Carrying traffic</span><br><span class=big>{esc(label)}</span>
<table>{links}</table></div>
<div class=card><b>Link mode</b><div class=row>{modes}</div>
<p><small>{' &middot; '.join(f'<b>{m}</b>: {esc(MODE_HELP[m])}' for m in MODES)}</small></p>
<p><small>When the link changes, the agent restarts itself to reconnect quickly.</small></p></div>
{lte_html}
<div class=card><b>Actions</b><div class=row>
<form method=post action=/speedtest><button class=sec>Run LTE speed test</button></form>
<form method=post action=/hotspot onsubmit="return confirm('The setup hotspot uses the WiFi radio, so WiFi will drop while it is on. Continue?')">
<button class=sec>{'Stop' if hotspot_on else 'Start'} setup hotspot</button></form></div></div>{restart}"""
        return page("Connectivity", body, refresh=8)

    def wifi_page(self, host, message="", rescan=False):
        nets = self.hotspot.networks_for_ui(rescan=rescan)
        s = self.get_state() or {}
        current = next((l.get("ssid") for l in s.get("links", []) if l["label"] == "WiFi" and l.get("ssid")), "")
        opts = ["<option value=''>(not listed - type it below)</option>"] + [
            f"<option value='{esc(n['ssid'])}'>{esc(n['ssid'])} ({n['signal']}%, {'secured' if n['secure'] else 'open'})</option>"
            for n in nets]
        banner = f"<div class=err>{esc(message)}</div>" if message else ""
        body = f"""<h1>WiFi</h1><nav><a href=/>Status</a></nav>{banner}
<p>Connected to: <b>{esc(current or 'no WiFi')}</b></p>
<form method=get action=/wifi><input type=hidden name=rescan value=1><button class=sec>Rescan networks</button></form>
<form method=post action=/wifi/join onsubmit="return confirm('Joining a network drops the current WiFi connection and this page. If it fails the device returns to its saved WiFi. Continue?')">
<label>Network</label><select name=ssid>{''.join(opts)}</select>
<label>Or type the network name</label><input type=text name=ssid_manual autocapitalize=off autocorrect=off>
<label>Password</label><input type=password name=password autocomplete=off>
<button type=submit>Join</button></form>
<p><small>If joining works, the device may get a new address; check the screen on the device.</small></p>"""
        return page("WiFi", body)

    # ---- server ------------------------------------------------------
    def _handler(self):
        ui = self

        class H(BaseHTTPRequestHandler):
            server_version = "MavConnUI"

            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            def _redirect(self, to, headers=None):
                self._send(303, "", headers={"Location": to, **(headers or {})})

            def _token(self):
                for part in (self.headers.get("Cookie") or "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == "mc_session":
                        return v
                return ""

            def _host(self):
                return (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]") or "localhost"

            def _authed(self):
                return ui.valid(self._token())

            def _origin_ok(self):
                origin = self.headers.get("Origin")
                return not origin or urlparse(origin).netloc == self.headers.get("Host")

            def _form(self):
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = 0
                if n < 0 or n > 4096:
                    return {}
                return {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode("utf-8", "replace"),
                                                     keep_blank_values=True).items()}

            def do_GET(self):
                u = urlparse(self.path)
                if u.path == "/api/lte-hint":
                    if self.client_address[0] not in ("127.0.0.1", "::1"):
                        return self._send(403, "{}", "application/json")
                    return self._send(200, json.dumps(lte_hint(ui.get_state())), "application/json")
                if u.path == "/login":
                    return self._send(200, ui.login_page())
                if not self._authed():
                    return self._redirect("/login")
                q = parse_qs(u.query)
                if u.path == "/":
                    return self._send(200, ui.status_page(self._host(), (q.get("m") or [""])[0]))
                if u.path == "/wifi":
                    return self._send(200, ui.wifi_page(self._host(), rescan="rescan" in q))
                if u.path == "/api/status":
                    return self._send(200, json.dumps(ui.get_state() or {}), "application/json")
                self._send(404, page("Not found", "<h1>Not found</h1><a href=/>Back</a>"))

            do_HEAD = do_GET

            def do_POST(self):
                path = urlparse(self.path).path
                if not self._origin_ok():
                    return self._send(403, page("Refused", "<h1>Refused</h1>"))
                form = self._form()
                if path == "/login":
                    ip = self.client_address[0]
                    if ui.locked(ip):
                        return self._send(429, ui.login_page("Too many attempts. Wait a minute."))
                    token = ui.check_login(ip, form.get("password", ""))
                    if not token:
                        return self._send(401, ui.login_page("Wrong password."))
                    return self._redirect("/", {"Set-Cookie": f"mc_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}"})
                if not self._authed():
                    return self._redirect("/login")
                if path == "/logout":
                    ui.logout(self._token())
                    return self._redirect("/login", {"Set-Cookie": "mc_session=; Max-Age=0; Path=/"})
                if path == "/mode":
                    mode = form.get("mode", "")
                    if mode not in MODES:
                        return self._redirect("/?m=badmode")
                    cc.write_atomic(cc.MODE_FILE, mode + "\n")
                    return self._redirect("/?m=mode")
                if path == "/speedtest":
                    cc.add_request("speedtest")
                    return self._redirect("/?m=speedtest")
                if path == "/hotspot":
                    cc.add_request("hotspot")
                    return self._redirect("/?m=hotspot")
                if path == "/wifi/join":
                    ssid = (form.get("ssid_manual", "").strip() or form.get("ssid", ""))
                    password = form.get("password", "")
                    err = validate(ssid, password, ui.hotspot.networks_for_ui())
                    if err:
                        return self._send(400, ui.wifi_page(self._host(), err))
                    if not ui.hotspot.request_join(ssid, password):
                        return self._send(409, ui.wifi_page(self._host(), "Already connecting - please wait."))
                    return self._send(200, page("Connecting", f"<h1>Connecting&hellip;</h1><div class=flash>Joining <b>{esc(ssid)}</b>.</div>"
                                                "<p>You may lose this page. If it fails the device goes back to its saved WiFi.</p>"))
                self._send(404, page("Not found", "<h1>Not found</h1>"))

        return H

    def start(self):
        self.server = ThreadingHTTPServer((self.u["bind"], self.u["port"]), self._handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        print(f"[ui] listening on {self.u['bind']}:{self.u['port']}", flush=True)

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
