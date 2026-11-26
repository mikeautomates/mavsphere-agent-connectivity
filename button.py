#!/usr/bin/env python3
"""Physical button: short press cycles the link mode (auto -> lte -> wifi), a long press
starts or stops the setup hotspot. Button between the GPIO pin and GND (internal pull-up)."""
import time

import conn_common as cc
from conn_common import add_request, load_config
from shaper import read_mode

CYCLE = ("auto", "lte", "wifi")

POLL = 0.02
DEBOUNCE = 0.05


class ButtonLogic:
    """Turns raw pin levels (0 = pressed) into 'short' / 'long' events."""

    def __init__(self, long_seconds):
        self.long = long_seconds
        self.down_since = None
        self.fired_long = False
        self.last_change = 0.0
        self.level = 1

    def update(self, level, now):
        if level != self.level:
            if now - self.last_change < DEBOUNCE:
                return None
            self.level, self.last_change = level, now
            if level == 0:
                self.down_since, self.fired_long = now, False
                return None
            was_long = self.fired_long
            started, self.down_since = self.down_since, None
            if started is not None and not was_long:
                return "short"
            return None
        if self.level == 0 and self.down_since is not None and not self.fired_long \
                and now - self.down_since >= self.long:
            self.fired_long = True
            return "long"
        return None


def next_mode(current):
    return CYCLE[(CYCLE.index(current) + 1) % len(CYCLE)] if current in CYCLE else "auto"


def handle(event):
    if event == "short":
        new = next_mode(read_mode())
        cc.write_atomic(cc.MODE_FILE, new + "\n")
        print(f"[button] short press: mode -> {new}", flush=True)
    elif event == "long":
        add_request("hotspot")
        print("[button] long press: hotspot toggle requested", flush=True)


def main():
    cfg = load_config()["button"]
    if not cfg["enabled"]:
        return
    import lgpio
    h = None
    for chip in (0, 4):
        try:
            h = lgpio.gpiochip_open(chip)
            break
        except Exception:
            continue
    if h is None:
        raise SystemExit("cannot open a gpiochip")
    lgpio.gpio_claim_input(h, cfg["pin"], lgpio.SET_PULL_UP)
    logic = ButtonLogic(cfg["long_seconds"])
    print(f"[button] watching GPIO{cfg['pin']}", flush=True)
    while True:
        event = logic.update(lgpio.gpio_read(h, cfg["pin"]), time.monotonic())
        if event:
            handle(event)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
