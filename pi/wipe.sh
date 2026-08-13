#!/usr/bin/env bash
# Знищення LUKS-контейнера рекордера. Викликається з ble_recorder.py
# у відповідь на opcode 0x05 (WIPE_CONFIRM) з валідною пасфразою.
#
# Кроки:
#   1. Kill усе, що тримає mountpoint
#   2. umount /mnt/rec
#   3. cryptsetup close rec (ключ з RAM)
#   4. cryptsetup luksErase (все keyslot-и, ~350 мс)
#   5. dd if=/dev/urandom of=rec.img bs=1M count=16 conv=notrunc (хедер)
#   6. touch destroyed-marker → STATUS_EXT показує 'destroyed'
#
# Не чіпає жодного файлу поза $IMG і $MNT.
set -u

OPSEC_DIR="${OPSEC_DIR:-/opt/opsec}"
IMG="${IMG:-$OPSEC_DIR/rec.img}"
NAME="${NAME:-rec}"
MNT="${MNT:-/mnt/rec}"
MARKER="${MARKER:-$OPSEC_DIR/destroyed}"

log() { printf '[wipe %s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

if [ "$(id -u)" -ne 0 ]; then
  log "потрібен root"; exit 1
fi

# 1. звільняємо mountpoint
if mountpoint -q "$MNT"; then
  log "fuser -km $MNT"
  fuser -km "$MNT" 2>/dev/null || true
  sleep 0.5
fi

# 2. umount
if mountpoint -q "$MNT"; then
  if ! umount "$MNT" 2>&1; then
    log "lazy umount"
    umount -l "$MNT" || true
  fi
fi

# 3. close mapper
if [ -e "/dev/mapper/$NAME" ]; then
  cryptsetup close "$NAME" 2>&1 || {
    log "close failed, спробую ще раз через 1с"
    sleep 1; cryptsetup close "$NAME" || true
  }
fi

# 4. luksErase — всі keyslot-и стають випадковим шумом
if [ -f "$IMG" ]; then
  log "luksErase $IMG"
  cryptsetup luksErase --batch-mode "$IMG" || {
    log "luksErase FAIL"; exit 3
  }

  # 5. wipe LUKS header (16 MiB — покриває первинний + вторинний)
  log "dd urandom 16M header wipe"
  dd if=/dev/urandom of="$IMG" bs=1M count=16 conv=notrunc status=none || {
    log "header wipe FAIL"; exit 4
  }
fi

# 6. marker для STATUS_EXT
touch "$MARKER"
sync

log "DONE. $IMG вже не LUKS, ключі математично неможливо відновити."
cryptsetup isLuks "$IMG" 2>&1 && log "WARN: isLuks каже так — щось пішло не так" || \
  log "isLuks: NO (як і треба)"

exit 0
