"""LAN-only HTTP-сервер для Pi 3B secure recorder.

Заміняє BLE-транспорт з ble_recorder.py:
  - Всі команди через REST на 0.0.0.0:8080
  - Real-time логи через SSE (/api/logs) — journalctl -f у стрім
  - Статична сторінка / (index.html) — same-origin fetch, no mixed-content

Threat model: LAN-only. За замовч слухає всі інтерфейси, але install-скрипт
кладе nftables rule = accept тільки з 192.168.1.0/24 (LAN).
"""
import json
import logging
import os
import pathlib
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

OPSEC_DIR = os.environ.get("OPSEC_DIR", "/opt/opsec")
IMG = os.environ.get("IMG", os.path.join(OPSEC_DIR, "rec.img"))
LUKS_NAME = os.environ.get("LUKS_NAME", "rec")
MNT = os.environ.get("MNT", "/mnt/rec")
KEY_FILE = os.environ.get("KEY_FILE", os.path.join(OPSEC_DIR, "rec-pass.txt"))
DESTROYED_MARKER = os.environ.get("DESTROYED_MARKER",
                                  os.path.join(OPSEC_DIR, "destroyed"))
WIPE_SCRIPT = os.environ.get("WIPE_SCRIPT",
                             os.path.join(os.path.dirname(__file__), "wipe.sh"))
STATIC_DIR = os.environ.get("STATIC_DIR",
                            os.path.dirname(os.path.dirname(__file__)))
SERVICE_NAME = os.environ.get("SERVICE_NAME", "rpi3b-lan-recorder.service")

REC_DIR = os.environ.get("REC_DIR", MNT)
VIDEO_DEV = os.environ.get("VIDEO_DEV", "/dev/video0")
SEGMENT_SEC = int(os.environ.get("SEGMENT_SEC", 0))
MAX_FILES = int(os.environ.get("MAX_FILES", 50))
SYNC_INTERVAL_SEC = int(os.environ.get("SYNC_INTERVAL_SEC", 3))
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", 8080))

# Пресети під Pi 3B USB webcam.
PRESETS = [
    (1280, 720, 30, 4_000_000, "hardware"),
    (640,  480, 30, 2_000_000, "hardware"),
    (1920, 1080, 15, 6_000_000, "hardware"),
]
DEFAULT_ENCODER = os.environ.get("ENCODER", "auto")

SNAPSHOT_WIDTH = int(os.environ.get("SNAPSHOT_WIDTH", 640))
SNAPSHOT_HEIGHT = int(os.environ.get("SNAPSHOT_HEIGHT", 480))
SNAPSHOT_QUALITY = int(os.environ.get("SNAPSHOT_QUALITY", 4))
SNAPSHOT_PATH = "/tmp/rpi3bcam_snap.jpg"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("lan_server")

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


def _detect_camera():
    try:
        r = subprocess.run(["v4l2-ctl", "-d", VIDEO_DEV, "--list-formats-ext"],
                           capture_output=True, text=True, timeout=5)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    text = (r.stdout or "") + (r.stderr or "")
    formats = re.findall(r"'(MJPG|YUYV)'.*?Size: Discrete (\d+)x(\d+)", text, re.S)
    if not formats:
        return None
    mjpg_sizes = [(int(w), int(h)) for f, w, h in formats if f == "MJPG"]
    return {"has_mjpg": bool(mjpg_sizes)}


CAMERA = _detect_camera()
if CAMERA:
    log.info("camera %s mjpg=%s", VIDEO_DEV, CAMERA["has_mjpg"])
else:
    log.warning("no camera on %s", VIDEO_DEV)

state = {"recording": False, "ff": None, "stop_event": None,
         "rot_thread": None, "sync_thread": None}
lock = threading.Lock()


def _luks_status():
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
    except OSError:
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


