#!/usr/bin/env bash
# Знищення всього секретного периметру Pi: усі LUKS-контейнери у $OPSEC_DIR.
# Викликається з lan_server.py у відповідь на /api/wipe з валідною пасфразою.
#
# Цілі (треба щоб порядок був: спершу umount, потім close, потім erase+dd):
#   /opt/opsec/rec.img  → mapper 'rec' → /mnt/rec  (відео)
#   /opt/opsec/cv.img   → mapper 'cv'  → /mnt/cv   (cv-tracking + venv)
#   і будь-який інший *.img у $OPSEC_DIR (буде оброблений без mapper/mount
#   якщо ми його не знаємо — просто luksErase + dd)
#
# Кроки на КОЖЕН контейнер:
#   1. fuser -km $MNT (звільнити mountpoint від процесів)
#   2. umount $MNT
#   3. cryptsetup close $NAME (ключ з RAM)
#   4. cryptsetup luksErase (всі keyslot-и стають шумом, ~350 мс)
#   5. dd if=/dev/urandom of=$IMG bs=1M count=16 conv=notrunc (хедер)
# Після успішного циклу — touch $MARKER, sync.
set -u

OPSEC_DIR="${OPSEC_DIR:-/opt/opsec}"
MARKER="${MARKER:-$OPSEC_DIR/destroyed}"

# Реєстр відомих контейнерів: img|mapper|mount
declare -a TARGETS=(
  "$OPSEC_DIR/rec.img|rec|/mnt/rec"
  "$OPSEC_DIR/cv.img|cv|/mnt/cv"
)

log() { printf '[wipe %s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

if [ "$(id -u)" -ne 0 ]; then
  log "потрібен root"; exit 1
fi

wipe_one() {
  local img="$1" name="$2" mnt="$3"

  if [ ! -f "$img" ]; then
    log "$img: не існує, пропускаю"
    return 0
  fi

  log "── target: $img (mapper=$name, mount=$mnt) ──"

  # 1+2. umount якщо змонтований
  if [ -n "$mnt" ] && mountpoint -q "$mnt" 2>/dev/null; then
    log "  fuser -km $mnt"
    fuser -km "$mnt" 2>/dev/null || true
    sleep 0.4
    if ! umount "$mnt" 2>/dev/null; then
      log "  lazy umount $mnt"
      umount -l "$mnt" 2>/dev/null || true
    fi
  fi

  # 3. close mapper
  if [ -n "$name" ] && [ -e "/dev/mapper/$name" ]; then
    log "  cryptsetup close $name"
    cryptsetup close "$name" 2>/dev/null || {
      sleep 0.5
      cryptsetup close "$name" 2>/dev/null || log "  WARN: close $name не вдалось"
    }
  fi

  # 4. luksErase — стирає ВСІ keyslot-и
  log "  cryptsetup luksErase $img"
  if ! cryptsetup luksErase --batch-mode "$img"; then
    log "  ERROR: luksErase $img"
    return 1
  fi

  # 5. dd urandom 16M header
  log "  dd urandom 16M header wipe"
  if ! dd if=/dev/urandom of="$img" bs=1M count=16 conv=notrunc status=none; then
    log "  ERROR: header wipe $img"
    return 1
  fi

  # verify
  if cryptsetup isLuks "$img" 2>/dev/null; then
    log "  WARN: isLuks каже так — щось пішло не так з $img"
  else
    log "  OK: $img більше не LUKS"
  fi
  return 0
}

RC=0
for row in "${TARGETS[@]}"; do
  IFS='|' read -r img name mnt <<< "$row"
  wipe_one "$img" "$name" "$mnt" || RC=1
done

# Якщо у $OPSEC_DIR є ще якісь *.img, які ми не знаємо — теж їх обробимо
# (тільки luksErase + dd, без umount/close, бо не знаємо mapper name/mount).
shopt -s nullglob
for extra in "$OPSEC_DIR"/*.img; do
  known=0
  for row in "${TARGETS[@]}"; do
    IFS='|' read -r img _ _ <<< "$row"
    [ "$extra" = "$img" ] && { known=1; break; }
  done
  [ "$known" -eq 1 ] && continue
  log "── unknown target: $extra (best-effort) ──"
  cryptsetup luksErase --batch-mode "$extra" 2>/dev/null || true
  dd if=/dev/urandom of="$extra" bs=1M count=16 conv=notrunc status=none 2>/dev/null || true
done

touch "$MARKER"
sync
log "DONE (rc=$RC). Всі відомі LUKS-контейнери у $OPSEC_DIR затерто."
exit $RC
