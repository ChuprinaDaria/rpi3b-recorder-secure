import logging
import os
import pathlib
import shutil
import signal
import subprocess
import threading
import time

from bluezero import adapter, peripheral

SERVICE_UUID = "12345678-1234-5678-1234-56789abcdef0"
CHAR_UUID = "12345678-1234-5678-1234-56789abcdef1"
STATUS_UUID = "12345678-1234-5678-1234-56789abcdef2"

REC_DIR = os.environ.get("REC_DIR", os.path.expanduser("~/recordings"))
DEVICE_NAME = os.environ.get("DEVICE_NAME", "RPi5-CAM")
WIDTH = int(os.environ.get("WIDTH", 1920))
HEIGHT = int(os.environ.get("HEIGHT", 1080))
FPS = int(os.environ.get("FPS", 30))
BITRATE = int(os.environ.get("BITRATE", 10_000_000))
# manual + lens_position 0 = фіксований фокус на безкінечність.
AUTOFOCUS_MODE = os.environ.get("AUTOFOCUS_MODE", "manual")
LENS_POSITION = os.environ.get("LENS_POSITION", "0")
# Довжина одного mp4-чанку в секундах. 0 = не різати, писати одним файлом.
ENCODER = os.environ.get("ENCODER", "auto")
SEGMENT_SEC = int(os.environ.get("SEGMENT_SEC", 0))
MAX_FILES = int(os.environ.get("MAX_FILES", 50))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("ble_recorder")

os.makedirs(REC_DIR, exist_ok=True)

state = {"recording": False, "cam": None, "ff": None, "stop_event": None, "rot_thread": None}
lock = threading.Lock()


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


def _use_hw_encoder():
    """Pi 4 має апаратний H.264 (bcm2835-codec), Pi 5 — ні, там кодує CPU.

    ENCODER=software треба примусово для роздільностей ширших за 1920.
    """
    if ENCODER != "auto":
        return ENCODER == "hardware"
    return any(
        p.read_text().strip() == "bcm2835-codec-encode"
        for p in pathlib.Path("/sys/class/video4linux").glob("*/name")
    )


def _start_pipeline():
    """rpicam-vid → ffmpeg → mp4."""
    # Сегмент ріжеться тільки по keyframe, тому GOP = довжині сегмента.
    intra = FPS * SEGMENT_SEC if SEGMENT_SEC > 0 else FPS
    cam_cmd = [
        "rpicam-vid", "-t", "0", "-n",
        "--width", str(WIDTH), "--height", str(HEIGHT),
        "--framerate", str(FPS),
        "--autofocus-mode", AUTOFOCUS_MODE,
    ]
    if AUTOFOCUS_MODE == "manual":
        cam_cmd += ["--lens-position", LENS_POSITION]

    ff_cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin"]
    hardware = _use_hw_encoder()
    if hardware:
        cam_cmd += [
            "--bitrate", str(BITRATE),
            "--codec", "h264", "--inline", "--intra", str(intra),
        ]
        ff_cmd += [
            "-fflags", "+genpts", "-r", str(FPS), "-f", "h264", "-i", "-",
            "-c", "copy",
        ]
    else:
        # Софтверний шлях: камера віддає сирий YUV, кодує ffmpeg. Через libav
        # всередині rpicam-vid не йдемо — його elementary stream сегментний
        # муксер не ріже (кейфрейми є, але поділ не відбувається).
        cam_cmd += ["--codec", "yuv420"]
        ff_cmd += [
            "-f", "rawvideo", "-pix_fmt", "yuv420p",
            "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "-",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-b:v", str(BITRATE), "-pix_fmt", "yuv420p",
        ]
        if SEGMENT_SEC > 0:
            ff_cmd += [
                "-flags", "+cgop", "-g", str(intra), "-keyint_min", str(intra),
                "-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_SEC})",
            ]
    cam_cmd += ["-o", "-"]

    if SEGMENT_SEC > 0:
        ff_cmd += [
            "-f", "segment", "-segment_time", str(SEGMENT_SEC),
            "-segment_format", "mp4",
            "-segment_format_options", f"movflags={FRAG_FLAGS}",
            "-reset_timestamps", "1", "-strftime", "1",
            os.path.join(REC_DIR, "rec_%Y%m%d_%H%M%S.mp4"),
        ]
    else:
        ff_cmd += [
            "-movflags", FRAG_FLAGS, "-f", "mp4",
            os.path.join(REC_DIR, time.strftime("rec_%Y%m%d_%H%M%S.mp4")),
        ]

    cam = subprocess.Popen(cam_cmd, stdout=subprocess.PIPE)
    ff = subprocess.Popen(ff_cmd, stdin=cam.stdout)
    cam.stdout.close()
    log.info(
        "pipeline start %dx%d@%d encoder=%s af=%s",
        WIDTH, HEIGHT, FPS, "hardware" if hardware else "software", AUTOFOCUS_MODE,
    )
    return cam, ff


def _rotator_loop(stop_event):
    while not stop_event.wait(30):
        rotate_old_files()


def start_recording():
    with lock:
        if state["recording"]:
            return False
        rotate_old_files()
        cam, ff = _start_pipeline()
        stop_event = threading.Event()
        rot_t = threading.Thread(target=_rotator_loop, args=(stop_event,), daemon=True)
        rot_t.start()
        state.update(recording=True, cam=cam, ff=ff, stop_event=stop_event, rot_thread=rot_t)
        log.info("REC start (segments=%ds)", SEGMENT_SEC)
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


def on_write(value, options):
    if not value:
        return
    cmd = value[0]
    if cmd == 0x01:
        start_recording()
    elif cmd == 0x00:
        stop_recording()
    else:
        log.warning("unknown cmd byte: 0x%02x", cmd)


def read_status(options):
    return [0x01 if state["recording"] else 0x00]


def main():
    for tool in ("ffmpeg", "rpicam-vid"):
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

    log.info("advertising as '%s'", DEVICE_NAME)
    p.publish()


if __name__ == "__main__":
    main()
