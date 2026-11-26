"""Restart the agent's container when the link carrying traffic changes (WiFi <-> LTE).

Connections opened over the old link are dead after a switch (their source address is gone),
and waiting for them to time out can take minutes. A restart reconnects straight away.
Nothing happens at startup, when the link drops and the same one returns, or more often than
min_interval_seconds.
"""
import threading
import time

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

    def step(self, now, iface):
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
        if previous is None:
            return False            # first link seen since the controller started
        if now - self.last_restart < self.c["min_interval_seconds"]:
            log(f"[agent] link changed {previous} -> {iface}, restart skipped (too soon after the last one)")
            return False
        self.last_restart = now
        self.last_reason = f"link changed {previous} -> {iface}"
        log(f"[agent] {self.last_reason}: restarting {self.c['container']}")
        self.thread = threading.Thread(target=self._restart, daemon=True)
        self.thread.start()
        return True

    def _restart(self):
        rc, out = self.run("docker", "restart", self.c["container"], timeout=90)
        if rc != 0:
            log(f"[agent] docker restart failed: {out.strip()[:200]}")

    def info(self):
        return {"last_restart": self.last_restart or None, "reason": self.last_reason}
