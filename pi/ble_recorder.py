"""BLE-керований відео-рекордер для Raspberry Pi 3B з USB UVC-камерою.

Форк bluebird-works/rpi5-recorder під:
  - Pi 3B armv7 (без CSI, USB webcam на /dev/video0)
  - Запис у LUKS-mount /mnt/rec (шифрування at-rest)
  - Нові BLE opcodes: 0x05 WIPE, 0x06 STATUS_EXT
    -> знищення контейнера з телефона через Web Bluetooth (LAN-only за
       архітектурою: Web Bluetooth API не проходить через WAN)
"""
import json
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
# Нова характеристика: розширений статус для UI (recording/mounted/destroyed/free).
STATUS_EXT_UUID = "12345678-1234-5678-1234-56789abcdef4"

OPSEC_DIR = os.environ.get("OPSEC_DIR", "/opt/opsec")
IMG = os.environ.get("IMG", os.path.join(OPSEC_DIR, "rec.img"))
LUKS_NAME = os.environ.get("LUKS_NAME", "rec")
MNT = os.environ.get("MNT", "/mnt/rec")
KEY_FILE = os.environ.get("KEY_FILE", os.path.join(OPSEC_DIR, "rec-pass.txt"))
DESTROYED_MARKER = os.environ.get("DESTROYED_MARKER", os.path.join(OPSEC_DIR, "destroyed"))
WIPE_SCRIPT = os.environ.get("WIPE_SCRIPT",
                             os.path.join(os.path.dirname(__file__), "wipe.sh"))

REC_DIR = os.environ.get("REC_DIR", MNT)
DEVICE_NAME = os.environ.get("DEVICE_NAME", "RPi3B-CAM")

VIDEO_DEV = os.environ.get("VIDEO_DEV", "/dev/video0")
SEGMENT_SEC = int(os.environ.get("SEGMENT_SEC", 0))
MAX_FILES = int(os.environ.get("MAX_FILES", 50))
SYNC_INTERVAL_SEC = int(os.environ.get("SYNC_INTERVAL_SEC", 3))

# Пресети під Pi 3B USB webcam. Тримаємось MJPG (usb-webcam його завжди має),
# і hw h264_v4l2m2m як енкодер — Pi 3B/4 має bcm2835-codec.
# (width, height, fps, bitrate, encoder_hint)
PRESETS = [
    (1280, 720, 30, 4_000_000, "hardware"),  # 0: 720p30 HW (дефолт, комфортно)
    (640,  480, 30, 2_000_000, "hardware"),  # 1: 480p30 HW (мінімум навантаження)
    (1920, 1080, 15, 6_000_000, "hardware"),  # 2: 1080p15 HW (детальніше, менший fps)
]

DEFAULT_WIDTH = int(os.environ.get("WIDTH", PRESETS[0][0]))
DEFAULT_HEIGHT = int(os.environ.get("HEIGHT", PRESETS[0][1]))
DEFAULT_FPS = int(os.environ.get("FPS", PRESETS[0][2]))
DEFAULT_BITRATE = int(os.environ.get("BITRATE", PRESETS[0][3]))
DEFAULT_ENCODER = os.environ.get("ENCODER", "auto")

SNAPSHOT_WIDTH = int(os.environ.get("SNAPSHOT_WIDTH", 480))
SNAPSHOT_HEIGHT = int(os.environ.get("SNAPSHOT_HEIGHT", 320))
SNAPSHOT_QUALITY = int(os.environ.get("SNAPSHOT_QUALITY", 4))  # ffmpeg -q:v (2..31, менше=краще)
SNAPSHOT_PATH = "/tmp/rpi3bcam_snap.jpg"
SNAP_CHUNK_PAYLOAD = 180

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("ble_recorder")

STATE_FILE = os.path.join(OPSEC_DIR, ".recording_state")


def _persist_state(active, preset_id=None):
    if active:
        tmp = STATE_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"active": True, "preset_id": preset_id}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, STATE_FILE)
        except OSError as e:
            log.warning("could not persist state: %s", e)
    else:
        try:
            os.remove(STATE_FILE)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("could not clear state: %s", e)


def _load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        log.warning("could not load state: %s", e)
        return None


