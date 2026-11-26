"""Tiny captive-portal style page for choosing a home WiFi network (stdlib only).

Runs only while the setup hotspot is up and is bound to the hotspot address,
so it is never reachable from your home network.
"""
import html
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

STYLE = """
body{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#101418;color:#e8eaed;max-width:480px}
h1{font-size:1.3em;margin:0 0 4px}p{color:#aab;margin:6px 0 14px}
label{display:block;margin:14px 0 4px;font-weight:600}
select,input[type=text],input[type=password]{width:100%;box-sizing:border-box;padding:12px;font-size:1em;
border-radius:8px;border:1px solid #445;background:#1b2128;color:#e8eaed}
button{width:100%;margin-top:18px;padding:14px;font-size:1.05em;border:0;border-radius:8px;background:#2f7bff;color:#fff}
.err{background:#4a1d1d;border:1px solid #a33;padding:10px;border-radius:8px;margin-bottom:12px}
.ok{background:#1d3a24;border:1px solid #3a8;padding:10px;border-radius:8px}
a{color:#7fb0ff}small{color:#889}
"""


def page(body):
    return ("<!doctype html><html><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>Pi WiFi setup</title><style>{STYLE}</style></head><body>{body}</body></html>")


def validate(ssid, password, networks):
    """Return an error string, or None if the request looks OK."""
    if not ssid or len(ssid.encode("utf-8")) > 32:
        return "Network name must be 1-32 characters."
    if ssid.startswith("-") or "\x00" in ssid or "\x00" in password:
        return "That network name or password has characters this page can't use."
    if password and not 8 <= len(password) <= 63:
        return "WiFi passwords are 8-63 characters."
    known = next((n for n in networks if n["ssid"] == ssid), None)
    if known and known["secure"] and not password:
        return "That network needs a password."
    return None


class Portal:
    def __init__(self, ip, port, agent_port, get_networks, get_message, on_join):
        self.ip, self.port, self.agent_port = ip, port, agent_port
        self.get_networks, self.get_message, self.on_join = get_networks, get_message, on_join
        self.server = None
        self.thread = None

    # ---- pages -------------------------------------------------------
    def form_page(self, error=""):
        nets = self.get_networks()
        opts = ["<option value=''>(not listed - type it below)</option>"]
        for n in nets:
            tag = "secured" if n["secure"] else "open"
            opts.append(f"<option value='{html.escape(n['ssid'], quote=True)}'>"
                        f"{html.escape(n['ssid'])} ({n['signal']}%, {tag})</option>")
        banner = error or self.get_message()
        banner_html = f"<div class=err>{html.escape(banner)}</div>" if banner else ""
        agent = f"http://{self.ip}:{self.agent_port}/"
        return page(f"""
<h1>WiFi setup</h1><p>Choose the network this device should use.</p>{banner_html}
<form method=post action=/connect>
<label>Network</label><select name=ssid>{''.join(opts)}</select>
<label>Or type the network name</label><input type=text name=ssid_manual autocapitalize=off autocorrect=off>
<label>Password</label><input type=password name=password id=pw autocomplete=off>
<label style='font-weight:400'><input type=checkbox onclick="document.getElementById('pw').type=this.checked?'text':'password'"> Show password</label>
<button type=submit>Connect</button></form>
<p style='margin-top:22px'><small>Agent settings: <a href='{agent}'>{html.escape(agent)}</a></small></p>""")

    @staticmethod
    def connecting_page(ssid):
        return page(f"""<h1>Connecting&hellip;</h1>
<div class=ok>Trying to join <b>{html.escape(ssid)}</b>.</div>
<p>This setup network will disappear while the device connects, so your phone will drop off it.
Reconnect your phone to its normal WiFi or mobile data and check the display on the device.</p>
<p>If it can't join (for example a wrong password), this setup network comes back with the
same password and you can try again.</p>""")

    # ---- server ------------------------------------------------------
    def _handler(self):
        portal = self

        class H(BaseHTTPRequestHandler):
            server_version = "MavConn"

            def log_message(self, *args):  # keep passwords out of logs
                pass

            def _send(self, code, body, extra=None):
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            def _redirect(self):
                self._send(302, "", {"Location": f"http://{portal.ip}/"})

            def do_GET(self):
                if self.path.split("?")[0] in ("/", "/index.html"):
                    self._send(200, portal.form_page())
                else:  # captive-portal probes (generate_204, hotspot-detect, ...) land here
                    self._redirect()

            do_HEAD = do_GET

            def do_POST(self):
                if self.path.split("?")[0] != "/connect":
                    return self._redirect()
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                if length <= 0 or length > 4096:
                    return self._send(413, portal.form_page("Request too large."))
                form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)
                manual = form.get("ssid_manual", [""])[0].strip()
                ssid = manual or form.get("ssid", [""])[0]
                password = form.get("password", [""])[0]
                err = validate(ssid, password, portal.get_networks())
                if err:
                    return self._send(400, portal.form_page(err))
                if not portal.on_join(ssid, password):
                    return self._send(409, portal.form_page("Already connecting - please wait."))
                self._send(200, portal.connecting_page(ssid))

        return H

    def start(self):
        self.server = ThreadingHTTPServer((self.ip, self.port), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
