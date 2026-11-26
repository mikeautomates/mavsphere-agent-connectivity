#!/usr/bin/env python3
"""Second OLED: flight-controller status (mode, armed, GPS/battery, FC messages).

Listens on MAVProxy's spare UDP output (default 127.0.0.1:14601), so it works
independently of the MavSphere agent. Read-only; sends nothing to the FC.
"""
import select
import socket
import time

from PIL import Image, ImageDraw, ImageFont

from conn_common import load_config
from fc_parser import FcState, parse_datagram
from oled_driver import H, SSD1306, W

FONT_DIR = "/usr/share/fonts/truetype/dejavu/"


def load_fonts():
    try:
        return (ImageFont.truetype(FONT_DIR + "DejaVuSansMono-Bold.ttf", 12),
                ImageFont.truetype(FONT_DIR + "DejaVuSansMono.ttf", 8))
    except OSError:
        f = ImageFont.load_default()
        return f, f


def fit(d, text, font_big, font_small, width):
    for font in (font_big, font_small):
        if d.textlength(text, font=font) <= width:
            return font, text
    while len(text) > 1 and d.textlength(text, font=font_small) > width:
        text = text[:-1]
    return font_small, text


def render(state, now, fonts):
    big, small = fonts
    img = Image.new("1", (W, H))
    d = ImageDraw.Draw(img)
    link = state.link_ok(now)

    if not link:
        d.text((0, 0), "NO FC LINK", font=big, fill=1)
        if state.last_heartbeat is None:
            d.text((0, 15), "waiting for MAVProxy", font=small, fill=1)
        else:
            d.text((0, 15), f"last heartbeat {int(now - state.last_heartbeat)}s ago", font=small, fill=1)
    else:
        box_x = 74
        font, text = fit(d, state.mode, big, small, box_x - 4)
        d.text((0, 0 if font is big else 3), text, font=font, fill=1)
        if state.armed:
            d.rectangle((box_x, 0, W - 1, 13), fill=1)
            d.text((box_x + 8, 0), "ARMED", font=big, fill=0)
        else:
            d.rectangle((box_x, 0, W - 1, 13), outline=1)
            d.text((box_x + 6, 0), "DISARM", font=big, fill=1)
        d.text((0, 15), state.status_line()[:26], font=small, fill=1)
    d.line((0, 25, W, 25), fill=1)

    lines = state.message_lines(now, 26, 4)
    if not lines and not link:
        lines = ["check the mavproxy", "service is running"]
    y = 27
    for line in lines:
        d.text((0, y), line, font=small, fill=1)
        y += 9
    return img


def main():
    cfg = load_config()
    fcfg = cfg["fc_display"]
    if not fcfg["enabled"]:
        return
    host, port = fcfg["udp_listen"].rsplit(":", 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, int(port)))
    state = FcState(fcfg["alert_hold_seconds"], fcfg["link_timeout_seconds"])
    fonts = load_fonts()
    oled, last, last_draw = None, None, 0.0
    while True:
        ready, _, _ = select.select([sock], [], [], 0.5)
        now = time.time()
        if ready:
            while True:
                try:
                    data, _ = sock.recvfrom(4096)
                except BlockingIOError:
                    break
                for sysid, compid, msgid, payload in parse_datagram(data):
                    state.feed(sysid, compid, msgid, payload, now)
                if not select.select([sock], [], [], 0)[0]:
                    break
        state.tick(now)
        if now - last_draw < 0.4:
            continue
        last_draw = now
        try:
            if oled is None:
                oled = SSD1306(fcfg["i2c_bus"], fcfg["i2c_address"], fcfg["rotate_180"], fcfg["controller"])
            img = render(state, now, fonts)
            key = img.tobytes()
            if key != last:
                oled.show(img)
                last = key
        except OSError:
            oled, last = None, None  # display not ready/connected; keep listening


if __name__ == "__main__":
    main()
