# MavSphere connectivity controller (optional add-on)

Keeps the Pi reachable and shows link status and flight-controller status on a 1.8" colour TFT. The MavSphere
agent does not depend on it; run your own controller instead if you prefer.

## Parts
- `controller.py` – detects WiFi / Ethernet / LTE, works out which link real
  traffic leaves through, writes the state file, and runs a setup hotspot when
  WiFi is missing.
- `portal.py` – the small WiFi setup web page (standard library only), served
  only while the hotspot is up.
- `tft_display.py` + `tft_driver.py` – the colour TFT dashboard (read-only): links, which one carries data, FC mode / armed / GPS / battery / messages, and the setup-hotspot details.
- `oled_lte.py` – 0.96" I2C OLED: LTE signal bars + today's data use, alternating with a live upload-speed graph.
- `webui.py` – connectivity web page (port 8091, password). `button.py` – physical button. `modem.py` – LTE signal / data use / rates. `egress.py` – restarts the agent when the active link changes.
- `fc_display.py` – legacy optional second OLED: flight-controller mode, armed state,
  GPS / battery and FC messages (read-only; see below). `fc_parser.py` is its
  small dependency-free MAVLink reader and `oled_driver.py` the shared driver.
- `config.json` – `/etc/mavsphere-connectivity/config.json`; see `config.example.json`.
  `i2c_address` is decimal (60 = 0x3C).

## Colour TFT (ST7735S 1.8", 128x160)
Wiring (Pi 4B, physical pins):

| TFT | Pi |
|-----|----|
| VCC | 3.3V (pin 17) |
| GND | GND (pin 20) |
| SCL / SCK | GPIO11 (pin 23) |
| SDA / MOSI | GPIO10 (pin 19) |
| CS | GPIO8 / CE0 (pin 24) |
| DC / A0 | GPIO25 (pin 22) |
| RES / RST | GPIO24 (pin 18) |
| BLK / LED | 3.3V (pin 1) for always-on, or a GPIO and set `blk_pin` |

Pages (rotates automatically; landscape by default, set `"rotation": 0` for portrait):
- **Dashboard** (8 s): header with `via WiFi` (green) / `via LTE` (orange) / `NO NET` (red); one line per link, `*` marks the link carrying data; mode + `ARMED` (red) / `DISARM` (green); GPS fix, satellites, battery; newest FC messages coloured by severity (red critical incl. `PreArm:`, orange error, amber warning, grey info).
- **FC messages** (5 s): only appears when there are more messages than the dashboard has room for.
- **Setup** (6 s): only while the setup hotspot is up: SSID, big password, page address.
- **Connecting**: replaces the others while the Pi joins your WiFi.

Calibration: if colours look wrong or an edge shows noise, run
`sudo systemctl stop mavsphere-tft && sudo python3 /opt/mavsphere-connectivity/tft_display.py --test`.
It should show red, green, blue, white bars with a yellow border. Red/blue swapped -> `"bgr": true`.
Negative-looking -> `"invert": true`. Noise or a missing line on one edge -> try `col_offset` / `row_offset` 1 or 2
(some panels need 2 and 1, or 26 and 1). Then `sudo systemctl restart mavsphere-tft`.
Blank screen: check `sudo systemctl status mavsphere-tft`, that `ls /dev/spidev0.0` exists (reboot once after
install), and the wiring above.

The TFT listens on MAVProxy's spare output `udp:127.0.0.1:14601`. Do not enable the legacy `fc_display`
OLED at the same time (same port).

## Controls: web page, button, OLED
- **Web page:** `http://<pi-ip>:8091` (works on WiFi, and over the LTE address only from the Pi itself). Sign in with
  the password from `sudo mavsphere-conn ui-password` (`--reset` makes a new one). Choose AUTO / WIFI / LTE, see signal,
  upload speed, shaping, today's data use, the recommended video bitrate, start a speed test, start or stop the setup
  hotspot, and join a different WiFi (`/wifi`). It is plain HTTP: keep it to networks you trust. Five wrong passwords
  lock that address out for a minute.
- **Button:** a momentary switch between GPIO17 (pin 11) and GND (pin 9). Short press cycles auto -> lte -> wifi;
  hold 3 s starts or stops the setup hotspot (WiFi drops while it is on). The TFT shows the mode.
