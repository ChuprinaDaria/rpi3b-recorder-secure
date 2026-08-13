#!/usr/bin/env bash
# One-shot bootstrap: створити LUKS2-контейнер у файлі, відформатувати
# ext4, підготувати точку монтування /mnt/rec.
#
# Ідемпотентний: якщо контейнер уже існує — нічого не переформатовує.
# Пасфраза: береться з --key-file <path>, або згенерується у OPSEC_DIR/pass.txt.
#
# Використання:
#   sudo ./pi/luks_setup.sh                  # авто: 4G, згенерована пасфраза
#   sudo ./pi/luks_setup.sh --size 8G
#   sudo ./pi/luks_setup.sh --key-file /path/to/pass.txt
set -euo pipefail

OPSEC_DIR="${OPSEC_DIR:-/opt/opsec}"
IMG="${IMG:-$OPSEC_DIR/rec.img}"
NAME="${NAME:-rec}"
MNT="${MNT:-/mnt/rec}"
SIZE="${SIZE:-4G}"
KEY_FILE="${KEY_FILE:-$OPSEC_DIR/rec-pass.txt}"
# Обмеження RAM на Pi 3B (921 MB) — 128 MiB Argon2id безпечно для тунелів.
PBKDF_MEM_KB="${PBKDF_MEM_KB:-131072}"
PBKDF_ITER="${PBKDF_ITER:-4}"
PBKDF_PARALLEL="${PBKDF_PARALLEL:-2}"

while [ $# -gt 0 ]; do
  case "$1" in
    --size)     SIZE="$2"; shift 2 ;;
    --key-file) KEY_FILE="$2"; shift 2 ;;
    --img)      IMG="$2"; shift 2 ;;
    --mnt)      MNT="$2"; shift 2 ;;
    -h|--help)
      grep -E '^#' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "невідомий аргумент: $1"; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "потрібен root (sudo)"; exit 1
fi

for t in cryptsetup mkfs.ext4 truncate openssl; do
  command -v "$t" >/dev/null || { echo "нема $t в PATH"; exit 1; }
done

install -d -m 700 "$OPSEC_DIR"

if [ ! -f "$KEY_FILE" ]; then
  umask 077
  openssl rand -base64 32 > "$KEY_FILE"
  chmod 600 "$KEY_FILE"
  echo "згенеровано пасфразу: $KEY_FILE (chmod 600). Збережи копію ОКРЕМО."
fi

if [ -f "$IMG" ]; then
  echo "контейнер уже існує: $IMG — пропускаю format"
else
  echo "створюю контейнер $IMG розміром $SIZE..."
  truncate -s "$SIZE" "$IMG"

  echo "cryptsetup luksFormat (argon2id mem=${PBKDF_MEM_KB}KB iter=$PBKDF_ITER)..."
  nice -n 19 cryptsetup luksFormat \
    --type luks2 \
    --cipher aes-xts-plain64 --key-size 512 \
    --sector-size 4096 \
    --pbkdf argon2id \
    --pbkdf-memory "$PBKDF_MEM_KB" \
    --pbkdf-force-iterations "$PBKDF_ITER" \
    --pbkdf-parallel "$PBKDF_PARALLEL" \
    --batch-mode --key-file "$KEY_FILE" "$IMG"
fi

if [ -e "/dev/mapper/$NAME" ]; then
  echo "мапер уже відкритий: /dev/mapper/$NAME"
else
  echo "cryptsetup open $NAME..."
  cryptsetup open --key-file "$KEY_FILE" "$IMG" "$NAME"
fi

if ! blkid -o value -s TYPE "/dev/mapper/$NAME" >/dev/null 2>&1; then
  echo "mkfs.ext4 -L rec-secret ..."
  mkfs.ext4 -q -L rec-secret "/dev/mapper/$NAME"
fi

install -d -m 755 "$MNT"

if ! mountpoint -q "$MNT"; then
  mount -o noatime "/dev/mapper/$NAME" "$MNT"
  # власник — юзер, який запустив sudo, або pi як fallback.
  OWNER_USER="${SUDO_USER:-pi}"
  OWNER_GRP="$(id -gn "$OWNER_USER" 2>/dev/null || echo "$OWNER_USER")"
  chown "$OWNER_USER:$OWNER_GRP" "$MNT"
fi

df -h "$MNT"
echo
echo "готово. Файли пиши в $MNT — вони будуть зашифровані at-rest."
echo "закрити:  cryptsetup close $NAME  (після umount $MNT)"
echo "знищити:  див. pi/wipe.sh (викликається з BLE 0x05)"
