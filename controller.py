#!/usr/bin/env python3
"""MavSphere connectivity controller.

* Detects the links that are up (WiFi / Ethernet / LTE) and which one real
  traffic actually leaves through.
* Writes that to /run/mavsphere-connectivity/state.json (the public contract
  other services, such as the OLED display or the agent, can read).
* If WiFi is missing for a while, starts a setup hotspot with a small web page
  (portal.py) where you pick a home network from your phone.
  Credentials go to hotspot.json (mode 0600), never to state.json.

It also applies the link mode (auto / wifi / lte) and shapes the LTE uplink with CAKE
(see shaper.py). It never touches the MavSphere agent; it only talks to NetworkManager and tc.
"""
import json
import os
import secrets
import signal
import socket
import string
import sys
import threading
import time

from conn_common import (SECRETS_FILE, STATE_DIR, STATE_FILE, label_for,
                         load_config, run, run_rc)
from portal import Portal
from shaper import LinkMode, Shaper, read_mode

LOOP_SECONDS = 3
HOTSPOT_RETRY_AFTER_FAIL = 30
AP_IP = "10.42.0.1"          # must match install.sh's captive-DNS file
PORTAL_PORT = 80
JOIN_DELAY = 2.0             # let the portal's reply reach the phone before the AP goes away


def log(msg):
    print(msg, flush=True)


def write_json(path, data, mode):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def split_nmcli(line):
    """Split an `nmcli -t` line on unescaped ':' (values escape ':' and '\\')."""
    parts, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def wifi_ssid(iface):
    for line in run("iw", "dev", iface, "link").splitlines():
        line = line.strip()
        if line.startswith("SSID:"):
            return line[5:].strip()
    return ""


def egress_iface():
    """Interface a real packet to the internet would leave through right now."""
    p = run("ip", "-4", "route", "get", "1.1.1.1").split()
    return p[p.index("dev") + 1] if "dev" in p else None


def get_links(cfg, skip_ifaces=()):
    links = []
    for line in run("ip", "-4", "-o", "addr", "show", "scope", "global").splitlines():
        p = line.split()
        if len(p) < 4:
            continue
        iface, ip = p[1], p[3].split("/")[0]
        if iface in skip_ifaces or iface.startswith(tuple(cfg["ignore_prefixes"])):
            continue
        label = label_for(cfg, iface)
        links.append({
            "iface": iface,
            "label": label,
            "ip": ip,
            "ssid": wifi_ssid(iface) if label == "WiFi" else "",
        })
    return links


def iface_ipv4(iface):
    for line in run("ip", "-4", "-o", "addr", "show", "dev", iface).splitlines():
        p = line.split()
        if len(p) >= 4:
            return p[3].split("/")[0]
    return ""


