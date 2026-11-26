#!/usr/bin/env python3
"""0.96" OLED: LTE signal bars + today's data use, alternating with a live upload graph.

Read-only view of state.json (written by the controller). Needs python3-smbus and python3-pil.
"""
import collections
import json
import time

from PIL import Image, ImageDraw, ImageFont

from conn_common import STATE_FILE, load_config, read_json
from oled_driver import H, SSD1306, W

STALE_AFTER = 45
FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
HISTORY = 128           # one sample per controller tick, so about 6 minutes across the screen


def load_fonts():
    try:
        return (ImageFont.truetype(FONT_DIR + "DejaVuSansMono-Bold.ttf", 12),
                ImageFont.truetype(FONT_DIR + "DejaVuSansMono.ttf", 9),
                ImageFont.truetype(FONT_DIR + "DejaVuSansMono-Bold.ttf", 20))
    except OSError:
        f = ImageFont.load_default()
        return f, f, f


def kbps_text(v):
    if v is None:
        return "-"
    return f"{v / 1000:.1f}M" if v >= 1000 else f"{int(v)}k"


def render_signal(state, fonts):
    big, small, huge = fonts
    img = Image.new("1", (W, H))
    d = ImageDraw.Draw(img)
    lte = state.get("lte") or {}
    d.text((0, 0), "LTE", font=big, fill=1)
    if (state.get("egress") or {}).get("label") == "LTE":
        d.rectangle((72, 0, W - 1, 13), fill=1)
        d.text((78, 1), "ACTIVE", font=big, fill=0)
    else:
        d.text((78, 1), "standby", font=small, fill=1)
    d.line((0, 14, W, 14), fill=1)
    if not lte.get("present") and not lte.get("signal"):
        d.text((0, 24), "no LTE modem", font=big, fill=1)
        d.text((0, 40), "or no link", font=small, fill=1)
        return img
    sig = lte.get("signal")
    bars = sig["bars"] if sig else 0
    for i in range(5):
        x, h = 2 + i * 12, 5 + i * 5
        box = (x, 44 - h, x + 8, 44)
        d.rectangle(box, fill=1) if i < bars else d.rectangle(box, outline=1)
    if sig and sig.get("dbm") is not None:
        d.text((70, 20), str(sig["dbm"]), font=huge, fill=1)
        d.text((108, 29), "dBm", font=small, fill=1)
    else:
        d.text((72, 22), "no sig", font=big, fill=1)
    use = lte.get("usage") or {}
    d.text((0, 46), f"Today {use.get('total_mb', 0):.1f} MB", font=small, fill=1)
    d.text((0, 55), f"up {use.get('tx_mb', 0):.1f} dn {use.get('rx_mb', 0):.1f}", font=small, fill=1)
    return img


def render_graph(state, history, fonts):
    big, small, _ = fonts
    img = Image.new("1", (W, H))
    d = ImageDraw.Draw(img)
    lte = state.get("lte") or {}
    cap = (lte.get("shaping") or {}).get("kbit")
    now_k = lte.get("tx_kbps", 0)
    d.text((0, 0), f"UP {kbps_text(now_k)}", font=big, fill=1)
    d.text((W - 62, 3), f"cap {kbps_text(cap)}" if cap else "no cap", font=small, fill=1)
    top, bottom = 16, H - 1
    d.line((0, bottom, W, bottom), fill=1)
    d.line((0, top, 0, bottom), fill=1)
    peak = max([200] + list(history) + ([cap * 1.15] if cap else []))
    scale = (bottom - top - 1) / peak
    ys = lambda v: bottom - 1 - min(bottom - top - 2, int(v * scale))
    if cap:
        y = ys(cap)
        for x in range(1, W, 4):
            d.point((x, y), fill=1)
            d.point((x + 1, y), fill=1)
    pts = list(history)[-(W - 1):]
    x0 = W - len(pts)
    prev = None
    for i, v in enumerate(pts):
        p = (x0 + i, ys(v))
        if prev:
            d.line((prev, p), fill=1)
        prev = p
    return img


def render_stale(fonts):
    big, small, _ = fonts
    img = Image.new("1", (W, H))
    d = ImageDraw.Draw(img)
    d.text((0, 0), "Connectivity", font=big, fill=1)
    d.text((0, 16), "controller not", font=small, fill=1)
    d.text((0, 27), "running", font=small, fill=1)
    return img


def render(state, page, history, fonts, now=None):
    now = time.time() if now is None else now
    if not state or now - state.get("timestamp", 0) > STALE_AFTER:
        return render_stale(fonts)
    return render_signal(state, fonts) if page == 0 else render_graph(state, history, fonts)


def main():
    cfg = load_config()["oled"]
    if not cfg["enabled"]:
        return
    fonts = load_fonts()
    history = collections.deque(maxlen=HISTORY)
    oled, last, last_ts, page, flipped = None, None, None, 0, 0.0
    while True:
        try:
            if oled is None:
                oled = SSD1306(cfg["i2c_bus"], cfg["i2c_address"], cfg["rotate_180"], cfg["controller"])
            now = time.time()
            state = read_json(STATE_FILE)
            if state and state.get("timestamp") != last_ts:
                last_ts = state.get("timestamp")
                history.append((state.get("lte") or {}).get("tx_kbps", 0))
            if now - flipped >= cfg["page_seconds"]:
                page, flipped = 1 - page, now
            img = render(state, page, history, fonts, now)
            key = img.tobytes()
            if key != last:
                oled.show(img)
                last = key
        except OSError:
            oled, last = None, None      # I2C not ready yet; retry
        time.sleep(1)


if __name__ == "__main__":
    main()
