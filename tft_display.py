#!/usr/bin/env python3
"""Colour TFT (ST7735S 1.8") dashboard: connectivity + flight-controller status.

Reads the connectivity controller's state.json and listens to MAVProxy's spare
UDP output for flight-controller data. Read-only: it never changes the network
and never sends anything to the flight controller.

  tft_display.py          run the dashboard (what the systemd service does)
  tft_display.py --test   draw colour bars + a border, to calibrate bgr/invert/offsets
"""
import json
import select
import socket
import sys
import time

from PIL import Image, ImageDraw, ImageFont

from conn_common import SECRETS_FILE, STATE_FILE, load_config, read_json
from fc_parser import FcState, parse_datagram

STALE_AFTER = 45
FONT_DIR = "/usr/share/fonts/truetype/dejavu/"

BG = (0, 0, 0)
HEAD = (18, 38, 84)
WHITE = (235, 235, 235)
DIM = (130, 140, 150)
GREEN = (70, 220, 120)
ORANGE = (255, 165, 40)
RED = (255, 80, 80)
AMBER = (255, 200, 60)
KIND_COLOUR = {"crit": RED, "error": ORANGE, "warn": AMBER, "info": DIM}


class Fonts:
    def __init__(self):
        try:
            self.bold = ImageFont.truetype(FONT_DIR + "DejaVuSansMono-Bold.ttf", 11)
            self.big = ImageFont.truetype(FONT_DIR + "DejaVuSansMono-Bold.ttf", 13)
            self.body = ImageFont.truetype(FONT_DIR + "DejaVuSansMono.ttf", 9)
        except OSError:
            self.bold = self.big = self.body = ImageFont.load_default()


def text_w(font, s):
    return font.getlength(s)


def fit(font, s, width):
    while s and text_w(font, s) > width:
        s = s[:-1]
    return s


