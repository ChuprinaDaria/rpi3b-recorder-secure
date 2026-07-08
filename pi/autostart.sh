#!/usr/bin/env bash
set -euo pipefail

REC_DIR="${REC_DIR:-/home/pi/recordings}"
VIDEO_DEV="${VIDEO_DEV:-/dev/video0}"
INPUT_FORMAT="${INPUT_FORMAT:-mjpeg}"
WIDTH="${WIDTH:-1920}"
HEIGHT="${HEIGHT:-1080}"
FPS="${FPS:-30}"
BITRATE="${BITRATE:-10000000}"
SEGMENT_SEC="${SEGMENT_SEC:-0}"
MAX_FILES="${MAX_FILES:-50}"
FREE_MB_MIN="${FREE_MB_MIN:-500}"

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

rotate_loop &
ROT_PID=$!
trap 'kill "${ROT_PID}" 2>/dev/null; exit 0' TERM INT

echo "recorder started, dir=${REC_DIR}, dev=${VIDEO_DEV}, segment=${SEGMENT_SEC}s"

if [ "${SEGMENT_SEC}" -gt 0 ]; then
  exec ffmpeg -hide_banner -loglevel warning -nostdin \
    -f v4l2 -input_format "${INPUT_FORMAT}" \
    -video_size "${WIDTH}x${HEIGHT}" -framerate "${FPS}" \
    -i "${VIDEO_DEV}" \
    -c:v libx264 -preset ultrafast -tune zerolatency \
    -b:v "${BITRATE}" -pix_fmt yuv420p \
    -f segment -segment_time "${SEGMENT_SEC}" \
    -segment_format mp4 -reset_timestamps 1 -strftime 1 \
    "${REC_DIR}/rec_%Y%m%d_%H%M%S.mp4"
else
  OUT="${REC_DIR}/rec_$(date +%Y%m%d_%H%M%S).mp4"
  exec ffmpeg -hide_banner -loglevel warning -nostdin \
    -f v4l2 -input_format "${INPUT_FORMAT}" \
    -video_size "${WIDTH}x${HEIGHT}" -framerate "${FPS}" \
    -i "${VIDEO_DEV}" \
    -c:v libx264 -preset ultrafast -tune zerolatency \
    -b:v "${BITRATE}" -pix_fmt yuv420p \
    -f mp4 "${OUT}"
fi
