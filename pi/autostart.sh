#!/usr/bin/env bash
# Auto-record mode для Pi 3B + USB webcam + LUKS-mount.
# Форк bluebird-works/rpi5-recorder: запис піде лише якщо /mnt/rec змонтований.
set -euo pipefail

OPSEC_DIR="${OPSEC_DIR:-/opt/opsec}"
MNT="${MNT:-/mnt/rec}"
DESTROYED_MARKER="${DESTROYED_MARKER:-$OPSEC_DIR/destroyed}"
REC_DIR="${REC_DIR:-$MNT}"

VIDEO_DEV="${VIDEO_DEV:-/dev/video0}"
WIDTH="${WIDTH:-1280}"
HEIGHT="${HEIGHT:-720}"
FPS="${FPS:-30}"
BITRATE="${BITRATE:-4000000}"
SEGMENT_SEC="${SEGMENT_SEC:-0}"
MAX_FILES="${MAX_FILES:-50}"
FREE_MB_MIN="${FREE_MB_MIN:-200}"
SYNC_INTERVAL_SEC="${SYNC_INTERVAL_SEC:-3}"

# Pre-flight: LUKS має бути змонтований і не знищений — інакше писали б у голий rootfs.
if [ -e "$DESTROYED_MARKER" ]; then
  echo "STOP: LUKS-контейнер знищений ($DESTROYED_MARKER існує)"
  exit 2
fi
if ! mountpoint -q "$MNT"; then
  echo "STOP: $MNT не змонтований — запусти spочатку pi/luks_setup.sh"
  exit 2
fi

if [ ! -e "$VIDEO_DEV" ]; then
  echo "STOP: нема $VIDEO_DEV — USB-камера не встромлена?"
  exit 3
fi

mkdir -p "${REC_DIR}"

rotate_loop() {
  while true; do
    sleep 30
    mapfile -t old < <(ls -1t "${REC_DIR}"/rec_*.mp4 2>/dev/null | tail -n +"$((MAX_FILES+1))")
    for f in "${old[@]:-}"; do
      [ -n "$f" ] && rm -f "$f" && echo "rotated $f"
    done
    free_mb=$(df -Pm "${REC_DIR}" | awk 'NR==2 {print $4}')
    if [ "${free_mb}" -lt "${FREE_MB_MIN}" ]; then
      echo "low space (<${FREE_MB_MIN}MB), forcing extra rotation"
      ls -1t "${REC_DIR}"/rec_*.mp4 2>/dev/null | tail -n 5 | xargs -r rm -f
    fi
  done
}

sync_loop() {
  while true; do
    sleep "${SYNC_INTERVAL_SEC}"
    sync
  done
}

# Pi 3B/4 має апаратний H.264 (bcm2835-codec), Pi 5 — ні.
ENCODER="${ENCODER:-auto}"
if [ "${ENCODER}" = auto ]; then
  if grep -qs bcm2835-codec-encode /sys/class/video4linux/*/name; then
    ENCODER=hardware
  else
    ENCODER=software
  fi
fi

# Форматний хінт: MJPG якщо камера підтримує (усі USB UVC — так), інакше YUYV.
INPUT_FORMAT=mjpeg
if ! v4l2-ctl -d "$VIDEO_DEV" --list-formats 2>/dev/null | grep -q "'MJPG'"; then
  INPUT_FORMAT=yuyv422
fi

FRAG_FLAGS="+frag_keyframe+empty_moov+default_base_moof"

if [ "${ENCODER}" = hardware ]; then
  CODEC=(-c:v h264_v4l2m2m -b:v "${BITRATE}" -pix_fmt yuv420p)
else
  CODEC=(-c:v libx264 -preset ultrafast -b:v "${BITRATE}" -pix_fmt yuv420p)
fi

FF=(ffmpeg -hide_banner -loglevel warning -nostdin -flush_packets 1
    -f v4l2 -input_format "${INPUT_FORMAT}"
    -video_size "${WIDTH}x${HEIGHT}" -framerate "${FPS}"
    -i "${VIDEO_DEV}"
    "${CODEC[@]}")

if [ "${SEGMENT_SEC}" -gt 0 ]; then
  FF+=(-f segment -segment_time "${SEGMENT_SEC}" -segment_format mp4
    -segment_format_options "movflags=${FRAG_FLAGS}"
    -reset_timestamps 1 -strftime 1 "${REC_DIR}/rec_%Y%m%d_%H%M%S.mp4")
else
  FF+=(-movflags "${FRAG_FLAGS}" -f mp4
    "${REC_DIR}/rec_$(date +%Y%m%d_%H%M%S).mp4")
fi

find "${REC_DIR}" -maxdepth 1 -type f -name 'rec_*.mp4' -size 0c -delete 2>/dev/null || true

rotate_loop &
ROT_PID=$!

if [ "${SYNC_INTERVAL_SEC}" -gt 0 ]; then
  sync_loop &
  SYNC_PID=$!
else
  SYNC_PID=
fi

echo "recorder started: ${WIDTH}x${HEIGHT}@${FPS}, encoder=${ENCODER}, input=${INPUT_FORMAT}, segment=${SEGMENT_SEC}s, dst=${REC_DIR}"

"${FF[@]}" &
FF_PID=$!

shutdown() {
  kill -INT "${FF_PID}" 2>/dev/null || true
  wait "${FF_PID}" 2>/dev/null || true
  kill "${ROT_PID}" 2>/dev/null || true
  [ -n "${SYNC_PID}" ] && kill "${SYNC_PID}" 2>/dev/null || true
  sync
  find "${REC_DIR}" -maxdepth 1 -type f -name 'rec_*.mp4' -size 0c -delete 2>/dev/null || true
  exit 0
}
trap shutdown TERM INT

wait "${FF_PID}" || true
shutdown
