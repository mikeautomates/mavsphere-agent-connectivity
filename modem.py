"""LTE statistics for the display and web UI: signal strength, today's data use, live rates.

Signal comes from the modem's AT port (AT+CSQ), polled in a background thread so the
controller never blocks. Data use and rates come from the interface byte counters.
"""
import json
import os
import select
import termios
import threading
import time

import conn_common as cc

SYSFS_NET = "/sys/class/net"


def log(msg):
    print(msg, flush=True)


def csq_to_dbm(csq):
    if csq is None or csq == 99 or not 0 <= csq <= 31:
        return None
    return -113 + 2 * csq if csq < 31 else -51


def dbm_to_bars(dbm):
    if dbm is None:
        return 0
    for limit, bars in ((-65, 5), (-75, 4), (-85, 3), (-95, 2), (-105, 1)):
        if dbm >= limit:
            return bars
    return 0


def parse_csq(text):
    """'+CSQ: 19,99' -> 19, or None."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("+CSQ:"):
            try:
                return int(line.split(":", 1)[1].split(",")[0])
            except ValueError:
                return None
    return None


def at_command(port, cmd, timeout=2.0):
    """Send one AT command on a serial port and return the reply text ('' on failure)."""
    fd = None
    try:
        fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(fd)
        attrs[0] = attrs[1] = attrs[3] = 0                      # raw
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[4] = attrs[5] = termios.B115200
        attrs[6][termios.VMIN], attrs[6][termios.VTIME] = 0, 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIOFLUSH)
        os.write(fd, (cmd + "\r").encode())
        buf, end = b"", time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.2)
            if r:
                buf += os.read(fd, 256)
                if b"OK" in buf or b"ERROR" in buf:
                    break
        return buf.decode("ascii", "replace")
    except OSError:
        return ""
    finally:
        if fd is not None:
            os.close(fd)


class LteStats:
    def __init__(self, cfg):
        self.cfg = cfg
        self.port = cfg["lte"].get("modem_at_port", "")
        self.poll = cfg["lte"].get("signal_poll_seconds", 20)
        self.signal = None
        self._sig_thread = None
        self._last_poll = 0.0
        self.iface = None
        self.prev = None            # (time, tx, rx)
        self.tx_kbps = 0.0
        self.rx_kbps = 0.0
        self.last_saved = 0.0
        data = cc.read_json(cc.USAGE_FILE) or {}
        self.day = data.get("day", self._today())
        self.tx = int(data.get("tx", 0))
        self.rx = int(data.get("rx", 0))
        if self.day != self._today():
            self.day, self.tx, self.rx = self._today(), 0, 0

    @staticmethod
    def _today():
        return time.strftime("%Y-%m-%d")

    # ---- signal (background) -----------------------------------------
    def _poll_signal(self):
        csq = parse_csq(at_command(self.port, "AT+CSQ"))
        dbm = csq_to_dbm(csq)
        self.signal = None if csq is None else {"csq": csq, "dbm": dbm, "bars": dbm_to_bars(dbm)}

    def _maybe_poll(self, now):
        if not self.port or not os.path.exists(self.port):
            self.signal = None
            return
        if self._sig_thread and self._sig_thread.is_alive():
            return
        if now - self._last_poll < self.poll:
            return
        self._last_poll = now
        self._sig_thread = threading.Thread(target=self._poll_signal, daemon=True)
        self._sig_thread.start()

    # ---- counters ----------------------------------------------------
    def _read(self, iface):
        try:
            with open(f"{SYSFS_NET}/{iface}/statistics/tx_bytes") as f:
                tx = int(f.read())
            with open(f"{SYSFS_NET}/{iface}/statistics/rx_bytes") as f:
                rx = int(f.read())
            return tx, rx
        except (OSError, ValueError):
            return None

    def _save(self, now):
        try:
            cc.write_atomic(cc.USAGE_FILE, json.dumps({"day": self.day, "tx": self.tx, "rx": self.rx}))
            self.last_saved = now
        except OSError as e:
            log(f"[lte] cannot save usage: {e}")

    def step(self, now, iface):
        today = self._today()
        if today != self.day:
            self.day, self.tx, self.rx = today, 0, 0
            self._save(now)
        if iface != self.iface:
            self.iface, self.prev = iface, None
            self.tx_kbps = self.rx_kbps = 0.0
        if iface:
            cur = self._read(iface)
            if cur:
                if self.prev:
                    t0, tx0, rx0 = self.prev
                    dtx = cur[0] - tx0 if cur[0] >= tx0 else cur[0]   # counter reset: count from zero
                    drx = cur[1] - rx0 if cur[1] >= rx0 else cur[1]
                    self.tx += dtx
                    self.rx += drx
                    if now - t0 > 0.5:
                        self.tx_kbps = dtx * 8 / 1000 / (now - t0)
                        self.rx_kbps = drx * 8 / 1000 / (now - t0)
                self.prev = (now, cur[0], cur[1])
            self._maybe_poll(now)
        else:
            self.signal = self.signal if self.port and os.path.exists(self.port) else None
            self._maybe_poll(now)
        if now - self.last_saved >= 60:
            self._save(now)
        return self.info()

    def info(self):
        return {
            "signal": self.signal,
            "usage": {"day": self.day, "tx_mb": round(self.tx / 1e6, 1), "rx_mb": round(self.rx / 1e6, 1),
                      "total_mb": round((self.tx + self.rx) / 1e6, 1)},
            "tx_kbps": int(self.tx_kbps),
            "rx_kbps": int(self.rx_kbps),
        }
