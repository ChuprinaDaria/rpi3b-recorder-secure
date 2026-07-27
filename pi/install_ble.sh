#!/usr/bin/env bash
set -euo pipefail

REC_USER="${REC_USER:-${SUDO_USER:-pi}}"
REC_HOME="$(getent passwd "${REC_USER}" | cut -d: -f6)"
APP_DIR="${REC_HOME}/rpi5-ble"
SERVICE_NAME="rpi5-ble-recorder.service"

apt-get update
apt-get install -y ffmpeg rpicam-apps bluez python3-pip python3-dbus python3-gi

# bluezero: try apt first, fall back to pip (PEP 668 override — dedicated Pi)
if ! apt-get install -y python3-bluezero 2>/dev/null; then
  echo "python3-bluezero not in apt, installing via pip"
  pip3 install --break-system-packages bluezero
fi

# доступ до CSI-камери йде через групу video
usermod -aG video "${REC_USER}" || true

# на свіжому образі адаптер часто лежить DOWN і soft-blocked
rfkill unblock bluetooth || true
hciconfig hci0 up || true

mkdir -p "${APP_DIR}"
cp "$(dirname "$0")/ble_recorder.py" "${APP_DIR}/"
install -m 755 "$(dirname "$0")/ble_advertise.sh" "${APP_DIR}/ble_advertise.sh"
chown -R "${REC_USER}:${REC_USER}" "${APP_DIR}"
mkdir -p "${REC_HOME}/recordings"
chown "${REC_USER}:${REC_USER}" "${REC_HOME}/recordings"

cat >"/etc/systemd/system/${SERVICE_NAME}" <<UNIT
[Unit]
Description=RPi5 BLE video recorder
After=bluetooth.target
Requires=bluetooth.target

[Service]
Type=simple
User=${REC_USER}
SupplementaryGroups=video
WorkingDirectory=${APP_DIR}
Environment=HOME=${REC_HOME}
Environment=WIDTH=1920 HEIGHT=1080 FPS=30 BITRATE=10000000
Environment=AUTOFOCUS_MODE=manual LENS_POSITION=0
Environment=SEGMENT_SEC=0
# «+» = виконати від root, бо сам сервіс крутиться під ${REC_USER}
ExecStartPre=+/usr/sbin/rfkill unblock bluetooth
ExecStartPre=+/usr/bin/hciconfig hci0 up
ExecStart=/usr/bin/python3 ${APP_DIR}/ble_recorder.py
# Рекламу піднімає окремий скрипт — bluetoothd на Pi 4 це зробити не може,
# деталі в шапці ble_advertise.sh
ExecStartPost=+/bin/bash -c 'sleep 3; DEVICE_NAME=RPi5-CAM ${APP_DIR}/ble_advertise.sh start'
ExecStopPost=+${APP_DIR}/ble_advertise.sh stop
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"

systemctl --no-pager status "${SERVICE_NAME}" || true
echo "logs: journalctl -u ${SERVICE_NAME} -f"
