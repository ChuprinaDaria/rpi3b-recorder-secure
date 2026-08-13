#!/usr/bin/env bash
# Форк bluebird-works/rpi5-recorder під Pi 3B: auto-record при boot,
# запис у LUKS-mount, USB webcam.
set -euo pipefail

REC_USER="${REC_USER:-${SUDO_USER:-pi}}"
REC_HOME="$(getent passwd "${REC_USER}" | cut -d: -f6)"
APP_DIR="${APP_DIR:-${REC_HOME}/rpi3b-auto}"
SERVICE_NAME="${SERVICE_NAME:-rpi3b-auto-recorder.service}"
OPSEC_DIR="${OPSEC_DIR:-/opt/opsec}"
MNT="${MNT:-/mnt/rec}"

apt-get update
apt-get install -y --no-install-recommends ffmpeg v4l-utils cryptsetup

usermod -aG video "${REC_USER}" || true

# LUKS bootstrap (idempotent).
"$(dirname "$0")/luks_setup.sh"

mkdir -p "${APP_DIR}"
install -m 755 "$(dirname "$0")/autostart.sh" "${APP_DIR}/autostart.sh"
install -m 755 "$(dirname "$0")/wipe.sh" "${APP_DIR}/wipe.sh"
chown -R "${REC_USER}:${REC_USER}" "${APP_DIR}"

cat >"/etc/systemd/system/${SERVICE_NAME}" <<UNIT
[Unit]
Description=Pi 3B auto video recorder (LUKS-backed, USB webcam)
After=multi-user.target

[Service]
Type=simple
User=${REC_USER}
Group=video
WorkingDirectory=${APP_DIR}
Environment=HOME=${REC_HOME}
Environment=OPSEC_DIR=${OPSEC_DIR}
Environment=MNT=${MNT}
Environment=REC_DIR=${MNT}
Environment=WIDTH=1280 HEIGHT=720 FPS=30 BITRATE=4000000
Environment=SEGMENT_SEC=0
ExecStart=/usr/bin/env bash ${APP_DIR}/autostart.sh
KillSignal=SIGTERM
TimeoutStopSec=15
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"

systemctl --no-pager status "${SERVICE_NAME}" || true
echo "logs: journalctl -u ${SERVICE_NAME} -f"
echo "NB: сервіс НЕ стартує, поки $MNT не змонтований (запусти luks_setup.sh)"