def cols_for(font, width):
    return max(8, int(width // max(1.0, text_w(font, "M"))))


def new_canvas(size):
    img = Image.new("RGB", size, BG)
    return img, ImageDraw.Draw(img)


def header(d, f, w, left, chip, chip_colour):
    d.rectangle((0, 0, w - 1, 16), fill=HEAD)
    cw = int(text_w(f.bold, chip)) + 8 if chip else 0
    if chip:
        d.rectangle((w - cw - 2, 2, w - 3, 14), fill=chip_colour)
        d.text((w - cw + 2, 2), chip, font=f.bold, fill=(0, 0, 0))
    d.text((3, 2), fit(f.bold, left, w - cw - 8), font=f.bold, fill=WHITE)
    return 19


def fc_row(d, f, w, y, fc, now):
    """Mode + armed box. Returns next y."""
    if not fc.link_ok(now):
        d.text((3, y + 1), "NO FC LINK", font=f.bold, fill=RED)
        sub = "waiting for MAVProxy" if fc.last_heartbeat is None else f"last beat {int(now - fc.last_heartbeat)}s ago"
        d.text((3, y + 14), sub, font=f.body, fill=DIM)
        return y + 27
    label = "ARMED" if fc.armed else "DISARM"
    bw = int(text_w(f.bold, label)) + 8
    mfont = next((x for x in (f.big, f.bold, f.body) if text_w(x, fc.mode) <= w - bw - 10), f.body)
    d.text((3, y + (0 if mfont is f.big else 2 if mfont is f.bold else 4)), fit(mfont, fc.mode, w - bw - 10), font=mfont, fill=WHITE)
    if fc.armed:
        d.rectangle((w - bw - 2, y, w - 3, y + 14), fill=RED)
        d.text((w - bw + 2, y + 1), label, font=f.bold, fill=(255, 255, 255))
    else:
        d.rectangle((w - bw - 2, y, w - 3, y + 14), outline=GREEN)
        d.text((w - bw + 2, y + 1), label, font=f.bold, fill=GREEN)
    y += 17
    parts = []
    if fc.fix is not None:
        fixcol = GREEN if fc.fix >= 3 else AMBER if fc.fix == 2 else RED
        parts.append((fc.status_line().split(" ")[0] if fc.fix < 2 else " ".join(fc.status_line().split(" ")[:2]), fixcol))
    if fc.volts is not None:
        vcol = RED if fc.pct is not None and fc.pct < 20 else AMBER if fc.pct is not None and fc.pct < 40 else WHITE
        parts.append((f"{fc.volts:.1f}V" + (f" {fc.pct}%" if fc.pct is not None else ""), vcol))
    x = 3
    for txt, col in parts:
        d.text((x, y), txt, font=f.body, fill=col)
        x += int(text_w(f.body, txt)) + 8
    return y + 13


def render_dashboard(f, size, state, fc, now):
    w, h = size
    img, d = new_canvas(size)
    label = state["egress"]["label"]
    chip, col = (f"via {label}", GREEN if label == "WiFi" else ORANGE) if label else ("NO NET", RED)
    y = header(d, f, w, state["hostname"], chip, col)
    cols = cols_for(f.body, w - 6)
    links = state.get("links", [])
    if not links:
        d.text((3, y), "no network yet", font=f.body, fill=RED)
        y += 12
    for link in links[:3]:
        mark = "*" if link.get("default") else " "
        line = f"{mark}{link['label']} {link['ip']}"
        if link.get("ssid") and len(line) + 1 + 4 <= cols:
            line = fit(f.body, f"{line} {link['ssid']}", w - 6)
        d.text((3, y), line, font=f.body, fill=GREEN if link.get("default") else DIM)
        y += 11
    lte = state.get("lte") or {}
    mode = state.get("link_mode", "auto")
    if lte.get("present") and lte.get("upload_kbps"):
        shaped = (lte.get("shaping") or {}).get("kbit")
        line = f" LTE up {lte['upload_kbps'] / 1000:.1f}M" + (f" cake {shaped / 1000:.1f}M" if shaped else "")
        d.text((3, y), fit(f.body, line, w - 6), font=f.body, fill=DIM)
        y += 11
    if mode != "auto":
        d.text((3, y), f" MODE: {mode.upper()} ONLY", font=f.body, fill=AMBER)
        y += 11
    y += 2
    d.line((0, y, w, y), fill=(50, 60, 75))
    y += 3
    y = fc_row(d, f, w, y, fc, now)
    rows_room = max(0, (h - y - 1) // 10)
    if state.get("hotspot", {}).get("active"):
        d.text((3, h - 11), "SETUP WIFI ACTIVE", font=f.body, fill=AMBER)
        rows_room = max(0, (h - 12 - y) // 10)
    rows = fc.message_rows(now, cols, rows_room, max_alerts=2, max_alert_lines=6) if rows_room else []
    for text, kind in rows:
        d.text((3, y), text, font=f.body, fill=KIND_COLOUR[kind])
        y += 10
    return img


def render_messages(f, size, state, fc, now):
    w, h = size
    img, d = new_canvas(size)
    y = header(d, f, w, "FC MESSAGES" if w >= 150 else "FC MSGS", fc.mode if fc.link_ok(now) and w >= 150 else "", WHITE)
    cols = cols_for(f.body, w - 6)
    rows = fc.message_rows(now, cols, (h - y) // 10, max_alerts=3, max_alert_lines=6)
    for text, kind in rows:
        d.text((3, y), text, font=f.body, fill=KIND_COLOUR[kind])
        y += 10
    return img


def render_setup(f, size, state, secrets):
    w, h = size
    img, d = new_canvas(size)
    hs = state["hotspot"]
    y = header(d, f, w, "SETUP MODE", "", WHITE)
    d.text((3, y), "1. Join this WiFi:", font=f.body, fill=DIM)
    d.text((3, y + 10), fit(f.bold, hs["ssid"], w - 6), font=f.bold, fill=WHITE)
    d.text((3, y + 26), "2. Password:", font=f.body, fill=DIM)
    pw = secrets["password"]
    font = f.big if text_w(f.big, pw) <= w - 6 else f.bold
    d.text((3, y + 36), fit(font, pw, w - 6), font=font, fill=AMBER)
    d.text((3, y + 54), "3. Open the page:", font=f.body, fill=DIM)
    url = (hs.get("portal_url") or hs.get("ui_url") or "").rstrip("/")
    d.text((3, y + 64), fit(f.body, url, w - 6), font=f.body, fill=WHITE)
    if state.get("setup", {}).get("status") == "failed":
        d.text((3, h - 12), fit(f.body, "Join failed, retry", w - 6), font=f.body, fill=RED)
    return img


def render_connecting(f, size, state):
    w, h = size
    img, d = new_canvas(size)
    y = header(d, f, w, "CONNECTING", "", WHITE)
    d.text((3, y + 4), "to " + fit(f.bold, state["setup"].get("target_ssid", ""), w - 28), font=f.bold, fill=WHITE)
    d.text((3, y + 24), "Setup WiFi is off.", font=f.body, fill=DIM)
    d.text((3, y + 36), "Please wait...", font=f.body, fill=DIM)
    return img


def render_no_controller(f, size):
    img, d = new_canvas(size)
    header(d, f, size[0], "mavsphere", "", WHITE)
    d.text((3, 24), "Connectivity", font=f.bold, fill=RED)
    d.text((3, 38), "controller not running", font=f.body, fill=DIM)
    return img


def pages_for(state, secrets, fc, now, cfg, size):
    """Pages to rotate through, as [(name, seconds)]."""
    if state is None or now - state.get("timestamp", 0) > STALE_AFTER:
        return [("nocontroller", 1)]
    if state.get("setup", {}).get("status") == "connecting":
        return [("connecting", 1)]
    pages = [("dashboard", cfg["dashboard_seconds"])]
    if fc.link_ok(now):
        cols = cols_for(_BODY, size[0] - 6)
        room = max(2, (size[1] - 92) // 10)     # rows the dashboard has left for messages
        if len(fc.message_rows(now, cols, 99, max_alerts=3, max_alert_lines=99)) > room:
            pages.append(("messages", cfg["messages_seconds"]))
    if state.get("hotspot", {}).get("active") and secrets and secrets.get("password"):
        pages.append(("setup", cfg["setup_seconds"]))
    return pages


_BODY = Fonts().body


def run_test(dev):
    w, h = dev.width, dev.height
    img, d = new_canvas((w, h))
    bars = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)]
    bh = h // 4
    for i, c in enumerate(bars):
        d.rectangle((0, i * bh, w, (i + 1) * bh), fill=c)
    d.rectangle((0, 0, w - 1, h - 1), outline=(255, 255, 0))
    f = Fonts()
    d.text((4, 4), "RED", font=f.bold, fill=(0, 0, 0))
    d.text((4, bh + 4), "GREEN", font=f.bold, fill=(0, 0, 0))
    d.text((4, 2 * bh + 4), "BLUE", font=f.bold, fill=(255, 255, 255))
    d.text((4, 3 * bh + 4), "WHITE", font=f.bold, fill=(0, 0, 0))
    dev.show(img)


def make_device(t):
    from tft_driver import ST7735
    return ST7735(t["spi_bus"], t["spi_device"], t["speed_hz"], t["dc_pin"], t["rst_pin"], t["blk_pin"],
                  t["width"], t["height"], t["rotation"], t["col_offset"], t["row_offset"],
                  t["bgr"], t["invert"])


def main():
    cfg = load_config()
    t = cfg["tft"]
    if "--test" in sys.argv:
        run_test(make_device(t))
        print("Test pattern drawn. Rows should read red, green, blue, white with a yellow border.")
        return
    if not t["enabled"]:
        return
    host, port = t["udp_listen"].rsplit(":", 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, int(port)))
    fc = FcState(t["alert_hold_seconds"], t["link_timeout_seconds"])
    fonts = Fonts()
    dev, last_key = None, None
    page, page_since, last_draw = None, 0.0, 0.0
    while True:
        ready, _, _ = select.select([sock], [], [], 0.5)
        now = time.time()
        while ready:
            try:
                data, _ = sock.recvfrom(4096)
            except BlockingIOError:
                break
            for sysid, compid, msgid, payload in parse_datagram(data):
                fc.feed(sysid, compid, msgid, payload, now)
            ready = select.select([sock], [], [], 0)[0]
        fc.tick(now)
        if now - last_draw < 0.5:
            continue
        last_draw = now
        try:
            if dev is None:
                dev = make_device(t)
            state = read_json(STATE_FILE)
            secrets = read_json(SECRETS_FILE)
            size = (dev.width, dev.height)
            pages = pages_for(state, secrets, fc, now, t, size)
            names = [n for n, _ in pages]
            if page not in names:
                page, page_since = names[0], now
            elif now - page_since >= dict(pages)[page]:
                page, page_since = names[(names.index(page) + 1) % len(names)], now
            if page == "nocontroller":
                img = render_no_controller(fonts, size)
            elif page == "connecting":
                img = render_connecting(fonts, size, state)
            elif page == "messages":
                img = render_messages(fonts, size, state, fc, now)
            elif page == "setup":
                img = render_setup(fonts, size, state, secrets)
            else:
                img = render_dashboard(fonts, size, state, fc, now)
            key = img.tobytes()
            if key != last_key:
                dev.show(img)
                last_key = key
        except (OSError, ImportError) as e:
            print("display not ready:", e, file=sys.stderr, flush=True)
            dev, last_key = None, None
            time.sleep(3)


if __name__ == "__main__":
    main()
