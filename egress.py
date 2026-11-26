"""Restart the agent's container when the link carrying traffic changes (WiFi <-> LTE).

Connections opened over the old link are dead after a switch (their source address is gone),
and waiting for them to time out can take minutes. A restart reconnects straight away.
Nothing happens at startup, when the link drops and the same one returns, or more often than
min_interval_seconds.
"""
import json
import threading
import time
import urllib.request

from conn_common import run_rc


def log(msg):
    print(msg, flush=True)


class EgressWatcher:
    def __init__(self, cfg, runner=run_rc):
        self.c = cfg["agent"]
        self.run = runner
        self.stable = None          # last settled egress iface
        self.candidate = None
        self.candidate_since = 0.0
        self.last_restart = 0.0
        self.last_reason = ""
        self.thread = None
        self.bitrate_bps = None     # what we last wrote into the agent's config

    # ---- per-link video bitrate -------------------------------------
    def wanted_bps(self, label):
        b = self.c.get("bitrate", {})
        if not b.get("enabled") or label not in ("WiFi", "LTE"):
            return None
        v = int(b["lte_bps"] if label == "LTE" else b["wifi_bps"])
        return max(100_000, v)          # the agent rejects anything below 100 kbit/s

    def apply_bitrate(self, label):
        """Write the bitrate for this link into the agent's config. Returns True if it changed."""
        want = self.wanted_bps(label)
        if want is None:
            return False
        url = self.c["bitrate"]["api"].rstrip("/") + "/api/config"
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                conf = json.load(r)
            if conf.get("h264BitrateBps") == want and conf.get("webrtcMaxBitrateBps") == want:
                self.bitrate_bps = want
                return False
            conf.pop("_revision", None)
            conf["h264BitrateBps"] = want
            conf["webrtcMaxBitrateBps"] = want
            req = urllib.request.Request(
                url, data=json.dumps(conf).encode(), method="PUT",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception as e:
            log(f"[agent] could not set video bitrate for {label}: {e}")
            return False
        self.bitrate_bps = want
        log(f"[agent] video bitrate set to {want} bit/s for {label}")
        return True

    def step(self, now, iface, label=None):
        """Call every loop with the current egress interface (or None). Returns True if it restarted."""
        if not self.c["restart_on_egress_change"]:
            return False
        if iface is None or iface == self.stable:
            self.candidate = None
            return False
        if iface != self.candidate:
            self.candidate, self.candidate_since = iface, now
            return False
        if now - self.candidate_since < self.c["settle_seconds"]:
            return False
        previous, self.stable, self.candidate = self.stable, iface, None
        if previous is None:        # first link seen since the controller started:
            if self.wanted_bps(label) is not None:      # restart only if the bitrate is wrong
                self.last_reason = f"video bitrate for {label}"
                self.thread = threading.Thread(target=self._restart, args=(label, True), daemon=True)
                self.thread.start()
                return True
            return False
        if now - self.last_restart < self.c["min_interval_seconds"]:
            log(f"[agent] link changed {previous} -> {iface}, restart skipped (too soon after the last one)")
            return False
        self.last_restart = now
        self.last_reason = f"link changed {previous} -> {iface}"
        log(f"[agent] {self.last_reason}: restarting {self.c['container']}")
        self.thread = threading.Thread(target=self._restart, args=(label, False), daemon=True)
        self.thread.start()
        return True

    def _restart(self, label=None, only_if_changed=False):
        changed = self.apply_bitrate(label)
        if only_if_changed and not changed:
            return
        rc, out = self.run("docker", "restart", self.c["container"], timeout=90)
        if rc != 0:
            log(f"[agent] docker restart failed: {out.strip()[:200]}")

    def info(self):
        return {"last_restart": self.last_restart or None, "reason": self.last_reason}