- **OLED:** page 1 signal bars, dBm and today's data (up / down); page 2 the upload speed over the last few minutes with
  the CAKE limit as a dotted line. Signal comes from the modem's AT port (`lte.modem_at_port`, default `/dev/ttyUSB2`).
- **Link changes:** when the active link changes (WiFi <-> LTE, by hand, by the button, or because WiFi dropped) the
  connectivity service restarts the agent container after the new link has been stable for 8 s, so the agent
  reconnects straight away instead of waiting for old connections to time out. No Pi reboot is needed. Set
  `agent.restart_on_egress_change` to false to turn this off. It never fires at startup or more than once per 90 s.

**Video bitrate per link.** When the active link changes (and once when the controller first sees a link), the
service also writes the video bitrate for that link into the agent's config through the agent's own API
(`127.0.0.1:8090`) and restarts the agent so it takes effect: `agent.bitrate.wifi_bps` (default 5000000) on WiFi,
`agent.bitrate.lte_bps` (default 150000) on LTE. Only `h264BitrateBps` and `webrtcMaxBitrateBps` are changed; the
pairing token is untouched. The agent rejects values below 100000, so that is the floor. Set
`agent.bitrate.enabled` to false to manage the bitrate by hand. A bitrate edited by hand in the agent UI is kept
until the next link change.
- **Agent page hint:** `agent-lte-hint.patch` (in this package; apply it in the agent repo with `git apply`, then rebuild the image) adds a small panel to the agent's settings page showing
  the active link and the recommended LTE bitrate, with buttons that fill in the H.264 and WebRTC max fields.
  It reads `http://127.0.0.1:8091/api/lte-hint`, which the Pi itself can read without a login.

## Link choice, LTE speed test and CAKE shaping
`mavsphere-conn status | mode auto|wifi|lte | speedtest` (mode and speedtest need sudo).
- **auto** (default): WiFi preferred, LTE is the fallback. **lte**: LTE carries traffic even when WiFi is up
  (testing, or poor WiFi). **wifi**: LTE gets no default route and can never carry traffic. The mode survives
  reboots. It works by setting NetworkManager route metrics, so existing connections are never dropped.
  A future physical switch only needs to write the same mode file.
- **CAKE** is installed on the LTE interface while LTE is the active link (`lte.shaping.when`: `"up"` = whenever
  LTE is up). It limits upload to `share` (85%) of the measured upload speed so the queue stays in the Pi, not in
  the modem/carrier buffer. That keeps live video latency low and lets WebRTC's own rate control work properly.
  Needs the `sch_cake` kernel module (the installer checks; `linux-modules-extra-raspi`). Shapes upload only.
- **Upload measurement:** a ~3 MB upload through the LTE interface to `lte.speedtest.url` when LTE becomes the
  active link, then every `retest_minutes` (30) while it stays active, plus on request. It is skipped while the link
  is already sending more than `busy_kbit` (a stream is running), capped at `daily_mb_cap` (60 MB/day) for automatic
  tests, and an improvement is only half-trusted while a drop is taken in full. The last result is saved, so a
  reboot starts from it; before any measurement `fallback_kbit` (2000) is used. A failed test keeps the old value.
  Run `sudo mavsphere-conn speedtest` while on WiFi (LTE idle) to pre-measure; it uses about 3 MB of LTE data.
- **For the agent:** state.json has `link_mode` and `lte` (`upload_kbps`, `shaping`, `recommended_video_kbps` =
  25% of upload). The agent can read it, and when `egress.label` is `LTE` cap its video bitrate (e.g. WebRTC max)
  to `recommended_video_kbps`. Until then set the agent's max bitrate for LTE by hand.

## Install
`sudo bash install.sh` then reboot if SPI was just enabled. 

## Setup hotspot and WiFi page
No WiFi connection for `grace_seconds` (150) -> hotspot `<hostname>-setup` with a
random password. The TFT alternates between normal status and the SSID /
password / address. Join it on your phone: most phones open the setup page by
themselves (otherwise browse to `http://10.42.0.1`). Pick your home network,
enter its password, press Connect.

- The hotspot drops while the Pi tries to join; the screen shows "CONNECTING".
- If joining fails (usually a wrong password) the hotspot comes back with the
  **same** password and the page shows an error.
