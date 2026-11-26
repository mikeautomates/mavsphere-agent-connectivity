"""Link mode (auto / wifi / lte), LTE upload measurement and CAKE shaping.

Used by controller.py. Nothing here touches the MavSphere agent. The agent (or anything
else) can read the results from state.json: ``link_mode`` and ``lte``.
"""
import json
import os
import tempfile
import threading
import time

import conn_common as cc
from conn_common import run, run_rc

MODES = ("auto", "wifi", "lte")


def log(msg):
    print(msg, flush=True)


def read_mode():
    try:
        with open(cc.MODE_FILE) as f:
            m = f.read().strip().lower()
    except OSError:
        return "auto"
    return m if m in MODES else "auto"


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


class LinkMode:
    """Makes NetworkManager's route metrics match the chosen mode.

    auto: WiFi preferred, LTE is the fallback (metrics 600 / 700).
    lte:  LTE beats WiFi (metric 50) - for testing the modem or when WiFi is poor.
    wifi: LTE gets no default route, so it can never carry traffic.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.last = 0.0
        self.last_mode = None

    def wanted(self, mode, label):
        c = self.cfg["link"]
        if label == "WiFi":
            return str(c["wifi_metric"]), "no"
        if mode == "lte":
            return str(c["forced_lte_metric"]), "no"
        return str(c["lte_metric"]), "yes" if mode == "wifi" else "no"

    def step(self, now, links, mode):
        if mode == self.last_mode and now - self.last < 10:
            return []
        self.last, self.last_mode = now, mode
        by_dev = {l["iface"]: l["label"] for l in links if l["label"] in ("WiFi", "LTE")}
        changed = []
        skip = self.cfg["hotspot"]["profile"]
        for line in run("nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active").splitlines():
            if ":" not in line:
                continue
            name, dev = line.rsplit(":", 1)
            name = name.replace("\\:", ":")
            if dev not in by_dev or name == skip:
                continue
            metric, never = self.wanted(mode, by_dev[dev])
            cur = run("nmcli", "-g", "ipv4.route-metric,ipv4.never-default", "connection", "show", name).split("\n")
            cur = [x.strip() for x in cur if x.strip()] if cur else []
            if len(cur) >= 2 and cur[0] == metric and cur[1] == never:
                continue
            rc, out = run_rc("nmcli", "connection", "modify", name, "ipv4.route-metric", metric,
                             "ipv4.never-default", never)
            if rc != 0:
                log(f"[link] could not modify {name}: {out.strip()}")
                continue
            run_rc("nmcli", "device", "reapply", dev)
            changed.append((name, metric, never))
            log(f"[link] mode={mode}: {name} metric={metric} never-default={never}")
        return changed


class Shaper:
    def __init__(self, cfg):
        self.cfg = cfg
        self.sh = cfg["lte"]["shaping"]
        self.st = cfg["lte"]["speedtest"]
        self.speed = cc.read_json(cc.SPEED_FILE) or {}
        self.applied = None             # (iface, kbit) we installed
        self.last_check = 0.0
        self.test_state = "idle"
        self.test_thread = None
        self.test_result = None
        self.last_test = 0.0
        self.was_active = False
        self.tx_prev = None
        self.tx_kbit = 0.0
        self.shape_status = "off"
        self.module_ok = None

    # ---- measurement -------------------------------------------------
    def _today(self):
        return time.strftime("%Y-%m-%d")

    def _used_today(self):
        return self.speed.get("day_bytes", 0) if self.speed.get("day") == self._today() else 0

    def _save(self):
        try:
            cc.write_atomic(cc.SPEED_FILE, json.dumps(self.speed))
        except OSError as e:
            log(f"[lte] cannot save speed file: {e}")

    def _sample_tx(self, iface, now):
        try:
            with open(f"/sys/class/net/{iface}/statistics/tx_bytes") as f:
                b = int(f.read())
        except (OSError, ValueError):
            return
        if self.tx_prev and now > self.tx_prev[0]:
            self.tx_kbit = max(0.0, (b - self.tx_prev[1]) * 8 / 1000 / (now - self.tx_prev[0]))
        self.tx_prev = (now, b)

    def _run_test(self, iface):
        st = self.st
        path = None
        try:
            # Measure the link, not our own limit: lift CAKE for the test.
            if self.applied and self.applied[0] == iface:
                run_rc("tc", "qdisc", "replace", "dev", iface, "root", "cake",
                       "bandwidth", "100000kbit", *self.sh["options"].split())
            fd, path = tempfile.mkstemp(prefix="lte-up-")
            with os.fdopen(fd, "wb") as f:
                f.write(os.urandom(int(st["bytes"])))
            rc, out = run_rc("curl", "-s", "-o", "/dev/null", "--interface", iface,
                             "--max-time", str(st["max_seconds"]), "-X", "POST",
                             "-H", "Content-Type: application/octet-stream",
                             "--data-binary", "@" + path,
                             "-w", "%{size_upload} %{time_total} %{time_appconnect}", st["url"],
                             timeout=int(st["max_seconds"]) + 10)
            parts = out.split()
            sent, total, conn = float(parts[-3]), float(parts[-2]), float(parts[-1])
            dur = total - conn
            if sent < 200000 or dur <= 0.2:
                raise ValueError(f"only {int(sent)} bytes in {dur:.1f}s (curl rc={rc})")
            self.test_result = ("ok", int(sent * 8 / dur / 1000), int(sent))
        except Exception as e:
            self.test_result = ("failed", str(e), 0)
        finally:
            self.applied = None                      # force CAKE to be re-installed at the new rate
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def _finish_test(self, now):
        status, val, sent = self.test_result
        self.test_result = None
        self.last_test = now
        self.speed["day"] = self._today()
        self.speed["day_bytes"] = self._used_today() + sent
        if status == "ok":
            old = self.speed.get("upload_kbps")
            new = val
            if old and new > old:                 # be slow to trust an improvement
                new = int(old + (new - old) * 0.5)
            self.speed.update(upload_kbps=new, raw_kbps=val, measured_at=time.time())
            self.test_state = "idle"
            log(f"[lte] upload test: {val} kbit/s measured, using {new}")
        else:
            self.test_state = "failed"
            self.last_test = now - self.st["retest_minutes"] * 60 + 300   # try again in ~5 min
            log(f"[lte] upload test failed: {val}")
        self._save()

    def maybe_test(self, now, iface, active, mode, requested):
        if self.test_thread and not self.test_thread.is_alive():
            self.test_thread = None
            if self.test_result:
                self._finish_test(now)
        if self.test_thread:
            return
        if not self.st["enabled"] and not requested:
            self.test_state = "disabled"
            return
        if mode == "wifi":
            self.test_state = "skipped"             # LTE has no default route in wifi-only mode
            return
        due = requested
        if not due and active:
            due = (not self.was_active or not self.speed.get("upload_kbps")
                   or now - self.last_test >= self.st["retest_minutes"] * 60)
        if not due:
            return
        if not requested:
            if self._used_today() >= self.st["daily_mb_cap"] * 1_000_000:
                self.test_state = "capped"
                return
            if self.tx_kbit > self.st["busy_kbit"]:
                self.test_state = "busy"            # a stream is using the link; retry next tick
                return
        self.test_state = "testing"
        self.test_thread = threading.Thread(target=self._run_test, args=(iface,), daemon=True)
        self.test_thread.start()

    # ---- shaping -----------------------------------------------------
    def kbit(self):
        sh = self.sh
        up = self.speed.get("upload_kbps")
        v = up * sh["share"] if up else sh["fallback_kbit"]
        return int(clamp(v, sh["min_kbit"], sh["max_kbit"]))

    def _module(self):
        if self.module_ok is None:
            rc, _ = run_rc("modprobe", "sch_cake")
            self.module_ok = rc == 0
            if not self.module_ok:
                log("[lte] sch_cake kernel module not available; shaping disabled "
                    "(try: sudo apt install linux-modules-extra-raspi)")
        return self.module_ok

    def apply(self, iface, now):
        if self.test_thread and self.test_thread.is_alive():
            return                                    # test in progress: leave the qdisc alone
        want = self.kbit()
        cur = self.applied
        if cur and cur[0] == iface and abs(cur[1] - want) <= cur[1] * 0.1:
            if now - self.last_check < 60:
                return
            self.last_check = now
            if "cake" in run("tc", "qdisc", "show", "dev", iface):
                return                                # still installed
        if not self._module():
            self.shape_status = "unavailable"
            return
        rc, out = run_rc("tc", "qdisc", "replace", "dev", iface, "root", "cake",
                         "bandwidth", f"{want}kbit", *self.sh["options"].split())
        if rc != 0:
            self.shape_status = "error"
            log(f"[lte] tc failed: {out.strip()}")
            return
        self.applied, self.last_check = (iface, want), now
        self.shape_status = "active"
        log(f"[lte] CAKE on {iface} at {want} kbit/s")

    def clear(self):
        if self.applied:
            run_rc("tc", "qdisc", "del", "dev", self.applied[0], "root")
            log(f"[lte] CAKE removed from {self.applied[0]}")
        self.applied = None
        self.shape_status = "off"

    # ---- one loop tick -----------------------------------------------
    def step(self, now, links, egress_iface, mode, requested=False):
        lte = next((l for l in links if l["label"] == "LTE"), None)
        if lte is None:
            self.clear()
            self.was_active = False
            return self.info(None, False)
        iface = lte["iface"]
        active = egress_iface == iface
        self._sample_tx(iface, now)
        if self.sh["enabled"] and (active or self.sh["when"] == "up"):
            self.apply(iface, now)                    # set a sensible rate before testing
        elif self.applied:
            self.clear()
        self.maybe_test(now, iface, active, mode, requested)
        ap = self.applied
        if self.sh["enabled"] and ap and ap[1] != self.kbit():
            self.apply(iface, now)                    # a new measurement arrived
        self.was_active = active
        return self.info(iface, active)

    def info(self, iface, active):
        up = self.speed.get("upload_kbps")
        return {
            "present": iface is not None,
            "iface": iface,
            "active": active,
            "upload_kbps": up,
            "measured_at": self.speed.get("measured_at"),
            "test": self.test_state,
            "shaping": {"status": self.shape_status if iface else "off",
                        "kbit": self.applied[1] if self.applied else None},
            "recommended_video_kbps": int(up * self.cfg["lte"]["video_share"]) if up else None,
        }