def _rotate_old():
    _prune_empty()
    try:
        files = sorted((f for f in os.listdir(REC_DIR) if f.endswith(".mp4")),
                       reverse=True)
    except OSError:
        return
    for old in files[MAX_FILES:]:
        try:
            os.remove(os.path.join(REC_DIR, old))
            log.info("rotated %s", old)
        except OSError:
            pass


FRAG_FLAGS = "+frag_keyframe+empty_moov+default_base_moof"


def _use_hw(hint):
    if hint != "auto":
        return hint == "hardware"
    return any(p.read_text().strip() == "bcm2835-codec-encode"
               for p in Path("/sys/class/video4linux").glob("*/name"))


def _resolve_preset(preset_id):
    if preset_id is None or preset_id < 0 or preset_id >= len(PRESETS):
        return PRESETS[0]
    return PRESETS[preset_id]


def _start_pipeline(width, height, fps, bitrate, encoder_hint):
    if not os.path.ismount(MNT):
        log.error("mountpoint %s not mounted — REC not starting", MNT)
        return None, None
    hardware = _use_hw(encoder_hint)
    codec = (["-c:v", "h264_v4l2m2m", "-b:v", str(bitrate), "-pix_fmt", "yuv420p"]
             if hardware else
             ["-c:v", "libx264", "-preset", "ultrafast", "-b:v", str(bitrate),
              "-pix_fmt", "yuv420p"])
    out_path = os.path.join(REC_DIR, time.strftime("rec_%Y%m%d_%H%M%S.mp4"))
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-flush_packets", "1",
        "-f", "v4l2",
        "-input_format", "mjpeg" if CAMERA and CAMERA["has_mjpg"] else "yuyv422",
        "-video_size", f"{width}x{height}",
        "-framerate", str(fps),
        "-i", VIDEO_DEV,
        *codec,
        "-movflags", FRAG_FLAGS, "-f", "mp4", out_path,
    ]
    ff = subprocess.Popen(cmd)
    time.sleep(0.7)
    if ff.poll() is not None:
        log.error("ffmpeg exited at start rc=%d", ff.returncode)
        try:
            if os.path.getsize(out_path) == 0:
                os.remove(out_path)
        except OSError:
            pass
        return None, None
    log.info("pipeline start %dx%d@%d encoder=%s dst=%s",
             width, height, fps, "hardware" if hardware else "software", out_path)
    return ff, out_path


def _rotator_loop(stop_event):
    while not stop_event.wait(30):
        _rotate_old()


def _sync_loop(stop_event):
    while not stop_event.wait(SYNC_INTERVAL_SEC):
        try:
            os.sync()
        except OSError:
            pass


def start_recording(preset_id=None):
    with lock:
        if state["recording"]:
            return False, "already recording"
        if _luks_status() == "destroyed":
            return False, "контейнер знищений"
        if _luks_status() != "mounted":
            return False, "LUKS не змонтований"
        w, h, fps, br, enc = _resolve_preset(preset_id)
        _rotate_old()
        ff, out_path = _start_pipeline(w, h, fps, br, enc)
        if ff is None:
            return False, "ffmpeg не стартував"
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
        log.info("REC start preset=%s", preset_id)
        return True, os.path.basename(out_path)


def stop_recording():
    with lock:
        if not state["recording"]:
            return False, "not recording"
        ff = state["ff"]
        stop_event = state["stop_event"]
    stop_event.set()
    if ff:
        ff.send_signal(signal.SIGINT)
        try:
            ff.wait(timeout=15)
        except subprocess.TimeoutExpired:
            log.warning("ffmpeg hung, killing")
            ff.kill()
            ff.wait(timeout=5)
    try:
        os.sync()
    except OSError:
        pass
    with lock:
        state.update(recording=False, ff=None, stop_event=None,
                     rot_thread=None, sync_thread=None)
    _persist_state(False)
    log.info("REC stop")
    return True, "stopped"


