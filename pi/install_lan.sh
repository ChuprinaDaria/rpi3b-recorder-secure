#!/usr/bin/env bash
# LAN-only HTTP-сервер для secure recorder. Без BLE.
# Слухає 0.0.0.0:8080. nftables обмежує до 192.168.1.0/24.
set -euo pipefail

REC_USER="${REC_USER:-${SUDO_USER:-pi}}"
REC_HOME="$(getent passwd "${REC_USER}" | cut -d: -f6)"
APP_DIR="${APP_DIR:-${REC_HOME}/rpi3b-lan}"
SERVICE_NAME="${SERVICE_NAME:-rpi3b-lan-recorder.service}"
OPSEC_DIR="${OPSEC_DIR:-/opt/opsec}"
MNT="${MNT:-/mnt/rec}"
LISTEN_PORT="${LISTEN_PORT:-8080}"
LAN_CIDR="${LAN_CIDR:-192.168.1.0/24}"

apt-get update
apt-get install -y --no-install-recommends \
  ffmpeg v4l-utils cryptsetup python3-flask nftables inotify-tools

usermod -aG video "${REC_USER}" || true
# systemd-journal: щоб journalctl під ${REC_USER} бачив логи інших сервісів
# (без цього — 'Hint: not seeing messages from other users').
usermod -aG systemd-journal "${REC_USER}" || true

# LUKS bootstrap (idempotent).
"$(dirname "$0")/luks_setup.sh"

mkdir -p "${APP_DIR}"
cp "$(dirname "$0")/lan_server.py" "${APP_DIR}/"
install -m 755 "$(dirname "$0")/wipe.sh" "${APP_DIR}/wipe.sh"
# index.html лежить у корені репо — копіюємо поруч, Flask роздає її як /
cp "$(dirname "$0")/../index.html" "${APP_DIR}/"
chown -R "${REC_USER}:${REC_USER}" "${APP_DIR}"

# sudoers-drop: тільки для wipe.sh, без пароля.
cat >"/etc/sudoers.d/rpi3b-wipe" <<SUDOERS
${REC_USER} ALL=(root) NOPASSWD: ${APP_DIR}/wipe.sh
SUDOERS
chmod 440 "/etc/sudoers.d/rpi3b-wipe"

# nftables: приймаємо TCP $LISTEN_PORT тільки з LAN. Дефолтна політика хосту
# лишається як була — ми лише додаємо inet-таблицю rpi3b-lan.
nft list table inet rpi3b-lan >/dev/null 2>&1 || nft add table inet rpi3b-lan
nft 'add chain inet rpi3b-lan input { type filter hook input priority -100; policy accept; }' 2>/dev/null || true
nft flush chain inet rpi3b-lan input
nft add rule inet rpi3b-lan input tcp dport "${LISTEN_PORT}" ip saddr "${LAN_CIDR}" accept
nft add rule inet rpi3b-lan input tcp dport "${LISTEN_PORT}" drop

cat >"/etc/systemd/system/${SERVICE_NAME}" <<UNIT
[Unit]
Description=Pi 3B LAN secure recorder (Flask + LUKS + USB webcam)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${REC_USER}
SupplementaryGroups=video systemd-journal
WorkingDirectory=${APP_DIR}
Environment=HOME=${REC_HOME}
Environment=OPSEC_DIR=${OPSEC_DIR}
Environment=MNT=${MNT}
Environment=REC_DIR=${MNT}
Environment=STATIC_DIR=${APP_DIR}
Environment=WIPE_SCRIPT=${APP_DIR}/wipe.sh
Environment=SERVICE_NAME=${SERVICE_NAME}
Environment=LISTEN_HOST=0.0.0.0
Environment=LISTEN_PORT=${LISTEN_PORT}
ExecStart=/usr/bin/python3 ${APP_DIR}/lan_server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"

sleep 1
systemctl --no-pager status "${SERVICE_NAME}" || true
echo
echo "LAN URL:  http://$(hostname -I | awk '{print $1}'):${LISTEN_PORT}/"
echo "logs:     journalctl -u ${SERVICE_NAME} -f"
echo "firewall: nft list table inet rpi3b-lan"