def _detect_camera():
    """Читає capabilities /dev/video0 через v4l2-ctl.

    Повертає dict або None.
    """
    try:
        r = subprocess.run(
            ["v4l2-ctl", "-d", VIDEO_DEV, "--list-formats-ext"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log.warning("v4l2-ctl failed: %s", e)
        return None
    text = (r.stdout or "") + (r.stderr or "")
    # шукаємо максимальний size у MJPG (типово 1920x1080)
    formats = re.findall(r"'(MJPG|YUYV)'.*?Size: Discrete (\d+)x(\d+)",
                         text, re.S)
    if not formats:
        log.warning("no MJPG/YUYV formats on %s", VIDEO_DEV)
        return None
    mjpg_sizes = [(int(w), int(h)) for f, w, h in formats if f == "MJPG"]
    max_w, max_h = (max(mjpg_sizes) if mjpg_sizes
                    else max((int(w), int(h)) for _, w, h in formats))
    return {
        "device": VIDEO_DEV,
        "has_mjpg": bool(mjpg_sizes),
        "max_width": max_w,
        "max_height": max_h,
    }


CAMERA = _detect_camera()
if CAMERA:
    log.info("camera: %s max=%dx%d mjpg=%s",
             CAMERA["device"], CAMERA["max_width"], CAMERA["max_height"],
             CAMERA["has_mjpg"])
else:
    log.warning("no USB camera on %s — рекордер стартує, але запис впаде",
                VIDEO_DEV)

state = {"recording": False, "ff": None, "stop_event": None,
         "rot_thread": None, "sync_thread": None}
lock = threading.Lock()

snapshot_state = {"chunks": [], "idx": 0, "lock": threading.Lock()}


def _luks_status():
    """Returns 'destroyed' | 'unmounted' | 'mounted'."""
    if os.path.exists(DESTROYED_MARKER):
        return "destroyed"
    if os.path.ismount(MNT):
        return "mounted"
    return "unmounted"


def _free_mb():
    try:
        st = os.statvfs(MNT)
        return (st.f_bavail * st.f_frsize) // (1024 * 1024)
    except OSError:
        return 0


def _prune_empty():
    try:
        names = os.listdir(REC_DIR)
    except OSError as e:
        log.warning("prune scan failed: %s", e)
        return
    for name in names:
        if not name.endswith(".mp4"):
            continue
        p = os.path.join(REC_DIR, name)
        try:
            if os.path.getsize(p) == 0:
                os.remove(p)
                log.info("pruned empty %s", name)
        except OSError:
            pass


def rotate_old_files():
    _prune_empty()
    try:
        files = sorted(
            (f for f in os.listdir(REC_DIR) if f.endswith(".mp4")),
            reverse=True,
        )
    except OSError:
        return
    for old in files[MAX_FILES:]:
        try:
            os.remove(os.path.join(REC_DIR, old))
            log.info("rotated %s", old)
        except OSError as e:
            log.warning("rotate failed for %s: %s", old, e)


FRAG_FLAGS = "+frag_keyframe+empty_moov+default_base_moof"


def _use_hw_encoder(encoder_hint):
    """Pi 3B/4 має апаратний H.264 (bcm2835-codec), Pi 5 — ні."""
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
    """ffmpeg -f v4l2 → h264_v4l2m2m/libx264 → mp4 у $REC_DIR."""
    if not os.path.ismount(MNT):
        log.error("mountpoint %s не змонтований — запис не стартує", MNT)
        return None, None

    hardware = _use_hw_encoder(encoder_hint)
    codec = ["-c:v", "h264_v4l2m2m", "-b:v", str(bitrate),
             "-pix_fmt", "yuv420p"] if hardware else \
            ["-c:v", "libx264", "-preset", "ultrafast",
             "-b:v", str(bitrate), "-pix_fmt", "yuv420p"]

    out_path = None
    output_args = []
    if SEGMENT_SEC > 0:
        output_args = [
            "-f", "segment", "-segment_time", str(SEGMENT_SEC),
            "-segment_format", "mp4",
            "-segment_format_options", f"movflags={FRAG_FLAGS}",
            "-reset_timestamps", "1", "-strftime", "1",
            os.path.join(REC_DIR, "rec_%Y%m%d_%H%M%S.mp4"),
        ]
    else:
        out_path = os.path.join(REC_DIR, time.strftime("rec_%Y%m%d_%H%M%S.mp4"))
        output_args = ["-movflags", FRAG_FLAGS, "-f", "mp4", out_path]

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-flush_packets", "1",
        "-f", "v4l2",
        "-input_format", "mjpeg" if CAMERA and CAMERA["has_mjpg"] else "yuyv422",
        "-video_size", f"{width}x{height}",
        "-framerate", str(fps),
        "-i", VIDEO_DEV,
        *codec,
        *output_args,
    ]
    ff = subprocess.Popen(cmd)

    # Sanity check: якщо ffmpeg здох за пів секунди (нема камери, format not
    # supported) — не входимо в recording state.
    time.sleep(0.7)
    if ff.poll() is not None:
        log.error("ffmpeg exited at start rc=%d", ff.returncode)
        if out_path and os.path.exists(out_path):
            try:
                if os.path.getsize(out_path) == 0:
                    os.remove(out_path)
            except OSError:
                pass
        return None, None

    log.info("pipeline start %dx%d@%d encoder=%s device=%s dst=%s",
             width, height, fps, "hardware" if hardware else "software",
             VIDEO_DEV, REC_DIR)
    return ff, out_path


def _rotator_loop(stop_event):
    while not stop_event.wait(30):
        rotate_old_files()


def _sync_loop(stop_event):
    while not stop_event.wait(SYNC_INTERVAL_SEC):
        try:
            os.sync()
        except OSError as e:
            log.warning("sync failed: %s", e)


def start_recording(preset_id=None):
    with lock:
        if state["recording"]:
            return False
        if _luks_status() == "destroyed":
            log.warning("REC start rejected: контейнер знищений")
            return False
        w, h, fps, br, enc = _resolve_preset(preset_id)
        rotate_old_files()
        ff, out_path = _start_pipeline(w, h, fps, br, enc)
        if ff is None:
            log.warning("REC start failed preset=%s", preset_id)
            return False
        stop_event = threading.Event()
        rot_t = threading.Thread(target=_rotator_loop, args=(stop_event,), daemon=True)
        rot_t.start()
        sync_t = None
        if SYNC_INTERVAL_SEC > 0:
            sync_t = threading.Thread(target=_sync_loop, args=(stop_event,), daemon=True)
            sync_t.start()
        state.update(recording=True, ff=ff, stop_event=stop_event,
                     rot_thread=rot_t, sync_thread=sync_t)
        _persist_state(True, preset_id)
        log.info("REC start preset=%s segments=%ds sync=%ds",
                 preset_id, SEGMENT_SEC, SYNC_INTERVAL_SEC)
        return True


def stop_recording():
    with lock:
        if not state["recording"]:
            return False
        ff = state["ff"]
        stop_event = state["stop_event"]
    stop_event.set()
    if ff:
        # ffmpeg reads /dev/video0 напряму — SIGINT дає йому дописати moov.
        ff.send_signal(signal.SIGINT)
        try:
            ff.wait(timeout=15)
        except subprocess.TimeoutExpired:
            log.warning("ffmpeg hung after SIGINT, killing")
            ff.kill()
            ff.wait(timeout=5)
    try:
        os.sync()
    except OSError as e:
        log.warning("final sync failed: %s", e)
    with lock:
        state.update(recording=False, ff=None, stop_event=None,
                     rot_thread=None, sync_thread=None)
    _persist_state(False)
    log.info("REC stop")
    return True


def _capture_snapshot():
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-y",
        "-f", "v4l2",
        "-input_format", "mjpeg" if CAMERA and CAMERA["has_mjpg"] else "yuyv422",
        "-video_size", f"{SNAPSHOT_WIDTH}x{SNAPSHOT_HEIGHT}",
        "-i", VIDEO_DEV,
        "-frames:v", "1", "-q:v", str(SNAPSHOT_QUALITY),
        SNAPSHOT_PATH,
    ]
    subprocess.run(cmd, check=True, timeout=10, stderr=subprocess.PIPE)


