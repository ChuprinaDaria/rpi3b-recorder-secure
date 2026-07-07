#!/usr/bin/env bash
set -euo pipefail

REC_DIR="${REC_DIR:-/home/pi/recordings}"
WIDTH="${WIDTH:-1920}"
HEIGHT="${HEIGHT:-1080}"
FPS="${FPS:-30}"
BITRATE="${BITRATE:-10000000}"
SEGMENT_SEC="${SEGMENT_SEC:-300}"
MAX_FILES="${MAX_FILES:-50}"
FREE_MB_MIN="${FREE_MB_MIN:-500}"

mkdir -p "${REC_DIR}"

rotate() {
  mapfile -t old < <(ls -1t "${REC_DIR}"/rec_*.mp4 2>/dev/null | tail -n +"$((MAX_FILES+1))")
  for f in "${old[@]:-}"; do
    [ -n "$f" ] && rm -f "$f" && echo "rotated $f"
  done
}

free_mb() {
  df -Pm "${REC_DIR}" | awk 'NR==2 {print $4}'
}

record_segment() {
  local ts path
  ts=$(date +%Y%m%d_%H%M%S)
  path="${REC_DIR}/rec_${ts}.mp4"
  echo "REC -> ${path}"
  rpicam-vid \
    --nopreview \
    --width  "${WIDTH}" \
    --height "${HEIGHT}" \
    --framerate "${FPS}" \
    --bitrate "${BITRATE}" \
    --codec libav \
    --libav-format mp4 \
    --timeout $((SEGMENT_SEC * 1000)) \
    --output "${path}"
}

trap 'echo "stopping..."; exit 0' TERM INT

echo "recorder started, dir=${REC_DIR}"
while true; do
  rotate
  if [ "$(free_mb)" -lt "${FREE_MB_MIN}" ]; then
    echo "low space (<${FREE_MB_MIN}MB), forcing extra rotation"
    ls -1t "${REC_DIR}"/rec_*.mp4 2>/dev/null | tail -n 5 | xargs -r rm -f
  fi
  record_segment || {
    echo "record_segment failed, sleeping 5s"
    sleep 5
  }
done
