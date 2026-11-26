"""Tiny dependency-free MAVLink reader for the FC status display.

Understands just enough (HEARTBEAT, SYS_STATUS, GPS_RAW_INT, STATUSTEXT) in
MAVLink 1 and 2 framing, checks CRCs, and keeps the vehicle state the display
needs. Not a general MAVLink library.
"""
import struct
import textwrap

# msgid -> (crc_extra, minimum payload length). v2 truncates trailing zero bytes,
# so payloads are zero-padded back to this length before decoding.
MSGS = {0: (50, 9), 1: (124, 31), 24: (24, 30), 253: (83, 54)}

MAV_TYPE_IGNORE = {6, 18, 27}       # GCS, onboard controller, ADSB: not the vehicle
MAV_AUTOPILOT_ARDUPILOT = 3
MAV_AUTOPILOT_INVALID = 8

PLANE_TYPES = {1, 19, 20, 21, 22, 23, 24, 25}
COPTER_TYPES = {2, 3, 4, 13, 14, 15, 29}
ROVER_TYPES = {10, 11}
SUB_TYPES = {12}

PLANE_MODES = {0: "MANUAL", 1: "CIRCLE", 2: "STABILIZE", 3: "TRAINING", 4: "ACRO", 5: "FBWA",
               6: "FBWB", 7: "CRUISE", 8: "AUTOTUNE", 10: "AUTO", 11: "RTL", 12: "LOITER",
               13: "TAKEOFF", 14: "AVOID_ADSB", 15: "GUIDED", 17: "QSTABILIZE", 18: "QHOVER",
               19: "QLOITER", 20: "QLAND", 21: "QRTL", 22: "QAUTOTUNE", 23: "QACRO",
               24: "THERMAL", 25: "LOITER_ALT_QLAND"}
COPTER_MODES = {0: "STABILIZE", 1: "ACRO", 2: "ALT_HOLD", 3: "AUTO", 4: "GUIDED", 5: "LOITER",
                6: "RTL", 7: "CIRCLE", 9: "LAND", 11: "DRIFT", 13: "SPORT", 14: "FLIP",
                15: "AUTOTUNE", 16: "POSHOLD", 17: "BRAKE", 18: "THROW", 19: "AVOID_ADSB",
                20: "GUIDED_NOGPS", 21: "SMART_RTL", 22: "FLOWHOLD", 23: "FOLLOW",
                24: "ZIGZAG", 25: "SYSTEMID", 26: "AUTOROTATE", 27: "AUTO_RTL"}
ROVER_MODES = {0: "MANUAL", 1: "ACRO", 3: "STEERING", 4: "HOLD", 5: "LOITER", 6: "FOLLOW",
               7: "SIMPLE", 8: "DOCK", 9: "CIRCLE", 10: "AUTO", 11: "RTL", 12: "SMART_RTL",
               15: "GUIDED"}
SUB_MODES = {0: "STABILIZE", 1: "ACRO", 2: "ALT_HOLD", 3: "AUTO", 4: "GUIDED", 7: "CIRCLE",
             9: "SURFACE", 16: "POSHOLD", 19: "MANUAL"}

GPS_FIX = {0: "NoGPS", 1: "NoFix", 2: "2D", 3: "3D", 4: "DGPS", 5: "RTKf", 6: "RTKx"}


def x25(data, crc=0xFFFF):
    for b in data:
        tmp = b ^ (crc & 0xFF)
        tmp = (tmp ^ (tmp << 4)) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return crc


def parse_datagram(data):
    """Return [(sysid, compid, msgid, padded_payload)] for every valid known frame."""
    out, i, n = [], 0, len(data)
    while i < n:
        magic = data[i]
        if magic == 0xFD:
            if i + 10 > n:
                break
            ln, incompat = data[i + 1], data[i + 2]
            total = 10 + ln + 2 + (13 if incompat & 1 else 0)
            if i + total > n:
                break
            sysid, compid = data[i + 5], data[i + 6]
            msgid = data[i + 7] | data[i + 8] << 8 | data[i + 9] << 16
            hdr, payload = data[i + 1:i + 10], data[i + 10:i + 10 + ln]
            crc_rx = data[i + 10 + ln] | data[i + 11 + ln] << 8
        elif magic == 0xFE:
            if i + 6 > n:
                break
            ln = data[i + 1]
            total = 6 + ln + 2
            if i + total > n:
                break
            sysid, compid, msgid = data[i + 3], data[i + 4], data[i + 5]
            hdr, payload = data[i + 1:i + 6], data[i + 6:i + 6 + ln]
            crc_rx = data[i + 6 + ln] | data[i + 7 + ln] << 8
        else:
            i += 1
            continue
        spec = MSGS.get(msgid)
        if spec is None:              # a message we do not use; skip it whole
            i += total
            continue
        crc = x25(bytes([spec[0]]), x25(hdr + payload))
        if crc != crc_rx:
            i += 1                    # corrupt or not really a frame: resync
            continue
        out.append((sysid, compid, msgid, bytes(payload).ljust(spec[1], b"\0")))
        i += total
    return out


def mode_name(mav_type, autopilot, custom_mode):
    if autopilot != MAV_AUTOPILOT_ARDUPILOT:
        return f"MODE {custom_mode}"
    for types, names in ((PLANE_TYPES, PLANE_MODES), (COPTER_TYPES, COPTER_MODES),
                         (ROVER_TYPES, ROVER_MODES), (SUB_TYPES, SUB_MODES)):
        if mav_type in types:
            return names.get(custom_mode, f"MODE {custom_mode}")
    return f"MODE {custom_mode}"


