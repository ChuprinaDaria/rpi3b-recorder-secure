import logging
import os
import shutil
import signal
import subprocess
import threading
import time

from bluezero import adapter, peripheral

SERVICE_UUID = "12345678-1234-5678-1234-56789abcdef0"
CHAR_UUID = "12345678-1234-5678-1234-56789abcdef1"
STATUS_UUID = "12345678-1234-5678-1234-56789abcdef2"

REC_DIR = "/home/pi/recordings"
DEVICE_NAME = "RPi5-CAM"
VIDEO_DEV = "/dev/video0"
INPUT_FORMAT = "mjpeg"
WIDTH, HEIGHT, FPS = 1920, 1080, 30
BITRATE = 10_000_000
# Довжина одного mp4-чанку в секундах. 0 = не різати, писати одним файлом.
SEGMENT_SEC = 0
MAX_FILES = 50

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("ble_recorder")

os.makedirs(REC_DIR, exist_ok=True)

state = {"recording": False, "proc": None, "stop_event": None, "rot_thread": None}
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


def _start_ffmpeg():
    base = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-f", "v4l2", "-input_format", INPUT_FORMAT,
        "-video_size", f"{WIDTH}x{HEIGHT}", "-framerate", str(FPS),
        "-i", VIDEO_DEV,
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-b:v", str(BITRATE), "-pix_fmt", "yuv420p",
    ]
    if SEGMENT_SEC and SEGMENT_SEC > 0:
        pattern = os.path.join(REC_DIR, "rec_%Y%m%d_%H%M%S.mp4")
        cmd = base + [
            "-f", "segment", "-segment_time", str(SEGMENT_SEC),
            "-segment_format", "mp4", "-reset_timestamps", "1", "-strftime", "1",
            pattern,
        ]
    else:
        # SEGMENT_SEC=0 → один mp4 на всю сесію; SIGTERM даст ffmpeg закрити moov.
        out = os.path.join(REC_DIR, time.strftime("rec_%Y%m%d_%H%M%S.mp4"))
        cmd = base + ["-f", "mp4", out]
    log.info("ffmpeg start")
    return subprocess.Popen(cmd)


def _rotator_loop(stop_event):
    while not stop_event.wait(30):
        rotate_old_files()


def start_recording():
    with lock:
        if state["recording"]:
            return False
        rotate_old_files()
        proc = _start_ffmpeg()
        stop_event = threading.Event()
        rot_t = threading.Thread(target=_rotator_loop, args=(stop_event,), daemon=True)
        rot_t.start()
        state.update(recording=True, proc=proc, stop_event=stop_event, rot_thread=rot_t)
        log.info("REC start (segments=%ds)", SEGMENT_SEC)
        return True


def stop_recording():
    with lock:
        if not state["recording"]:
            return False
        proc = state["proc"]
        stop_event = state["stop_event"]
    stop_event.set()
    if proc:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    with lock:
        state.update(recording=False, proc=None, stop_event=None, rot_thread=None)
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
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg not found in PATH")

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
