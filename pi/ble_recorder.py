import logging
import os
import pathlib
import re
import shutil
import signal
import subprocess
import threading
import time

from bluezero import adapter, peripheral

SERVICE_UUID = "12345678-1234-5678-1234-56789abcdef0"
CHAR_UUID = "12345678-1234-5678-1234-56789abcdef1"
STATUS_UUID = "12345678-1234-5678-1234-56789abcdef2"
SNAPSHOT_UUID = "12345678-1234-5678-1234-56789abcdef3"

REC_DIR = os.environ.get("REC_DIR", os.path.expanduser("~/recordings"))
DEVICE_NAME = os.environ.get("DEVICE_NAME", "RPi5-CAM")
# manual + lens_position 0 = фіксований фокус на безкінечність.
AUTOFOCUS_MODE = os.environ.get("AUTOFOCUS_MODE", "manual")
LENS_POSITION = os.environ.get("LENS_POSITION", "0")
# Довжина одного mp4-чанку в секундах. 0 = не різати, писати одним файлом.
SEGMENT_SEC = int(os.environ.get("SEGMENT_SEC", 0))
MAX_FILES = int(os.environ.get("MAX_FILES", 50))

# Пресети, які веб-UI обирає другим байтом start-команди.
# Порядок мусить збігатися з <select> у index.html.
# (width, height, fps, bitrate, encoder_hint)
PRESETS = [
    (1920, 1080, 30, 10_000_000, "hardware"),  # 0: 1080p30 HW (дефолт)
    (1280, 720,  30,  6_000_000, "hardware"),  # 1: 720p30 HW (легкий)
    (2304, 1296, 30, 15_000_000, "software"),  # 2: 1296p30 SW (треба охолодження)
]

# Env-defaults для сумісності: якщо start-команда прийшла без байта пресета
# (старий клієнт), беремо дефолти з env або перший пресет.
DEFAULT_WIDTH = int(os.environ.get("WIDTH", PRESETS[0][0]))
DEFAULT_HEIGHT = int(os.environ.get("HEIGHT", PRESETS[0][1]))
DEFAULT_FPS = int(os.environ.get("FPS", PRESETS[0][2]))
DEFAULT_BITRATE = int(os.environ.get("BITRATE", PRESETS[0][3]))
DEFAULT_ENCODER = os.environ.get("ENCODER", "auto")

# Знімок для перевірки кадрування ДО запису. rpicam-jpeg бере камеру ексклюзивно —
# тому дозволено тільки коли rpicam-vid не працює.
SNAPSHOT_WIDTH = int(os.environ.get("SNAPSHOT_WIDTH", 480))
SNAPSHOT_HEIGHT = int(os.environ.get("SNAPSHOT_HEIGHT", 320))
SNAPSHOT_QUALITY = int(os.environ.get("SNAPSHOT_QUALITY", 75))
SNAPSHOT_PATH = "/tmp/rpi5cam_snap.jpg"
# 4-байтний header (seq_u16le + total_u16le) + payload. 180 залишає запас під
# найбільш поширений ATT_MTU=185 без ATT Read Blob.
SNAP_CHUNK_PAYLOAD = 180

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("ble_recorder")

os.makedirs(REC_DIR, exist_ok=True)

# Сенсори з моторним автофокусом. Для решти передавати --autofocus-mode /
# --lens-position не можна — rpicam-vid або вилетить, або тихо не почне писати
# кадри (=> 0-байтний mp4 на виході).
AF_CAPABLE_SENSORS = {"imx708"}


