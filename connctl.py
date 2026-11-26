#!/usr/bin/env python3
"""mavsphere-conn: control and inspect the connectivity service.

  mavsphere-conn status            what is carrying traffic, LTE speed, CAKE
  mavsphere-conn mode auto|wifi|lte   choose the link (needs sudo)
  mavsphere-conn speedtest         measure LTE upload now (uses ~3 MB of LTE data; needs sudo)
"""
import sys
import time

import conn_common as cc

USAGE = __doc__


def status():
    s = cc.read_json(cc.STATE_FILE)
    if not s or time.time() - s.get("timestamp", 0) > 45:
        print("controller is not running (sudo systemctl status mavsphere-connectivity)")
        return 1
    print(f"mode:     {s.get('link_mode', 'auto')}")
    print(f"carrying: {s['egress']['label'] or 'NO NETWORK'}")
    for l in s["links"]:
        print(f"  {'*' if l.get('default') else ' '} {l['label']:5} {l['ip']:16} {l.get('ssid', '')}")
    lte = s.get("lte") or {}
    if lte.get("present"):
        up = lte.get("upload_kbps")
        age = f", {int((time.time() - lte['measured_at']) / 60)} min ago" if lte.get("measured_at") else ""
        print(f"LTE upload: {up if up else 'not measured yet'} kbit/s{age if up else ''}  (test: {lte.get('test')})")
        sh = lte.get("shaping") or {}
        print(f"CAKE:       {sh.get('status')}" + (f" at {sh['kbit']} kbit/s" if sh.get("kbit") else ""))
        if lte.get("recommended_video_kbps"):
            print(f"video on LTE: up to ~{lte['recommended_video_kbps']} kbit/s recommended")
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    try:
        if argv[0] == "status":
            return status()
        if argv[0] == "mode" and len(argv) == 2 and argv[1] in ("auto", "wifi", "lte"):
            cc.write_atomic(cc.MODE_FILE, argv[1] + "\n")
            print(f"mode set to {argv[1]}; applied within ~10 seconds")
            return 0
        if argv[0] == "speedtest":
            cc.write_atomic(cc.REQUEST_FILE, "speedtest\n")
            print("speed test requested; check 'mavsphere-conn status' in about 30 seconds")
            return 0
    except PermissionError:
        print("permission denied: run with sudo")
        return 1
    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