class FcState:
    def __init__(self, alert_hold=60.0, link_timeout=5.0):
        self.alert_hold = alert_hold
        self.link_timeout = link_timeout
        self.sysid = None
        self.last_heartbeat = None
        self.armed = False
        self.mode = ""
        self.fix = None
        self.sats = None
        self.volts = None
        self.pct = None
        self.messages = []           # dicts: t, sev, text
        self._chunks = {}            # statustext id -> {"t", "sev", "parts": {seq: bytes}}

    # ---- input -------------------------------------------------------
    def feed(self, sysid, compid, msgid, p, now):
        if msgid == 0:
            custom, mtype, autopilot, base_mode = struct.unpack_from("<IBBB", p)
            if mtype in MAV_TYPE_IGNORE or autopilot == MAV_AUTOPILOT_INVALID:
                return
            stale = self.last_heartbeat is None or now - self.last_heartbeat > self.link_timeout * 3
            if self.sysid is None or sysid == self.sysid or stale:
                self.sysid = sysid
                self.last_heartbeat = now
                self.armed = bool(base_mode & 0x80)
                self.mode = mode_name(mtype, autopilot, custom)
            return
        if sysid != self.sysid:
            return
        if msgid == 1:
            mv = struct.unpack_from("<H", p, 14)[0]
            rem = struct.unpack_from("<b", p, 30)[0]
            self.volts = None if mv == 0xFFFF else mv / 1000.0
            self.pct = None if rem < 0 else rem
        elif msgid == 24:
            self.fix, self.sats = p[28], p[29]
        elif msgid == 253:
            sev, raw = p[0], p[1:51]
            ident, seq = p[51] | p[52] << 8, p[53]
            self._statustext(sev, raw, ident, seq, now)

    def _statustext(self, sev, raw, ident, seq, now):
        if ident == 0:
            self._add(now, sev, raw.split(b"\0")[0])
            return
        chunk = self._chunks.setdefault(ident, {"t": now, "sev": sev, "parts": {}})
        chunk["parts"][seq] = raw.split(b"\0")[0]
        if len(raw.split(b"\0")[0]) < 50:      # a short chunk ends the message
            self._finish(ident)

    def _finish(self, ident):
        chunk = self._chunks.pop(ident, None)
        if chunk:
            data = b"".join(chunk["parts"][k] for k in sorted(chunk["parts"]))
            self._add(chunk["t"], chunk["sev"], data)

    def _add(self, now, sev, raw):
        text = raw.decode("ascii", "replace").strip()
        if not text or sev >= 7:
            return
        if self.messages and self.messages[-1]["text"] == text and self.messages[-1]["sev"] == sev:
            self.messages[-1]["t"] = now        # repeated message: refresh, do not duplicate
            return
        self.messages.append({"t": now, "sev": sev, "text": text})
        del self.messages[:-50]

    def tick(self, now):
        for ident in [k for k, c in self._chunks.items() if now - c["t"] > 3]:
            self._finish(ident)                 # incomplete chunked message: show what we have

    # ---- views -------------------------------------------------------
    def link_ok(self, now):
        return self.last_heartbeat is not None and now - self.last_heartbeat <= self.link_timeout

    def status_line(self):
        parts = []
        if self.fix is not None:
            parts.append(GPS_FIX.get(self.fix, f"fix{self.fix}") + (f" {self.sats}sat" if self.fix >= 2 else ""))
        if self.volts is not None:
            parts.append(f"{self.volts:.1f}V")
        if self.pct is not None:
            parts.append(f"{self.pct}%")
        return " ".join(parts)

    def message_rows(self, now, cols, rows, max_alerts=2, max_alert_lines=3):
        """Wrapped message lines as (text, kind); kind is crit/error/warn/info.

        Recent alerts (severity <= 4, held alert_hold seconds) sit at the bottom,
        newest last; info lines fill whatever room is left above them.
        """
        def prefix(sev):
            return "! " if sev <= 2 else "E " if sev == 3 else "W "

        def kind(sev):
            return "crit" if sev <= 2 else "error" if sev == 3 else "warn"
        alerts, seen = [], set()
        for m in reversed(self.messages):
            if m["sev"] <= 4 and now - m["t"] <= self.alert_hold and m["text"] not in seen:
                seen.add(m["text"])
                alerts.append(m)
            if len(alerts) == max_alerts:
                break
        alert_rows = []
        for m in alerts:                        # newest first; keep whole messages that fit
            lines = [(line, kind(m["sev"])) for line in textwrap.wrap(
                m["text"], cols, initial_indent=prefix(m["sev"]), subsequent_indent="  ")]
            limit = min(max_alert_lines, rows)
            if not alert_rows:
                alert_rows = lines[:limit]      # newest always shows, from its first line
            elif len(alert_rows) + len(lines) <= limit:
                alert_rows = lines + alert_rows # older goes above
        info_rows = []
        for m in self.messages:
            if m["sev"] >= 5:
                info_rows += [(line, "info") for line in textwrap.wrap(m["text"], cols)]
        room = rows - len(alert_rows)
        return (info_rows[-room:] if room > 0 else []) + alert_rows

    def message_lines(self, now, cols, rows):
        return [t for t, _ in self.message_rows(now, cols, rows)]
