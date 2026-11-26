#!/usr/bin/env python3
"""OLED (SSD1306 128x64, I2C) view of the connectivity controller's state.

It only reads state.json / hotspot.json; it never changes the network.
Needs python3-smbus and python3-pil (from apt).
"""
import json
import time

from PIL import Image, ImageDraw, ImageFont

from conn_common import SECRETS_FILE, STATE_FILE, load_config
from oled_driver import H, SSD1306, W

STALE_AFTER = 45  # seconds without a state update = controller is not running (scans/joins can block a while)


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def load_font():
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 10)
    except OSError:
        return ImageFont.load_default()


def screens_for(state, secrets):
    """List of screens to cycle through, as ('links', [link, link]) / ('hotspot', None)."""
    if state.get("setup", {}).get("status") == "connecting":
        return [("connecting", None)]
    links = state.get("links", [])
    chunks = [links[i:i + 2] for i in range(0, len(links), 2)] or [[]]
    screens = [("links", c) for c in chunks]
    if state.get("hotspot", {}).get("active") and secrets and secrets.get("password"):
        screens.append(("hotspot", None))
    return screens


def render(font, state, secrets, screen, index, total, now=None):
    now = time.time() if now is None else now
    img = Image.new("1", (W, H))
    d = ImageDraw.Draw(img)

    if state is None or now - state.get("timestamp", 0) > STALE_AFTER:
        d.text((0, 0), "Connectivity", font=font, fill=1)
        d.text((0, 14), "controller not", font=font, fill=1)
        d.text((0, 26), "running", font=font, fill=1)
        return img

    kind, payload = screen
    setup = state.get("setup", {})
    if kind == "connecting":
        d.text((0, 0), "CONNECTING", font=font, fill=1)
        d.line((0, 12, W, 12), fill=1)
        d.text((0, 16), ("to " + setup.get("target_ssid", ""))[:21], font=font, fill=1)
        d.text((0, 30), "Setup WiFi is off.", font=font, fill=1)
        d.text((0, 42), "Please wait...", font=font, fill=1)
        return img
    if kind == "hotspot":
        hs = state["hotspot"]
        d.text((0, 0), "SETUP MODE", font=font, fill=1)
        d.line((0, 12, W, 12), fill=1)
        d.text((0, 15), ("WiFi " + hs["ssid"])[:21], font=font, fill=1)
        d.text((0, 27), ("Pass " + secrets["password"])[:21], font=font, fill=1)
        d.text((0, 39), (hs.get("portal_url") or hs["ui_url"]).rstrip("/")[:21], font=font, fill=1)
        last = "Join failed, retry" if setup.get("status") == "failed" else "Join, then open page"
        d.text((0, 51), last, font=font, fill=1)
        return img

    label = state["egress"]["label"]
    head = f"{state['hostname'][:9]} via {label}" if label else f"{state['hostname'][:9]} NO NET"
    d.text((0, 0), head[:21], font=font, fill=1)
    if total > 1:
        d.text((W - 21, 0), f"{index + 1}/{total}", font=font, fill=1)
    d.line((0, 12, W, 12), fill=1)
    if not payload:
        d.text((0, 20), "Waiting for network", font=font, fill=1)
        return img
    y = 15
    for link in payload:
        head = link["label"] + ("*" if link.get("default") else "") + (" " + link["ssid"] if link.get("ssid") else "")
        d.text((0, y), head[:21], font=font, fill=1)
        d.text((6, y + 11), link["ip"], font=font, fill=1)
        y += 25
    return img


def main():
    cfg = load_config()
    dcfg = cfg["display"]
    if not dcfg["enabled"]:
        return
    font = load_font()
    oled, last, idx, last_flip = None, None, 0, 0.0
    while True:
        try:
            if oled is None:
                oled = SSD1306(dcfg["i2c_bus"], dcfg["i2c_address"], dcfg["rotate_180"], dcfg["controller"])
            state = read_json(STATE_FILE)
            secrets = read_json(SECRETS_FILE)
            screens = screens_for(state, secrets) if state else [("links", [])]
            now = time.time()
            if now - last_flip >= dcfg["page_seconds"]:
                idx, last_flip = idx + 1, now
            n = idx % len(screens)
            stale = state is None or now - state.get("timestamp", 0) > STALE_AFTER
            # Redraw only when what is on screen changes (not on every timestamp tick).
            visible = {k: v for k, v in (state or {}).items() if k != "timestamp"}
            key = (stale, json.dumps(visible, sort_keys=True), n, secrets and secrets.get("password"))
            if key != last:
                oled.show(render(font, state, secrets, screens[n], n, len(screens)))
                last = key
        except OSError:
            oled, last = None, None  # I2C not ready yet; retry
        time.sleep(1)


if __name__ == "__main__":
    main()
