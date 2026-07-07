import datetime
import logging
import os
import threading

from bluezero import adapter, peripheral
from picamera2 import Picamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput

SERVICE_UUID = "12345678-1234-5678-1234-56789abcdef0"
CHAR_UUID = "12345678-1234-5678-1234-56789abcdef1"
STATUS_UUID = "12345678-1234-5678-1234-56789abcdef2"

REC_DIR = "/home/pi/recordings"
DEVICE_NAME = "RPi5-CAM"
WIDTH, HEIGHT, FPS = 1920, 1080, 30
BITRATE = 10_000_000
MAX_FILES = 50

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("ble_recorder")

os.makedirs(REC_DIR, exist_ok=True)

picam2 = Picamera2()
picam2.configure(picam2.create_video_configuration(
    main={"size": (WIDTH, HEIGHT)},
    controls={"FrameRate": FPS},
))

state = {"recording": False, "file": None}
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


def start_recording():
    with lock:
        if state["recording"]:
            return False
        rotate_old_files()
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(REC_DIR, f"rec_{ts}.mp4")
        picam2.start_recording(H264Encoder(bitrate=BITRATE), FfmpegOutput(path))
        state.update(recording=True, file=path)
        log.info("REC start -> %s", path)
        return True


def stop_recording():
    with lock:
        if not state["recording"]:
            return False
        picam2.stop_recording()
        log.info("REC stop  -> %s", state["file"])
        state.update(recording=False, file=None)
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
