#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/home/pi/rpi5-auto"
SERVICE_NAME="rpi5-auto-recorder.service"

apt-get update
apt-get install -y ffmpeg v4l-utils

usermod -aG video pi || true

mkdir -p "${APP_DIR}"
install -m 755 "$(dirname "$0")/autostart.sh" "${APP_DIR}/autostart.sh"
chown -R pi:pi "${APP_DIR}"
mkdir -p /home/pi/recordings
chown pi:pi /home/pi/recordings

cat >"/etc/systemd/system/${SERVICE_NAME}" <<UNIT
[Unit]
Description=RPi5 auto video recorder
After=multi-user.target

[Service]
Type=simple
User=pi
Group=video
WorkingDirectory=${APP_DIR}
ExecStart=/usr/bin/env bash ${APP_DIR}/autostart.sh
KillSignal=SIGTERM
TimeoutStopSec=15
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"

systemctl --no-pager status "${SERVICE_NAME}" || true
echo "logs: journalctl -u ${SERVICE_NAME} -f"