- With no clients for `retry_seconds` (300) it stops and retries saved WiFi.
- The page links to the agent config UI (`http://10.42.0.1:8090/`).
- The page is bound to the hotspot address only, never your home network.
- The network list comes from a scan taken just before the hotspot starts (one
  radio cannot scan while acting as an access point); you can also type a name.
- Anyone who can read the screen can join, so this relies on physical access.
- Joined networks are saved as NetworkManager profiles named `mavsphere-<ssid>`.

## Flight-controller OLED (legacy, optional)
Shows mode, ARMED / DISARM, GPS fix + satellites, battery, and the latest FC
messages. It listens on MAVProxy's spare output `udp:127.0.0.1:14601` (set up by
the agent's `setup_pi.sh`), so it works without the agent and sends nothing to
the FC. Enable it in `/etc/mavsphere-connectivity/config.json`
(`"fc_display": {"enabled": true, ...}`) then
`sudo systemctl restart mavsphere-fc-oled`.

- Message markers: `!` critical/emergency (this includes ArduPilot `PreArm:`
  failures), `E` error, `W` warning; notices/info have no marker. Alerts stay
  on screen for `alert_hold_seconds` (60) at the bottom; info fills the rest.
- `NO FC LINK` means no vehicle heartbeat for `link_timeout_seconds` (5):
  check `systemctl status mavproxy` and that MAVProxy has the spare output.
- The agent should connect to MAVProxy (`udpclient:127.0.0.1:14600`), not open
  the serial port itself, or the two will fight over the port.
- Understands ArduPilot Plane, Copter, Rover and Sub mode names; other modes
  show as `MODE <n>`.
- **Wiring:** same SDA/SCL/3.3V/GND as the first display, in parallel. The two
  displays need different I2C addresses. Default here is `0x3D` (61); on
  0.96" modules move the address resistor from R11 (0x78) to R12 (0x7A). Check
  with `sudo i2cdetect -y 1` (expect `3c` and `3d`).
- **2.42" SSD1309 modules:** set `"controller": "ssd1309"`. They are usually
  shipped set to SPI; move the two 0R resistors to the I2C position, and the DC
  pin selects the address (low 0x3C, high 0x3D).

## State contract
`/run/mavsphere-connectivity/state.json` (world-readable, no secrets):

```json
{
  "version": 1,
  "timestamp": 1764000000.0,
  "hostname": "mav01",
  "egress": {"iface": "wlan0", "label": "WiFi"},
  "links": [{"iface": "wlan0", "label": "WiFi", "ip": "192.168.68.64",
             "ssid": "HOME", "default": true}],
  "hotspot": {"active": false, "ssid": "", "ip": "", "portal_url": "", "ui_url": ""},
  "setup": {"status": "idle", "target_ssid": "", "message": ""},
  "link_mode": "auto",
  "lte": {"present": true, "iface": "enx1a85f3e1e38d", "active": false, "upload_kbps": 4363,
          "measured_at": 1764000000.0, "test": "idle",
          "shaping": {"status": "off", "kbit": null}, "recommended_video_kbps": 2617,
          "signal": {"csq": 19, "dbm": -75, "bars": 4},
          "usage": {"day": "2026-11-26", "tx_mb": 12.3, "rx_mb": 111.1, "total_mb": 123.4},
          "tx_kbps": 0, "rx_kbps": 0},
  "agent": {"last_restart": null, "reason": ""}
}
```
`egress.label` is null when there is no route to the internet.
`setup.status` is `idle`, `connecting` or `failed`. The hotspot password lives
only in `hotspot.json` (mode 0600).

## Troubleshooting
- `journalctl -u mavsphere-connectivity -f`
- `journalctl -u mavsphere-oled -f`
- `sudo i2cdetect -y 1` should show `3c`.
- `journalctl -u mavsphere-connectivity` shows `[hotspot]`, `[portal]` and `[join]` lines.

## Fixed hotspot password

The setup hotspot (`<hostname>-setup`) uses the fixed password `hotspot.password` (default `mav12345`; change it in
`/etc/mavsphere-connectivity/config.json`, 8 to 63 characters, then restart the service). Set it to an empty string
for a new random password each time the hotspot starts, shown on the TFT. Anyone who knows it can join the hotspot and reach the agent's config page
(port 8090, no login), so pick something not easily guessed.
