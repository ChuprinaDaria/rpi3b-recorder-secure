#!/usr/bin/env bash
set -euo pipefail

REC_DIR="${REC_DIR:-${HOME}/recordings}"
WIDTH="${WIDTH:-1920}"
HEIGHT="${HEIGHT:-1080}"
FPS="${FPS:-30}"
BITRATE="${BITRATE:-10000000}"
AUTOFOCUS_MODE="${AUTOFOCUS_MODE:-manual}"
LENS_POSITION="${LENS_POSITION:-0}"
SEGMENT_SEC="${SEGMENT_SEC:-0}"
MAX_FILES="${MAX_FILES:-50}"
FREE_MB_MIN="${FREE_MB_MIN:-500}"
# Як часто sync поки йде запис. Без цього ffmpeg-пакети сидять у page cache
# і при power-cut ext4 з delayed alloc лишає файл 0 байт. 0 = вимкнути.
SYNC_INTERVAL_SEC="${SYNC_INTERVAL_SEC:-3}"

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

# Періодичний sync — обмежує втрату відео при power-cut до SYNC_INTERVAL_SEC.
sync_loop() {
  while true; do
    sleep "${SYNC_INTERVAL_SEC}"
    sync
  done
}

# Сегмент ріжеться тільки по keyframe, тому GOP має дорівнювати довжині сегмента.
if [ "${SEGMENT_SEC}" -gt 0 ]; then
  INTRA=$((FPS * SEGMENT_SEC))
else
  INTRA="${FPS}"
fi

# Autofocus-контроли валідні лише для сенсорів з моторним фокусом (imx708).
# На IMX219/IMX477/тощо rpicam-vid або впаде, або тихо не почне писати кадри
# і ffmpeg лишить 0-байтний mp4. Тому детектимо сенсор і кладемо AF-флаги
# тільки коли їх реально можна юзати. Оверрайд: HAS_AUTOFOCUS=1 або =0.
if [ -z "${HAS_AUTOFOCUS:-}" ]; then
  SENSOR="$(rpicam-vid --list-cameras 2>&1 | grep -oE '^[[:space:]]*[0-9]+[[:space:]]*:[[:space:]]*[^[:space:]]+' | head -n1 | awk '{print $NF}')"
  case "${SENSOR}" in
    imx708) HAS_AUTOFOCUS=1 ;;
    *)      HAS_AUTOFOCUS=0 ;;
  esac
  echo "detected sensor='${SENSOR:-unknown}' autofocus=${HAS_AUTOFOCUS}"
fi

CAM=(rpicam-vid -t 0 -n
  --width "${WIDTH}" --height "${HEIGHT}" --framerate "${FPS}")

if [ "${HAS_AUTOFOCUS}" = "1" ]; then
  CAM+=(--autofocus-mode "${AUTOFOCUS_MODE}")
  # lens-position має сенс лише в manual: 0 = безкінечність.
  if [ "${AUTOFOCUS_MODE}" = "manual" ]; then
    CAM+=(--lens-position "${LENS_POSITION}")
  fi
fi

