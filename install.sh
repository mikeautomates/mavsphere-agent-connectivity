#!/usr/bin/env bash
# Run on the Pi from this folder:  sudo bash install.sh
set -e
[ "$EUID" -eq 0 ] || { echo "Run with sudo"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"

apt-get update
apt-get install -y python3-smbus python3-pil python3-spidev python3-lgpio i2c-tools iw fonts-dejavu-core network-manager curl iproute2
apt-get install -y dnsmasq-base        # lets NetworkManager hand out IPs on the setup hotspot
apt-get install -y linux-modules-extra-raspi || true   # i2c modules on some Ubuntu releases

# Enable SPI for the TFT (and I2C for the optional OLEDs)
CFG=/boot/firmware/config.txt
grep -q '^dtparam=spi=on' "$CFG" || echo 'dtparam=spi=on' >> "$CFG"
grep -q '^dtparam=i2c_arm=on' "$CFG" || echo 'dtparam=i2c_arm=on' >> "$CFG"
echo i2c-dev > /etc/modules-load.d/i2c-dev.conf

# Code
install -d /opt/mavsphere-connectivity
install -m 644 "$HERE/conn_common.py" "$HERE/portal.py" "$HERE/oled_driver.py" "$HERE/tft_driver.py" "$HERE/shaper.py" "$HERE/modem.py" "$HERE/egress.py" "$HERE/webui.py" "$HERE/fc_parser.py" /opt/mavsphere-connectivity/
install -m 755 "$HERE/controller.py" "$HERE/oled_lte.py" "$HERE/button.py" "$HERE/tft_display.py" "$HERE/fc_display.py" "$HERE/connctl.py" /opt/mavsphere-connectivity/
ln -sf /opt/mavsphere-connectivity/connctl.py /usr/local/bin/mavsphere-conn
install -d /var/lib/mavsphere-connectivity
echo sch_cake > /etc/modules-load.d/sch_cake.conf     # CAKE traffic shaper for the LTE uplink
modprobe sch_cake || echo "WARNING: sch_cake not available - LTE shaping will stay off"

# Setup hotspot: answer every DNS name with the Pi so phones pop the setup page up
# (applies to NetworkManager "shared" connections, i.e. our setup hotspot only)
install -d /etc/NetworkManager/dnsmasq-shared.d
echo 'address=/#/10.42.0.1' > /etc/NetworkManager/dnsmasq-shared.d/mavsphere-captive.conf

# Config (never overwrite an existing one)
install -d /etc/mavsphere-connectivity
[ -f /etc/mavsphere-connectivity/config.json ] || install -m 644 "$HERE/config.example.json" /etc/mavsphere-connectivity/config.json

# Replace the old single-file OLED service if it is there
if systemctl list-unit-files | grep -q '^oled-ip.service'; then
  systemctl disable --now oled-ip.service || true
fi

cat > /etc/systemd/system/mavsphere-connectivity.service <<'EOF'
[Unit]
Description=MavSphere connectivity controller
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
ExecStart=/usr/bin/python3 /opt/mavsphere-connectivity/controller.py
Restart=always
RestartSec=5
RuntimeDirectory=mavsphere-connectivity
RuntimeDirectoryMode=0755
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/mavsphere-tft.service <<'EOF'
[Unit]
Description=MavSphere colour TFT dashboard (connectivity + flight controller)
After=mavsphere-connectivity.service
Wants=mavsphere-connectivity.service

[Service]
ExecStart=/usr/bin/python3 /opt/mavsphere-connectivity/tft_display.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/mavsphere-oled.service <<'EOF'
[Unit]
Description=MavSphere LTE OLED (signal, data use, upload graph)
After=mavsphere-connectivity.service
Wants=mavsphere-connectivity.service

[Service]
ExecStart=/usr/bin/python3 /opt/mavsphere-connectivity/oled_lte.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/mavsphere-button.service <<'EOF'
[Unit]
Description=MavSphere mode button (short = cycle link mode, hold = setup hotspot)
After=mavsphere-connectivity.service

[Service]
ExecStart=/usr/bin/python3 /opt/mavsphere-connectivity/button.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/mavsphere-fc-oled.service <<'EOF'
[Unit]
Description=MavSphere flight-controller status OLED
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/mavsphere-connectivity/fc_display.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now mavsphere-connectivity.service
systemctl enable mavsphere-tft.service
systemctl restart mavsphere-tft.service || true
# OLED services are legacy and idle unless enabled in the config. Never enable
# fc_display together with the TFT: both listen on UDP 14601.
systemctl disable --now mavsphere-fc-oled.service 2>/dev/null || true   # legacy, conflicts with the TFT
systemctl enable mavsphere-oled.service mavsphere-button.service
systemctl restart mavsphere-oled.service mavsphere-button.service || true
echo
echo "Done. If SPI was only just enabled, reboot once:  sudo reboot"
echo "Status: mavsphere-conn status      Link: sudo mavsphere-conn mode auto|wifi|lte      LTE test: sudo mavsphere-conn speedtest"
echo "State:  cat /run/mavsphere-connectivity/state.json"
echo "Config: /etc/mavsphere-connectivity/config.json (see README.md)"
echo "Web page:  http://<this-pi>:8091   password: sudo mavsphere-conn ui-password"
echo "Button:    GPIO17 (pin 11) to GND (pin 9): short press = cycle mode, hold 3 s = setup hotspot"
echo "Colours or edges wrong?  sudo systemctl stop mavsphere-tft && sudo python3 /opt/mavsphere-connectivity/tft_display.py --test"