def _detect_camera():
    """Читає перший рядок з `rpicam-vid --list-cameras`.

    Формат: '0 : imx708 [4608x2592 10-bit RGGB] (/base/...)'
    Повертає dict або None, якщо нічого не знайшли.
    """
    try:
        r = subprocess.run(
            ["rpicam-vid", "--list-cameras"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log.warning("camera list failed: %s", e)
        return None
    text = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"^\s*(\d+)\s*:\s*(\S+)\s*\[(\d+)x(\d+)", text, re.M)
    if not m:
        log.warning("could not parse --list-cameras output:\n%s", text)
        return None
    sensor = m.group(2).lower()
    return {
        "index": int(m.group(1)),
        "sensor": sensor,
        "max_width": int(m.group(3)),
        "max_height": int(m.group(4)),
        "has_autofocus": sensor in AF_CAPABLE_SENSORS,
    }


CAMERA = _detect_camera()
if CAMERA:
    log.info(
        "camera: %s max=%dx%d autofocus=%s",
        CAMERA["sensor"], CAMERA["max_width"], CAMERA["max_height"],
        CAMERA["has_autofocus"],
    )
else:
    log.warning("no CSI camera detected — рекордер стартує, але запис впаде")

state = {"recording": False, "cam": None, "ff": None, "stop_event": None, "rot_thread": None}
lock = threading.Lock()

snapshot_state = {"chunks": [], "idx": 0, "lock": threading.Lock()}


def rotate_old_files():
    files = sorted(
        (f for f in os.listdir(REC_DIR) if f.endswith(".mp4")),
        reverse=True,
    )
    for old in files[MAX_FILES:]:
        try:
            os.remove(os.path.join(REC_DIR, old))
            log.info("rotated %s", old)
        except OSError as e:
            log.warning("rotate failed for %s: %s", old, e)


# Робить mp4 «живучим» до крешу: якщо процес вб'ють / вирубають живлення без SIGTERM,
# файл лишається програвабельним — moov не потрібен, дані самоописні по фрагментах.
FRAG_FLAGS = "+frag_keyframe+empty_moov+default_base_moof"


def _use_hw_encoder(encoder_hint):
    """Pi 4 має апаратний H.264 (bcm2835-codec), Pi 5 — ні, там кодує CPU.

    encoder_hint=software треба примусово для роздільностей ширших за 1920.
    """
    if encoder_hint != "auto":
        return encoder_hint == "hardware"
    return any(
        p.read_text().strip() == "bcm2835-codec-encode"
        for p in pathlib.Path("/sys/class/video4linux").glob("*/name")
    )


def _resolve_preset(preset_id):
    if preset_id is None:
        return DEFAULT_WIDTH, DEFAULT_HEIGHT, DEFAULT_FPS, DEFAULT_BITRATE, DEFAULT_ENCODER
    if 0 <= preset_id < len(PRESETS):
        return PRESETS[preset_id]
    log.warning("unknown preset id %d, falling back to 0", preset_id)
    return PRESETS[0]


def _start_pipeline(width, height, fps, bitrate, encoder_hint):
    """rpicam-vid → ffmpeg → mp4. Повертає (cam, ff, out_path) або (None, None, None)."""
    # Сегмент ріжеться тільки по keyframe, тому GOP = довжині сегмента.
    intra = fps * SEGMENT_SEC if SEGMENT_SEC > 0 else fps
    cam_cmd = [
        "rpicam-vid", "-t", "0", "-n",
        "--width", str(width), "--height", str(height),
        "--framerate", str(fps),
    ]
    # AF-контроли валідні лише для сенсорів з моторним фокусом (Camera Module 3).
    # На IMX219/IMX477/тощо rpicam-vid зʼїсть їх мовчки або впаде — і в другому
    # випадку ffmpeg лишає 0-байтний mp4.
    if CAMERA is None or CAMERA["has_autofocus"]:
        cam_cmd += ["--autofocus-mode", AUTOFOCUS_MODE]
        if AUTOFOCUS_MODE == "manual":
            cam_cmd += ["--lens-position", LENS_POSITION]

    ff_cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin"]
    hardware = _use_hw_encoder(encoder_hint)
    if hardware:
        cam_cmd += [
            "--bitrate", str(bitrate),
            "--codec", "h264", "--inline", "--intra", str(intra),
        ]
        ff_cmd += [
            "-fflags", "+genpts", "-r", str(fps), "-f", "h264", "-i", "-",
            "-c", "copy",
        ]
    else:
        # Софтверний шлях: камера віддає сирий YUV, кодує ffmpeg. Через libav
        # всередині rpicam-vid не йдемо — його elementary stream сегментний
        # муксер не ріже (кейфрейми є, але поділ не відбувається).
        cam_cmd += ["--codec", "yuv420"]
        ff_cmd += [
            "-f", "rawvideo", "-pix_fmt", "yuv420p",
            "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-b:v", str(bitrate), "-pix_fmt", "yuv420p",
        ]
        if SEGMENT_SEC > 0:
            ff_cmd += [
                "-flags", "+cgop", "-g", str(intra), "-keyint_min", str(intra),
                "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_SEC})",
            ]
    cam_cmd += ["-o", "-"]

    out_path = None
    if SEGMENT_SEC > 0:
        ff_cmd += [
            "-f", "segment", "-segment_time", str(SEGMENT_SEC),
            "-segment_format", "mp4",
            "-segment_format_options", f"movflags={FRAG_FLAGS}",
            "-reset_timestamps", "1", "-strftime", "1",
            os.path.join(REC_DIR, "rec_%Y%m%d_%H%M%S.mp4"),
        ]
    else:
        out_path = os.path.join(REC_DIR, time.strftime("rec_%Y%m%d_%H%M%S.mp4"))
        ff_cmd += ["-movflags", FRAG_FLAGS, "-f", "mp4", out_path]

    cam = subprocess.Popen(cam_cmd, stdout=subprocess.PIPE)
    ff = subprocess.Popen(ff_cmd, stdin=cam.stdout)
    cam.stdout.close()

    # Якщо rpicam-vid здох на старті (несумісні флаги, відсутній сенсор) —
    # ffmpeg отримає EOF і закриє файл 0-байтовим. Ловимо це тут.
    time.sleep(0.5)
    if cam.poll() is not None:
        log.error("rpicam-vid exited at start with code %d", cam.returncode)
        try:
            ff.wait(timeout=3)
        except subprocess.TimeoutExpired:
            ff.terminate()
            try:
                ff.wait(timeout=2)
            except subprocess.TimeoutExpired:
                ff.kill()
        if out_path and os.path.exists(out_path):
            try:
                if os.path.getsize(out_path) == 0:
                    os.remove(out_path)
                    log.info("removed empty %s", os.path.basename(out_path))
            except OSError:
                pass
        return None, None, None

    log.info(
        "pipeline start %dx%d@%d encoder=%s af=%s sensor=%s",
        width, height, fps, "hardware" if hardware else "software",
        AUTOFOCUS_MODE if (CAMERA is None or CAMERA["has_autofocus"]) else "n/a",
        CAMERA["sensor"] if CAMERA else "unknown",
    )
    return cam, ff, out_path


def _rotator_loop(stop_event):
    while not stop_event.wait(30):
        rotate_old_files()


def start_recording(preset_id=None):
    with lock:
        if state["recording"]:
            return False
        w, h, fps, br, enc = _resolve_preset(preset_id)
        rotate_old_files()
        cam, ff, out_path = _start_pipeline(w, h, fps, br, enc)
        if cam is None:
            log.warning("REC start failed preset=%s", preset_id)
            return False
        stop_event = threading.Event()
        rot_t = threading.Thread(target=_rotator_loop, args=(stop_event,), daemon=True)
        rot_t.start()
        state.update(recording=True, cam=cam, ff=ff, stop_event=stop_event, rot_thread=rot_t)
        log.info("REC start preset=%s segments=%ds", preset_id, SEGMENT_SEC)
        return True


def stop_recording():
    with lock:
        if not state["recording"]:
            return False
        cam = state["cam"]
        ff = state["ff"]
        stop_event = state["stop_event"]
    stop_event.set()
    # Валимо тільки камеру: ffmpeg бачить EOF, дописує moov і виходить сам.
    if cam:
        cam.send_signal(signal.SIGTERM)
        try:
            cam.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cam.kill()
            cam.wait(timeout=5)
    if ff:
        try:
            ff.wait(timeout=15)
        except subprocess.TimeoutExpired:
            log.warning("ffmpeg hung after EOF, killing")
            ff.kill()
            ff.wait(timeout=5)
    with lock:
        state.update(recording=False, cam=None, ff=None, stop_event=None, rot_thread=None)
    log.info("REC stop")
    return True


def _capture_snapshot():
    cmd = [
        "rpicam-jpeg", "-n", "-t", "500",
        "--width", str(SNAPSHOT_WIDTH), "--height", str(SNAPSHOT_HEIGHT),
        "--quality", str(SNAPSHOT_QUALITY),
    ]
    if CAMERA is None or CAMERA["has_autofocus"]:
        cmd += ["--autofocus-mode", AUTOFOCUS_MODE]
        if AUTOFOCUS_MODE == "manual":
            cmd += ["--lens-position", LENS_POSITION]
    cmd += ["-o", SNAPSHOT_PATH]
    subprocess.run(cmd, check=True, timeout=10, stderr=subprocess.PIPE)


def _snapshot_flow():
    if state["recording"]:
        log.warning("snapshot rejected: recording in progress")
        return
    # Клієнт починає пулити зразу після write 0x02 — треба показати «not ready»,
    # поки rpicam-jpeg не закінчить. Тому спочатку чистимо стан.
    with snapshot_state["lock"]:
        snapshot_state["chunks"] = []
        snapshot_state["idx"] = 0
    try:
        _capture_snapshot()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.warning("rpicam-jpeg failed: %s", e)
        return
    try:
        with open(SNAPSHOT_PATH, "rb") as f:
            jpeg = f.read()
    except OSError as e:
        log.warning("snapshot read failed: %s", e)
        return
    ts_ms = int(time.time() * 1000)
    # Перші 8 байт payload (тобто перші 8 після заголовка чанка 0) = Unix ms LE.
    payload = ts_ms.to_bytes(8, "little") + jpeg
    total = (len(payload) + SNAP_CHUNK_PAYLOAD - 1) // SNAP_CHUNK_PAYLOAD
    if total > 0xFFFF:
        log.warning("snapshot too large (%d chunks)", total)
        return
    chunks = []
    for i in range(total):
        body = payload[i * SNAP_CHUNK_PAYLOAD : (i + 1) * SNAP_CHUNK_PAYLOAD]
        header = i.to_bytes(2, "little") + total.to_bytes(2, "little")
        chunks.append(header + body)
    with snapshot_state["lock"]:
        snapshot_state["chunks"] = chunks
        snapshot_state["idx"] = 0
    log.info("snapshot ready: jpeg=%d B, chunks=%d", len(jpeg), total)


def read_snapshot(options):
    # Chrome при малому MTU робить ATT Read Blob із offset. Просуваємо idx лише
    # на першому сегменті (offset=0), інакше зʼїмо чанк раніше часу.
    offset = 0
    try:
        if options and hasattr(options, "get"):
            offset = int(options.get("offset", 0) or 0)
    except Exception:
        offset = 0
    with snapshot_state["lock"]:
        chunks = snapshot_state["chunks"]
        idx = snapshot_state["idx"]
        if idx >= len(chunks):
            # [seq=0, total=0] = сигнал «нема нового кадру, ще не готовий».
            return list(bytes(4))
        data = chunks[idx]
        if offset == 0:
            snapshot_state["idx"] = idx + 1
    return list(data[offset:])


def on_write(value, options):
    if not value:
        return
    cmd = value[0]
    if cmd == 0x01:
        # Другий байт — індекс пресета (див. PRESETS). Опційний для сумісності.
        preset_id = value[1] if len(value) > 1 else None
        start_recording(preset_id)
    elif cmd == 0x00:
        stop_recording()
    elif cmd == 0x02:
        threading.Thread(target=_snapshot_flow, daemon=True).start()
    else:
        log.warning("unknown cmd byte: 0x%02x", cmd)


def read_status(options):
    return [0x01 if state["recording"] else 0x00]


def main():
    for tool in ("ffmpeg", "rpicam-vid", "rpicam-jpeg"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} not found in PATH")

    adapter_addr = list(adapter.Adapter.available())[0].address
    log.info("using adapter %s", adapter_addr)

    p = peripheral.Peripheral(adapter_addr, local_name=DEVICE_NAME)
    p.add_service(srv_id=1, uuid=SERVICE_UUID, primary=True)

    p.add_characteristic(
        srv_id=1, chr_id=1, uuid=CHAR_UUID,
        value=[], notifying=False,
        flags=["write", "write-without-response"],
        write_callback=on_write,
    )
    p.add_characteristic(
        srv_id=1, chr_id=2, uuid=STATUS_UUID,
        value=[0x00], notifying=False,
        flags=["read"],
        read_callback=read_status,
    )
    p.add_characteristic(
        srv_id=1, chr_id=3, uuid=SNAPSHOT_UUID,
        value=[0x00], notifying=False,
        flags=["read"],
        read_callback=read_snapshot,
    )

    log.info("advertising as '%s'", DEVICE_NAME)
    p.publish()


if __name__ == "__main__":
    main()