# Pi 4 має апаратний H.264 (bcm2835-codec), Pi 5 — ні, там кодує CPU.
# ENCODER=software треба примусово для роздільностей ширших за 1920.
ENCODER="${ENCODER:-auto}"
if [ "${ENCODER}" = auto ]; then
  if grep -qs bcm2835-codec-encode /sys/class/video4linux/*/name; then
    ENCODER=hardware
  else
    ENCODER=software
  fi
fi

if [ "${ENCODER}" = hardware ]; then
  CAM+=(--bitrate "${BITRATE}" --codec h264 --inline --intra "${INTRA}")
else
  # Софтверний шлях: камера віддає сирий YUV, кодує ffmpeg. Через libav
  # всередині rpicam-vid не йдемо — його elementary stream сегментний
  # муксер не ріже (кейфрейми є, але поділ не відбувається).
  CAM+=(--codec yuv420)
fi
CAM+=(-o -)

# Робить mp4 «живучим» до крешу: якщо живлення вирубають без SIGTERM,
# файл лишається програвабельним — moov не потрібен.
FRAG_FLAGS="+frag_keyframe+empty_moov+default_base_moof"

WORK="$(mktemp -d)"
FIFO="${WORK}/h264"
mkfifo "${FIFO}"

FF=(ffmpeg -hide_banner -loglevel warning -nostdin -flush_packets 1)

if [ "${ENCODER}" = hardware ]; then
  FF+=(-fflags +genpts -r "${FPS}" -f h264 -i "${FIFO}" -c copy)
else
  FF+=(-f rawvideo -pix_fmt yuv420p -s "${WIDTH}x${HEIGHT}" -r "${FPS}" -i "${FIFO}"
    -c:v libx264 -preset ultrafast -b:v "${BITRATE}" -pix_fmt yuv420p)
  if [ "${SEGMENT_SEC}" -gt 0 ]; then
    FF+=(-flags +cgop -g "${INTRA}" -keyint_min "${INTRA}"
      -force_key_frames "expr:gte(t,n_forced*${SEGMENT_SEC})")
  fi
fi

if [ "${SEGMENT_SEC}" -gt 0 ]; then
  FF+=(-f segment -segment_time "${SEGMENT_SEC}" -segment_format mp4
    -segment_format_options "movflags=${FRAG_FLAGS}"
    -reset_timestamps 1 -strftime 1 "${REC_DIR}/rec_%Y%m%d_%H%M%S.mp4")
else
  FF+=(-movflags "${FRAG_FLAGS}" -f mp4
    "${REC_DIR}/rec_$(date +%Y%m%d_%H%M%S).mp4")
fi

# Прибрати 0-байтні mp4, що лишились від попереднього power-cut (ffmpeg відкрив
# файл, але жоден фрагмент не долетів до диска через ext4 delayed alloc).
find "${REC_DIR}" -maxdepth 1 -type f -name 'rec_*.mp4' -size 0c -delete 2>/dev/null || true

rotate_loop &
ROT_PID=$!

if [ "${SYNC_INTERVAL_SEC}" -gt 0 ]; then
  sync_loop &
  SYNC_PID=$!
else
  SYNC_PID=
fi

echo "recorder started: ${WIDTH}x${HEIGHT}@${FPS}, encoder=${ENCODER}, af=${AUTOFOCUS_MODE}/${LENS_POSITION}, segment=${SEGMENT_SEC}s, dir=${REC_DIR}"

"${FF[@]}" &
FF_PID=$!
"${CAM[@]}" >"${FIFO}" &
CAM_PID=$!

# Камеру валимо першою: ffmpeg бачить EOF і коректно закриває mp4.
# На старті чистимо 0-байтні mp4 (rpicam-vid впав → ffmpeg відкрив і закрив пустим).
shutdown() {
  kill "${CAM_PID}" 2>/dev/null || true
  wait "${FF_PID}" 2>/dev/null || true
  kill "${ROT_PID}" 2>/dev/null || true
  [ -n "${SYNC_PID}" ] && kill "${SYNC_PID}" 2>/dev/null || true
  # Фінальний sync — щоб moov, дописаний ffmpeg після EOF, точно ліг на диск.
  sync
  find "${REC_DIR}" -maxdepth 1 -type f -name 'rec_*.mp4' -size 0c -delete 2>/dev/null || true
  rm -rf "${WORK}"
  exit 0
}
trap shutdown TERM INT

# Sanity check: якщо rpicam-vid здох за пів секунди — далі теж 0-байтний файл буде.
sleep 0.5
if ! kill -0 "${CAM_PID}" 2>/dev/null; then
  echo "rpicam-vid died at start — aborting" >&2
  shutdown
fi

# Якщо камера впала — ffmpeg бачить EOF і виходить, сюди ж і приходимо.
wait "${FF_PID}" || true
shutdown