class Hotspot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.h = cfg["hotspot"]
        self.wifi = cfg["wifi_iface"]
        self.profile = self.h["profile"]
        self.ssid = self.h["ssid"] or f"{socket.gethostname()}-setup"
        self.password = ""
        self.active = False
        self.ip = ""
        self.networks = []          # scan taken just before the AP went up
        self.setup = {"status": "idle", "target_ssid": "", "message": ""}
        self.busy = False           # True while a join attempt is running
        self.portal = None
        self.no_wifi_since = None
        self.idle_since = None
        self.next_try = 0.0

    # ---- helpers -----------------------------------------------------
    def clients(self):
        out = run("iw", "dev", self.wifi, "station", "dump")
        return sum(1 for line in out.splitlines() if line.startswith("Station "))

    def scan(self):
        rc, out = run_rc("nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                         "ifname", self.wifi, "--rescan", "yes", timeout=30)
        best = {}
        if rc == 0:
            for line in out.splitlines():
                parts = split_nmcli(line)
                if len(parts) < 3 or not parts[0]:
                    continue
                try:
                    sig = int(parts[1] or 0)
                except ValueError:
                    sig = 0
                secure = parts[2] not in ("", "--")
                if parts[0] not in best or sig > best[parts[0]]["signal"]:
                    best[parts[0]] = {"ssid": parts[0], "signal": sig, "secure": secure}
        return sorted(best.values(), key=lambda n: -n["signal"])[:20]

    def profile_exists(self):
        rc, out = run_rc("nmcli", "-t", "-f", "NAME", "connection", "show")
        return rc == 0 and self.profile in out.splitlines()

    def _new_password(self):
        alphabet = string.ascii_letters + string.digits
        alphabet = alphabet.translate({ord(c): None for c in "0Oo1lI"})  # easy to read on a tiny OLED
        return "".join(secrets.choice(alphabet) for _ in range(10))

    # ---- hotspot lifecycle -------------------------------------------
    def start(self, now, reuse_password=False):
        self.networks = self.scan()     # the single radio cannot scan once it is an AP
        if not (reuse_password and self.password):
            self.password = self._new_password()
        run_rc("nmcli", "connection", "delete", self.profile)
        rc, out = run_rc(
            "nmcli", "connection", "add", "type", "wifi", "ifname", self.wifi,
            "con-name", self.profile, "autoconnect", "no", "ssid", self.ssid,
            "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
            "ipv4.method", "shared", "ipv4.addresses", f"{AP_IP}/24", "ipv6.method", "disabled",
            "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.proto", "rsn",
            "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp", "wifi-sec.psk", self.password)
        if rc == 0:
            rc, out = run_rc("nmcli", "connection", "up", self.profile, timeout=40)
        if rc != 0:
            log(f"[hotspot] start failed: {out.strip()}")
            run_rc("nmcli", "connection", "delete", self.profile)
            self.password = ""
            self.next_try = now + HOTSPOT_RETRY_AFTER_FAIL
            return
        self.active = True
        self.idle_since = now
        self.ip = iface_ipv4(self.wifi) or AP_IP
        self._start_portal()
        log(f"[hotspot] started ssid={self.ssid} ip={self.ip}")

    def _start_portal(self):
        try:
            self.portal = Portal(self.ip, PORTAL_PORT, self.cfg["agent_ui_port"],
                                 lambda: self.networks, lambda: self.setup.get("message", ""),
                                 self.request_join)
            self.portal.start()
        except OSError as e:
            log(f"[portal] could not start on {self.ip}:{PORTAL_PORT}: {e}")
            self.portal = None

    def _teardown_ap(self):
        if self.portal:
            self.portal.stop()
            self.portal = None
        run_rc("nmcli", "connection", "down", self.profile)
        run_rc("nmcli", "connection", "delete", self.profile)
        self.active = False
        self.ip = ""
        self.idle_since = None

    def stop(self, now=None, reconnect=True):
        if not self.active and not self.profile_exists():
            return
        self._teardown_ap()
        self.password = ""
        self.setup = {"status": "idle", "target_ssid": "", "message": ""}
        if reconnect:
            run_rc("nmcli", "device", "connect", self.wifi, timeout=30)
        self.no_wifi_since = now
        log("[hotspot] stopped")

    # ---- joining a home network from the portal ----------------------
    def request_join(self, ssid, password):
        if self.busy:
            return False
        self.busy = True
        self.setup = {"status": "connecting", "target_ssid": ssid, "message": ""}
        threading.Thread(target=self._join, args=(ssid, password), daemon=True).start()
        return True

    def _join(self, ssid, password):
        try:
            time.sleep(JOIN_DELAY)
            keep_password = self.password
            self._teardown_ap()
            name = f"mavsphere-{ssid}"[:60]
            run_rc("nmcli", "connection", "delete", name)  # drop an earlier attempt with an old password
            args = ["nmcli", "-w", "30", "device", "wifi", "connect", ssid,
                    "ifname", self.wifi, "name", name]
            if password:
                args += ["password", password]
            rc, out = run_rc(*args, timeout=50)
            deadline = time.time() + 10
            while rc == 0 and not iface_ipv4(self.wifi) and time.time() < deadline:
                time.sleep(0.5)
            if rc == 0 and iface_ipv4(self.wifi):
                log(f"[join] connected to {ssid!r}")
                self.password = ""
                self.setup = {"status": "idle", "target_ssid": "", "message": ""}
                self.no_wifi_since = None
            else:
                log(f"[join] failed for {ssid!r}: {out.strip()[:200]}")
                run_rc("nmcli", "connection", "delete", name)
                self.password = keep_password
                self.setup = {"status": "failed", "target_ssid": ssid,
                              "message": f"Could not join {ssid}. Check the password and try again."}
                self.start(time.time(), reuse_password=True)
        except Exception as e:
            log(f"[join] error: {e!r}")
        finally:
            self.busy = False

    # ---- state machine -----------------------------------------------
    def step(self, wifi_up, now):
        if not self.h["enabled"] or self.busy:
            return
        if self.active:
            self.ip = iface_ipv4(self.wifi) or self.ip
            if self.clients() > 0 or self.idle_since is None:
                self.idle_since = now
            elif now - self.idle_since >= self.h["retry_seconds"]:
                log("[hotspot] idle, retrying home wifi")
                self.stop(now)
            return
        if wifi_up:
            self.no_wifi_since = None
            return
        if self.no_wifi_since is None:
            self.no_wifi_since = now
        if now - self.no_wifi_since >= self.h["grace_seconds"] and now >= self.next_try:
            self.start(now)


def build_state(cfg, hotspot):
    skip = (cfg["wifi_iface"],) if hotspot.active else ()
    links = get_links(cfg, skip)
    egress = egress_iface()
    if hotspot.active and egress == cfg["wifi_iface"]:
        egress = None  # AP-side address, not a path to the internet
    for link in links:
        link["default"] = link["iface"] == egress
    egress_label = next((l["label"] for l in links if l["default"]), None)
    active = hotspot.active
    state = {
        "version": 1,
        "timestamp": time.time(),
        "hostname": socket.gethostname(),
        "egress": {"iface": egress, "label": egress_label},
        "links": links,
        "hotspot": {
            "active": active,
            "ssid": hotspot.ssid if active else "",
            "ip": hotspot.ip,
            "portal_url": f"http://{hotspot.ip}/" if active and hotspot.ip else "",
            "ui_url": f"http://{hotspot.ip}:{cfg['agent_ui_port']}/" if active and hotspot.ip else "",
        },
        "setup": dict(hotspot.setup),
    }
    wifi_up = any(l["label"] == "WiFi" for l in links)
    return state, wifi_up


def step(cfg, hotspot, now=None, link=None, shaper=None):
    now = time.time() if now is None else now
    state, wifi_up = build_state(cfg, hotspot)
    hotspot.step(wifi_up, now)
    if hotspot.active != state["hotspot"]["active"]:  # started/stopped this tick
        state, _ = build_state(cfg, hotspot)
    mode = read_mode()
    state["link_mode"] = mode
    if link is not None:
        link.step(now, state["links"], mode)
    if shaper is not None:
        state["lte"] = shaper.step(now, state["links"], state["egress"]["iface"], mode)
    write_json(STATE_FILE, state, 0o644)
    write_json(SECRETS_FILE, {"password": hotspot.password if hotspot.active else ""}, 0o600)
    return state


def main():
    cfg = load_config()
    hotspot = Hotspot(cfg)
    link, shaper = LinkMode(cfg), Shaper(cfg)
    # Never come up in hotspot mode just because a stale profile survived a reboot.
    hotspot.stop(reconnect=False)

    def shutdown(*_):
        shaper.clear()
        hotspot.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log("[connectivity] controller started")
    while True:
        try:
            step(cfg, hotspot, link=link, shaper=shaper)
        except Exception as e:  # keep running whatever happens
            log(f"[connectivity] loop error: {e!r}")
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