def _snapshot_flow():
    if state["recording"]:
        log.warning("snapshot rejected: recording in progress")
        return
    if _luks_status() == "destroyed":
        log.warning("snapshot rejected: контейнер знищений")
        return
    with snapshot_state["lock"]:
        snapshot_state["chunks"] = []
        snapshot_state["idx"] = 0
    try:
        _capture_snapshot()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.warning("snapshot ffmpeg failed: %s", e)
        return
    try:
        with open(SNAPSHOT_PATH, "rb") as f:
            jpeg = f.read()
    except OSError as e:
        log.warning("snapshot read failed: %s", e)
        return
    ts_ms = int(time.time() * 1000)
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
            return list(bytes(4))
        data = chunks[idx]
        if offset == 0:
            snapshot_state["idx"] = idx + 1
    return list(data[offset:])


def _wipe_flow(supplied_pass_bytes):
    """Виконує знищення контейнера ПІСЛЯ перевірки пасфрази."""
    if _luks_status() == "destroyed":
        log.warning("wipe rejected: контейнер уже знищений")
        return False
    # Перевірка пасфрази через cryptsetup --test-passphrase.
    # Не намагаємось відкрити повторно якщо контейнер уже mounted — використовуємо
    # той факт, що luksErase не вимагає пасу, але ми не хочемо стирати без auth.
    try:
        r = subprocess.run(
            ["cryptsetup", "open", "--test-passphrase",
             "--key-file", "-", IMG],
            input=supplied_pass_bytes, capture_output=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log.error("test-passphrase failed: %s", e)
        return False
    if r.returncode != 0:
        log.warning("wipe rejected: невірна пасфраза")
        return False
    log.info("wipe: пасфраза підтверджена, стартую знищення")

    # stop recording (не тримаємо файли відкритими)
    stop_recording()

    try:
        r = subprocess.run(
            ["sudo", "-n", WIPE_SCRIPT],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log.error("wipe.sh call failed: %s", e)
        return False
    log.info("wipe.sh rc=%d\nstdout:%s\nstderr:%s",
             r.returncode, r.stdout, r.stderr)
    return r.returncode == 0


def on_write(value, options):
    if not value:
        return
    cmd = value[0]
    if cmd == 0x01:
        preset_id = value[1] if len(value) > 1 else None
        start_recording(preset_id)
    elif cmd == 0x00:
        stop_recording()
    elif cmd == 0x02:
        threading.Thread(target=_snapshot_flow, daemon=True).start()
    elif cmd == 0x05:
        # WIPE. payload[1:] = utf-8 пасфраза LUKS. Виконується у фоні
        # щоб BLE-стек не таймаутнув (luksErase + dd ~1с, але може бути й довше).
        pass_bytes = bytes(value[1:])
        if not pass_bytes:
            log.warning("wipe rejected: порожня пасфраза")
            return
        threading.Thread(target=_wipe_flow, args=(pass_bytes,), daemon=True).start()
    else:
        log.warning("unknown cmd byte: 0x%02x", cmd)


def read_status(options):
    return [0x01 if state["recording"] else 0x00]


def read_status_ext(options):
    """Розширений статус для UI, 6 байт:
      [0] rec (0/1)
      [1] luks_state: 0=unmounted, 1=mounted, 2=destroyed
      [2..5] free_mb u32 little-endian
    """
    st = _luks_status()
    st_byte = {"unmounted": 0, "mounted": 1, "destroyed": 2}[st]
    free = _free_mb() if st == "mounted" else 0
    return [
        0x01 if state["recording"] else 0x00,
        st_byte,
        (free >> 0) & 0xFF,
        (free >> 8) & 0xFF,
        (free >> 16) & 0xFF,
        (free >> 24) & 0xFF,
    ]


def main():
    for tool in ("ffmpeg", "v4l2-ctl", "cryptsetup"):
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
    p.add_characteristic(
        srv_id=1, chr_id=4, uuid=STATUS_EXT_UUID,
        value=[0x00] * 6, notifying=False,
        flags=["read"],
        read_callback=read_status_ext,
    )

    # resume після crash. Але тільки якщо LUKS змонтований.
    resumed = _load_state()
    if resumed and resumed.get("active"):
        if _luks_status() == "mounted":
            preset_id = resumed.get("preset_id")
            log.info("resuming recording after boot preset=%s", preset_id)
            if not start_recording(preset_id):
                log.warning("resume failed, clearing state")
                _persist_state(False)
        else:
            log.info("skip resume: LUKS не змонтований (state=%s)",
                     _luks_status())
            _persist_state(False)

    log.info("advertising as '%s'", DEVICE_NAME)
    p.publish()


if __name__ == "__main__":
    main()