def _capture_snapshot():
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "v4l2",
        "-input_format", "mjpeg" if CAMERA and CAMERA["has_mjpg"] else "yuyv422",
        "-video_size", f"{SNAPSHOT_WIDTH}x{SNAPSHOT_HEIGHT}",
        "-i", VIDEO_DEV,
        "-frames:v", "1", "-q:v", str(SNAPSHOT_QUALITY),
        SNAPSHOT_PATH,
    ]
    subprocess.run(cmd, check=True, timeout=10, stderr=subprocess.PIPE)


def _do_wipe(pass_bytes):
    if _luks_status() == "destroyed":
        return False, "already destroyed"
    try:
        r = subprocess.run(
            ["cryptsetup", "open", "--test-passphrase", "--key-file", "-", IMG],
            input=pass_bytes, capture_output=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return False, f"test-passphrase failed: {e}"
    if r.returncode != 0:
        return False, "невірна пасфраза"
    log.info("wipe: passphrase verified, starting destruction")
    stop_recording()
    try:
        r = subprocess.run(["sudo", "-n", WIPE_SCRIPT],
                           capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return False, f"wipe.sh failed: {e}"
    log.info("wipe.sh rc=%d stdout=%s stderr=%s",
             r.returncode, r.stdout.strip(), r.stderr.strip())
    return r.returncode == 0, r.stderr.strip() or "ok"


# ─────────── Flask ───────────
app = Flask(__name__, static_folder=None)


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/api/status")
def api_status():
    lst = _luks_status()
    return jsonify({
        "recording": state["recording"],
        "luks": lst,
        "free_mb": _free_mb() if lst == "mounted" else 0,
        "camera": bool(CAMERA),
        "video_dev": VIDEO_DEV,
        "mnt": MNT,
    })


@app.get("/api/snapshot.jpg")
def api_snapshot():
    if state["recording"]:
        return "recording in progress", 409
    if _luks_status() == "destroyed":
        return "destroyed", 410
    try:
        _capture_snapshot()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        return f"snapshot failed: {e}", 500
    try:
        with open(SNAPSHOT_PATH, "rb") as f:
            data = f.read()
    except OSError as e:
        return f"read failed: {e}", 500
    return Response(data, mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/start")
def api_start():
    body = request.get_json(silent=True) or {}
    preset_id = int(body.get("preset", 0))
    ok, msg = start_recording(preset_id)
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 400)


@app.post("/api/stop")
def api_stop():
    ok, msg = stop_recording()
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 400)


@app.post("/api/wipe")
def api_wipe():
    body = request.get_json(silent=True) or {}
    passphrase = body.get("passphrase", "")
    if not passphrase:
        return jsonify({"ok": False, "msg": "порожня пасфраза"}), 400
    ok, msg = _do_wipe(passphrase.encode("utf-8"))
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 400)


@app.get("/api/logs")
def api_logs():
    """SSE stream з journalctl -u <service> -f."""
    def gen():
        yield "retry: 3000\n\n"
        cmd = ["journalctl", "-u", SERVICE_NAME, "-f", "-n", "80",
               "--no-pager", "--output=short"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                # SSE: кожен рядок як окремий event
                yield f"data: {line}\n\n"
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                proc.kill()
    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers=headers)


def main():
    for tool in ("ffmpeg", "v4l2-ctl", "cryptsetup", "journalctl"):
        if not pathlib.Path(f"/usr/bin/{tool}").exists() and \
           not pathlib.Path(f"/usr/sbin/{tool}").exists() and \
           not pathlib.Path(f"/sbin/{tool}").exists():
            log.warning("missing tool: %s", tool)
    log.info("listening on %s:%d (STATIC=%s SERVICE=%s)",
             LISTEN_HOST, LISTEN_PORT, STATIC_DIR, SERVICE_NAME)
    # threaded=True — щоб SSE не блокував інші запити
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, threaded=True, debug=False)


if __name__ == "__main__":
    main()
