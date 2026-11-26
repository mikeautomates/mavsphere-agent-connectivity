"""Shared helpers for the MavSphere connectivity controller and its OLED display."""
import json
import os
import subprocess

CONFIG_PATH = os.environ.get("MAVSPHERE_CONN_CONFIG", "/etc/mavsphere-connectivity/config.json")
STATE_DIR = os.environ.get("MAVSPHERE_CONN_STATE_DIR", "/run/mavsphere-connectivity")
STATE_FILE = os.path.join(STATE_DIR, "state.json")        # public: safe for other services to read
SECRETS_FILE = os.path.join(STATE_DIR, "hotspot.json")     # private (0600): hotspot password
DATA_DIR = os.environ.get("MAVSPHERE_CONN_DATA_DIR", "/var/lib/mavsphere-connectivity")
MODE_FILE = os.path.join(DATA_DIR, "mode")                 # auto | wifi | lte (survives reboots)
SPEED_FILE = os.path.join(DATA_DIR, "lte_speed.json")      # last LTE upload measurement + data used
REQUEST_FILE = os.path.join(DATA_DIR, "request")           # one request per line: speedtest | hotspot
USAGE_FILE = os.path.join(DATA_DIR, "lte_usage.json")      # today's LTE data use
UI_PASSWORD_FILE = os.environ.get("MAVSPHERE_CONN_UI_PASSWORD", "/etc/mavsphere-connectivity/ui_password")

