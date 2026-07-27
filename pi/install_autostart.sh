#!/usr/bin/env bash
set -euo pipefail

REC_USER="${REC_USER:-${SUDO_USER:-pi}}"
REC_HOME="$(getent passwd "${REC_USER}" | cut -d: -f6)"
APP_DIR="${REC_HOME}/rpi5-auto"
SERVICE_NAME="rpi5-auto-recorder.service"

apt-get update
apt-get install -y ffmpeg rpicam-apps

usermod -aG video "${REC_USER}" || true

mkdir -p "${APP_DIR}"
install -m 755 "$(dirname "$0")/autostart.sh" "${APP_DIR}/autostart.sh"
chown -R "${REC_USER}:${REC_USER}" "${APP_DIR}"
mkdir -p "${REC_HOME}/recordings"
chown "${REC_USER}:${REC_USER}" "${REC_HOME}/recordings"

cat >"/etc/systemd/system/${SERVICE_NAME}" <<UNIT
[Unit]
Description=RPi5 auto video recorder
After=multi-user.target

[Service]
Type=simple
User=${REC_USER}
Group=video
WorkingDirectory=${APP_DIR}
Environment=HOME=${REC_HOME}
Environment=WIDTH=1920 HEIGHT=1080 FPS=30 BITRATE=10000000
Environment=AUTOFOCUS_MODE=manual LENS_POSITION=0
Environment=SEGMENT_SEC=0
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
