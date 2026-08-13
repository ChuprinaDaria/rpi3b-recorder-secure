#!/usr/bin/env bash
# Форк bluebird-works/rpi5-recorder під Pi 3B: USB-камера, LUKS-mount,
# WIPE-opcode. Ставить сервіс rpi3b-ble-recorder.
set -euo pipefail

REC_USER="${REC_USER:-${SUDO_USER:-pi}}"
REC_HOME="$(getent passwd "${REC_USER}" | cut -d: -f6)"
APP_DIR="${APP_DIR:-${REC_HOME}/rpi3b-ble}"
SERVICE_NAME="${SERVICE_NAME:-rpi3b-ble-recorder.service}"
OPSEC_DIR="${OPSEC_DIR:-/opt/opsec}"
MNT="${MNT:-/mnt/rec}"
DEVICE_NAME="${DEVICE_NAME:-RPi3B-CAM}"

apt-get update
# ffmpeg + v4l-utils: USB-камера. cryptsetup: LUKS. libhdf5/openblas/gtk: cv-stack залежності
# якщо колись докладеш opencv (можна не ставити для чистого рекордера).
apt-get install -y --no-install-recommends \
  ffmpeg v4l-utils cryptsetup bluez \
  python3-pip python3-dbus python3-gi

if ! apt-get install -y python3-bluezero 2>/dev/null; then
  echo "python3-bluezero not in apt, installing via pip"
  pip3 install --break-system-packages bluezero
fi

# USB-камера — /dev/video0, доступ через group video.
usermod -aG video "${REC_USER}" || true

rfkill unblock bluetooth || true
hciconfig hci0 up || true

# LUKS-контейнер (idempotent — якщо вже є, тільки перевіряє).
"$(dirname "$0")/luks_setup.sh"

mkdir -p "${APP_DIR}"
cp "$(dirname "$0")/ble_recorder.py" "${APP_DIR}/"
install -m 755 "$(dirname "$0")/ble_advertise.sh" "${APP_DIR}/ble_advertise.sh"
install -m 755 "$(dirname "$0")/wipe.sh" "${APP_DIR}/wipe.sh"
chown -R "${REC_USER}:${REC_USER}" "${APP_DIR}"

# sudoers-drop: щоб ble_recorder.py (під ${REC_USER}) міг викликати wipe.sh
# без пароля. Дозволяємо ЛИШЕ конкретний скрипт.
cat >"/etc/sudoers.d/rpi3b-wipe" <<SUDOERS
${REC_USER} ALL=(root) NOPASSWD: ${APP_DIR}/wipe.sh
SUDOERS
chmod 440 "/etc/sudoers.d/rpi3b-wipe"

cat >"/etc/systemd/system/${SERVICE_NAME}" <<UNIT
[Unit]
Description=Pi 3B BLE video recorder (LUKS-backed, USB webcam)
After=bluetooth.target
Requires=bluetooth.target

[Service]
Type=simple
User=${REC_USER}
SupplementaryGroups=video
WorkingDirectory=${APP_DIR}
Environment=HOME=${REC_HOME}
Environment=OPSEC_DIR=${OPSEC_DIR}
Environment=MNT=${MNT}
Environment=REC_DIR=${MNT}
Environment=DEVICE_NAME=${DEVICE_NAME}
Environment=WIDTH=1280 HEIGHT=720 FPS=30 BITRATE=4000000
Environment=SEGMENT_SEC=0
Environment=WIPE_SCRIPT=${APP_DIR}/wipe.sh
ExecStartPre=+/usr/sbin/rfkill unblock bluetooth
ExecStartPre=+/usr/bin/hciconfig hci0 up
ExecStart=/usr/bin/python3 ${APP_DIR}/ble_recorder.py
ExecStartPost=+/bin/bash -c 'sleep 3; DEVICE_NAME=${DEVICE_NAME} ${APP_DIR}/ble_advertise.sh start'
ExecStopPost=+${APP_DIR}/ble_advertise.sh stop
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"

systemctl --no-pager status "${SERVICE_NAME}" || true
echo
echo "logs: journalctl -u ${SERVICE_NAME} -f"
echo "webUI: https://<твій-github-pages>/  (Web Bluetooth — LAN-only за архітектурою)"