DEFAULTS = {
    "agent_ui_port": 8090,
    "wifi_iface": "wlan0",
    # Interface-name prefixes treated as LTE. Edit if your modem shows up differently.
    "lte_prefixes": ["ww", "usb", "ppp", "rmnet", "enx"],
    "ignore_prefixes": ["lo", "docker", "veth", "br-", "virbr", "tailscale"],
    # Which link carries traffic. auto = WiFi preferred, LTE as fallback.
    "link": {
        "wifi_metric": 600,
        "lte_metric": 700,
        "forced_lte_metric": 50,   # used in "lte" mode so LTE beats WiFi
    },
    # LTE upload measurement + CAKE traffic shaping on the LTE interface.
    "lte": {
        "shaping": {
            "enabled": True,
            "when": "active",      # "active" = only while LTE carries traffic, "up" = whenever LTE is up
            "share": 0.85,         # shape to this fraction of the measured upload speed
            "min_kbit": 300,
            "max_kbit": 20000,
            "fallback_kbit": 2000, # used until the first measurement succeeds
            "options": "besteffort",
        },
        "speedtest": {
            "enabled": True,
            "url": "https://speed.cloudflare.com/__up",
            "bytes": 3000000,      # data used per test (upload)
            "max_seconds": 25,
            "retest_minutes": 30,  # while LTE is the active link
            "daily_mb_cap": 60,    # stop automatic tests after this much test upload per day
            "busy_kbit": 400,      # skip automatic tests while the link is already sending this much (a stream)
        },
        "video_share": 0.25,       # recommended_video_kbps = measured upload x this
        "modem_at_port": "/dev/ttyUSB2",   # EC25 AT port, for signal strength ("" to disable)
        "signal_poll_seconds": 20,
    },
    "hotspot": {
        "enabled": True,
        "grace_seconds": 150,    # no wifi for this long -> start the setup hotspot (slow boots need time)
        "retry_seconds": 300,    # hotspot idle (no clients) this long -> stop and retry home wifi
        "ssid": "",              # empty = "<hostname>-setup"
        "password": "mav12345",  # fixed WPA2 key (8-63 chars); empty = a new random one each time
        "profile": "mav-hotspot",
    },
    # 0.96" I2C OLED: LTE signal + data use, alternating with a live upload graph.
    "oled": {
        "enabled": True,
        "i2c_bus": 1,
        "i2c_address": 60,       # 0x3C
        "rotate_180": False,
        "controller": "ssd1306",  # or "ssd1309" for 2.42" modules
        "page_seconds": 5,
    },
    # Web page for choosing the link, running a speed test, WiFi setup (needs the password
    # from `sudo mavsphere-conn ui-password`).
    "ui": {
        "enabled": True,
        "port": 8091,
        "bind": "0.0.0.0",
    },
    # Momentary push button to ground: short press cycles auto -> lte -> wifi, hold = setup hotspot.
    "button": {
        "enabled": True,
        "pin": 17,               # GPIO17 (pin 11); wire the other side to GND (pin 9)
        "long_seconds": 3.0,
    },
    # After the active link changes (WiFi <-> LTE) the agent's old connections are dead.
    # Restarting its container makes it reconnect straight away instead of waiting for timeouts.
    "agent": {
        "restart_on_egress_change": True,
        "container": "mavsphere-agent",
        "settle_seconds": 8,
        "min_interval_seconds": 90,
        # Video bitrate written into the agent's config when the link changes (and when the
        # controller first sees a link). The agent needs >= 100000. Set enabled=false to manage it by hand.
        "bitrate": {
            "enabled": True,
            "wifi_bps": 5000000,
            "lte_bps": 150000,
            "api": "http://127.0.0.1:8090",
        },
    },
    # 1.8" ST7735S SPI colour TFT: connectivity + flight-controller status on one screen.
    "tft": {
        "enabled": True,
        "spi_bus": 0,
        "spi_device": 0,         # CE0 = GPIO8 (pin 24)
        "speed_hz": 16000000,
        "dc_pin": 25,            # GPIO25 (pin 22), BCM numbering
        "rst_pin": 24,           # GPIO24 (pin 18)
        "blk_pin": None,         # backlight GPIO, or null if BLK is wired to 3.3V
        "width": 128,
        "height": 160,
        "rotation": 90,          # 0/90/180/270. 90 = landscape (160x128)
        "col_offset": 0,         # shift if you see a stripe/noise on one edge (try 2 / 1, or 26 / 1)
        "row_offset": 0,
        "bgr": False,            # flip if red and blue are swapped
        "invert": False,         # flip if colours look like a negative
        "dashboard_seconds": 8,
        "messages_seconds": 5,
        "setup_seconds": 6,
        "udp_listen": "127.0.0.1:14601",
        "alert_hold_seconds": 60,
        "link_timeout_seconds": 5,
    },
    # Optional second OLED showing flight-controller status. Needs MAVProxy's
    # spare output (--out=udp:127.0.0.1:14601). Off by default.
    "fc_display": {
        "enabled": False,
        "i2c_bus": 1,
        "i2c_address": 61,       # 0x3D, so it can sit next to the 0x3C display
        "rotate_180": False,
        "controller": "ssd1306",  # or "ssd1309" for 2.42" modules
        "udp_listen": "127.0.0.1:14601",
        "alert_hold_seconds": 60,
        "link_timeout_seconds": 5,
    },
}


def _merge(base, user):
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def load_config(path=CONFIG_PATH):
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(path) as f:
            user = json.load(f)
    except (OSError, ValueError):
        return cfg
    _merge(cfg, user)
    return cfg


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_atomic(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def add_request(name):
    """Ask the controller to do something (speedtest | hotspot). Safe to call from any process."""
    os.makedirs(DATA_DIR, exist_ok=True)
    fd = os.open(REQUEST_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    with os.fdopen(fd, "a") as f:
        f.write(name + "\n")


def pop_requests():
    """Return the set of pending request names and clear them."""
    work = REQUEST_FILE + ".work"
    try:
        os.replace(REQUEST_FILE, work)
        with open(work) as f:
            names = {line.strip() for line in f if line.strip()}
        os.unlink(work)
        return names
    except OSError:
        return set()


def run_rc(*args, timeout=20):
    """Run a command; return (returncode, combined output). Never raises."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # missing binary, timeout, ...
        return 127, str(e)


def run(*args, timeout=20):
    rc, out = run_rc(*args, timeout=timeout)
    return out if rc == 0 else ""


def label_for(cfg, iface):
    if iface.startswith("wl"):
        return "WiFi"
    if iface.startswith(tuple(cfg["lte_prefixes"])):
        return "LTE"
    if iface.startswith(("eth", "en")):
        return "Eth"
    return iface
