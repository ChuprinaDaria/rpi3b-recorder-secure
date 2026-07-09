#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/home/pi/rpi5-ble"
SERVICE_NAME="rpi5-ble-recorder.service"

apt-get update
apt-get install -y ffmpeg bluez v4l-utils python3-pip python3-dbus python3-gi

# bluezero: try apt first, fall back to pip (PEP 668 override — dedicated Pi)
if ! apt-get install -y python3-bluezero 2>/dev/null; then
  echo "python3-bluezero not in apt, installing via pip"
  pip3 install --break-system-packages bluezero
fi

# put pi in video group so it can access /dev/video0
usermod -aG video pi || true

mkdir -p "${APP_DIR}"
cp "$(dirname "$0")/ble_recorder.py" "${APP_DIR}/"
chown -R pi:pi "${APP_DIR}"
mkdir -p /home/pi/recordings
chown pi:pi /home/pi/recordings

cat >"/etc/systemd/system/${SERVICE_NAME}" <<UNIT
[Unit]
Description=RPi5 BLE video recorder
After=bluetooth.target
Requires=bluetooth.target

[Service]
Type=simple
User=pi
WorkingDirectory=${APP_DIR}
ExecStart=/usr/bin/python3 ${APP_DIR}/ble_recorder.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"

systemctl --no-pager status "${SERVICE_NAME}" || true
echo "logs: journalctl -u ${SERVICE_NAME} -f"
