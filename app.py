"""
Smart Room Digital Twin - backend

ESP32 contract is unchanged: GET http://192.168.4.1/data ->
    {"temperature":..,"humidity":..,"pir":0/1,"ldr":..,"gas":..}

Run:  python app.py      then open http://127.0.0.1:5000
"""
import json
import math
import os
import random
import socket
import threading
import time

import requests
from flask import Flask, jsonify, request, send_from_directory

import scan_job
from engine import World

try:
    import cv2
    import numpy as np
    import live_recon
    rec = live_recon.LiveRecon()
except ImportError:  # opencv missing: live mode is switched off, everything else still works
    live_recon = rec = None

# ============================================================
# CONFIGURATION
# ============================================================
ESP32_IP = "192.168.4.1"
ESP32_URL = f"http://{ESP32_IP}/data"
FLASK_PORT = 5000
USE_COLMAP_FOR_LIVE = False  # False = live panel uses its own fast measurement, never COLMAP
BASE = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=os.path.join(BASE, "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 3 * 1024 ** 3  # room videos can be large
world = World()

sensor_data = {
    "temperature": 0, "humidity": 0, "pir": 0, "ldr": 0, "gas": 0,
    "connected": False, "mode": "esp32", "last_update": "--",
}
data_lock = threading.Lock()
demo = {"gas_until": 0.0, "temp_until": 0.0, "pir_until": 0.0, "next_pir": time.time() + 4}


# ============================================================
# SENSOR COLLECTOR (ESP32 or demo)
# ============================================================
def read_esp32():
    try:
        r = requests.get(ESP32_URL, timeout=2)
        if r.status_code != 200:
            return None
        d = r.json()
        return {
            "temperature": float(d.get("temperature", 0)),
            "humidity": float(d.get("humidity", 0)),
            "pir": int(d.get("pir", 0)),
            "ldr": int(d.get("ldr", 0)),
            "gas": int(d.get("gas", 0)),
        }
    except Exception:
        return None


def read_demo():
    now = time.time()
    if now > demo["next_pir"]:
        demo["pir_until"] = now + random.uniform(4, 9)
        demo["next_pir"] = demo["pir_until"] + random.uniform(5, 14)
    return {
        "temperature": 27 + 1.2 * math.sin(now / 30) + (30 if now < demo["temp_until"] else 0),
        "humidity": 55 + 6 * math.sin(now / 45),
        "pir": 1 if now < demo["pir_until"] else 0,
        "ldr": int(2200 + 1500 * math.sin(now / 60)),
        "gas": int(350 + random.uniform(-40, 40) + (2900 if now < demo["gas_until"] else 0)),
    }


LOG_DIR = os.path.join(BASE, "logs")


def log_sensors(snap):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, "sensors.csv")
        new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as f:
            if new:
                f.write("time,mode,temperature,humidity,pir,ldr,gas\n")
            f.write("%s,%s,%.1f,%.1f,%d,%d,%d\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), snap["mode"], snap["temperature"],
                snap["humidity"], snap["pir"], snap["ldr"], snap["gas"]))
    except OSError:
        pass


def sensor_loop():
    while True:
        with data_lock:
            mode = sensor_data["mode"]
        reading = read_esp32() if mode == "esp32" else read_demo()
        with data_lock:
            if reading is not None:
                sensor_data.update(reading)
                sensor_data["connected"] = True
                sensor_data["last_update"] = time.strftime("%H:%M:%S")
            else:
                sensor_data["connected"] = False
            snap = dict(sensor_data)
        if snap["connected"]:
            log_sensors(snap)
        world.update_sensors(snap)
        time.sleep(2 if mode == "esp32" else 1)


def sim_loop():
    last = time.time()
    while True:
        now = time.time()
        world.advance(now - last)
        last = now
        time.sleep(0.2)


# ============================================================
# ROUTES
# ============================================================
@app.route("/")
def home():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/layout")
def api_layout():
    out = dict(world.layout)
    out["cols"], out["rows"] = world.grid.cols, world.grid.rows
    return jsonify(out)


@app.route("/api/layout", methods=["POST"])
def api_layout_save():
    b = body()
    world.set_layout(b)
    return jsonify(ok=True)


@app.route("/api/layout/default", methods=["POST"])
def api_layout_default():
    world.restore_default_layout()
    try:
        os.remove(os.path.join(app.static_folder, "models", "room_points.json"))
    except OSError:
        pass
    return jsonify(ok=True)


@app.route("/api/data")  # old endpoint, kept for compatibility
def api_data():
    with data_lock:
        return jsonify(sensor_data)


@app.route("/api/state")
def api_state():
    s = world.state()
    with data_lock:
        s["sensors"] = dict(sensor_data)
    return jsonify(s)


def body():
    return request.get_json(silent=True) or {}


@app.route("/api/disaster", methods=["POST"])
def api_disaster():
    b = body()
    ok = world.start_disaster(b.get("type"), b.get("x"), b.get("z"))
    return (jsonify(ok=True), 200) if ok else (jsonify(ok=False, error="unknown disaster type"), 400)


@app.route("/api/reset", methods=["POST"])
def api_reset():
    world.reset()
    return jsonify(ok=True)


@app.route("/api/speed", methods=["POST"])
def api_speed():
    world.speed = max(0.5, min(8.0, float(body().get("speed", 1))))
    return jsonify(ok=True)


@app.route("/api/occupants", methods=["POST"])
def api_occupants():
    world.set_occupants(body().get("n", 6))
    return jsonify(ok=True)


@app.route("/api/exit/<eid>", methods=["POST"])
def api_exit(eid):
    world.block_exit(eid, body().get("blocked", True))
    return jsonify(ok=True)


@app.route("/api/auto", methods=["POST"])
def api_auto():
    world.auto = bool(body().get("enabled", True))
    return jsonify(ok=True)


@app.route("/api/sensors/mode", methods=["POST"])
def api_sensor_mode():
    mode = "demo" if body().get("mode") == "demo" else "esp32"
    with data_lock:
        sensor_data["mode"] = mode
        sensor_data["connected"] = False
    return jsonify(ok=True)


@app.route("/api/sensors/spike", methods=["POST"])
def api_spike():
    kind = body().get("kind")
    if kind == "gas":
        demo["gas_until"] = time.time() + 12
    elif kind == "temp":
        demo["temp_until"] = time.time() + 12
    return jsonify(ok=True)


@app.route("/api/objects")
def api_objects():
    with world.lock:
        return jsonify(list(world.objects))


@app.route("/api/object/check", methods=["POST"])
def api_object_check():
    b = body()
    try:
        return jsonify(world.check_fit(float(b["x"]), float(b["z"]), float(b["w"]), float(b["d"]),
                                       float(b["h"]), b.get("exclude")))
    except (KeyError, TypeError, ValueError):
        return jsonify(error="bad object data"), 400


@app.route("/api/object", methods=["POST"])
def api_object_save():
    try:
        return jsonify(ok=True, id=world.save_object(body()))
    except (KeyError, TypeError, ValueError):
        return jsonify(ok=False, error="bad object data"), 400


@app.route("/api/object/<oid>", methods=["DELETE"])
def api_object_delete(oid):
    world.remove_object(oid)
    return jsonify(ok=True)


# ---------- 3D reconstruction from the web page ----------
def reload_scanned_layout():
    with open(os.path.join(BASE, "static", "models", "room_layout.json"), encoding="utf-8") as f:
        world.set_layout(json.load(f), persist=False)


@app.route("/api/scan/upload", methods=["POST"])
def api_scan_upload():
    if scan_job.busy():
        return jsonify(error="A scan is already running"), 409
    n = scan_job.save_uploads(request.files.getlist("files"))
    if not n:
        return jsonify(error="No video or photo files found (use mp4, mov, webm, jpg, png)"), 400
    return jsonify(ok=True, files=n)


@app.route("/api/scan/start", methods=["POST"])
def api_scan_start():
    b = body()
    try:
        width = float(b.get("room_width"))
        depth = float(b["room_depth"]) if b.get("room_depth") else None
    except (TypeError, ValueError):
        return jsonify(error="Enter the room's longest wall in metres"), 400
    if width <= 0:
        return jsonify(error="Enter the room's longest wall in metres"), 400
    if not scan_job.start(width, depth, (b.get("colmap") or "").strip().strip('"'), reload_scanned_layout):
        return jsonify(error="A scan is already running"), 409
    return jsonify(ok=True)


@app.route("/api/scan/status")
def api_scan_status():
    return jsonify(scan_job.status())


@app.route("/api/scan/cancel", methods=["POST"])
def api_scan_cancel():
    scan_job.cancel()
    return jsonify(ok=True)


# ---------- live reconstruction ----------
live = {"mode": "idle", "width": 6.0, "saved": 0, "n": 0, "progress": 0.0, "message": "", "result": None}
live_stop = threading.Event()


def live_unavailable():
    if rec is None:
        return jsonify(error="Live mode needs OpenCV. Run: pip install opencv-python"), 501
    return None


def finish_live(colmap_path):
    """Accurate result via COLMAP when it is installed, otherwise the rough live map."""
    import scan_room
    live["mode"] = "finishing"
    videos, images = scan_job.upload_count()
    if USE_COLMAP_FOR_LIVE and (videos or images >= 20) and scan_room.find_colmap(colmap_path or None):
        scan_job.start(live["width"], None, colmap_path, reload_scanned_layout)
        live["mode"] = "idle"
        return {"method": "colmap"}
    try:
        res = rec.finish(live["width"])
    except RuntimeError as e:
        live["mode"] = "idle"
        return {"error": str(e)}
    reload_scanned_layout()
    live["mode"] = "idle"
    return {"method": "live", "width": round(float(res["width"]), 2), "depth": round(float(res["depth"]), 2),
            "height": round(float(res["height"]), 2),
            "warning": "COLMAP was not found, so this is the rough live measurement. Install COLMAP for an accurate one."}


@app.route("/api/live/start", methods=["POST"])
def api_live_start():
    bad = live_unavailable()
    if bad:
        return bad
    try:
        width = float(body().get("room_width"))
    except (TypeError, ValueError):
        width = 0
    if width <= 0:
        return jsonify(error="Type the longest wall in metres first"), 400
    if scan_job.busy():
        return jsonify(error="A COLMAP scan is still running"), 409
    live_stop.clear()
    rec.reset()
    scan_job.clear_uploads()
    live.update(mode="camera", width=width, saved=0, n=0, progress=0.0, message="", result=None)
    return jsonify(ok=True)


@app.route("/api/live/frame", methods=["POST"])
def api_live_frame():
    bad = live_unavailable()
    if bad:
        return bad
    if live["mode"] != "camera":
        return jsonify(error="Live camera is not running"), 409
    data = request.get_data()
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return jsonify(error="Bad image"), 400
    st = rec.add_frame(img)
    live["n"] += 1
    ok_frame = not st["message"].startswith(("Too blurry", "Not enough"))
    if ok_frame and live["n"] % 3 == 0 and live["saved"] < 200:  # keep sharp, spaced frames for COLMAP
        with open(os.path.join(scan_job.WS, "uploads", "live_%04d.jpg" % live["saved"]), "wb") as f:
            f.write(data)
        live["saved"] += 1
    return jsonify(st)


@app.route("/api/live/state")
def api_live_state():
    bad = live_unavailable()
    if bad:
        return bad
    if live["mode"] in ("camera", "video"):
        out = rec.snapshot(live["width"])
    else:
        out = dict(rec._status(), cloud=None)
    out["live"] = dict(live, saved=live["saved"])
    return jsonify(out)


@app.route("/api/live/finish", methods=["POST"])
def api_live_finish():
    bad = live_unavailable()
    if bad:
        return bad
    if live["mode"] != "camera":
        return jsonify(error="Live camera is not running"), 409
    return jsonify(finish_live((body().get("colmap") or "").strip().strip('"')))


@app.route("/api/live/stop", methods=["POST"])
def api_live_stop():
    live_stop.set()
    live["mode"] = "idle"
    return jsonify(ok=True)


@app.route("/api/live/video", methods=["POST"])
def api_live_video():
    bad = live_unavailable()
    if bad:
        return bad
    try:
        width = float(request.form.get("room_width"))
    except (TypeError, ValueError):
        width = 0
    if width <= 0:
        return jsonify(error="Type the longest wall in metres first"), 400
    if scan_job.busy():
        return jsonify(error="A COLMAP scan is still running"), 409
    if not scan_job.save_uploads(request.files.getlist("files")) or not scan_job.upload_count()[0]:
        return jsonify(error="Upload a video file (mp4, mov, webm)"), 400
    colmap_path = (request.form.get("colmap") or "").strip().strip('"')
    video = sorted(f for f in os.listdir(os.path.join(scan_job.WS, "uploads"))
                   if os.path.splitext(f)[1].lower() in scan_job.VIDEO)[0]
    live_stop.clear()
    rec.reset()
    live.update(mode="video", width=width, saved=0, n=0, progress=0.0, message="", result=None)

    def work():
        try:
            live_recon.feed_video(rec, os.path.join(scan_job.WS, "uploads", video),
                                  lambda p: live.update(progress=p), live_stop.is_set)
            if live_stop.is_set():
                return
            live["progress"] = 1.0
            live["result"] = finish_live(colmap_path)
        except Exception as e:  # noqa: BLE001
            live["mode"], live["message"] = "error", str(e)

    threading.Thread(target=work, daemon=True).start()
    return jsonify(ok=True)


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


# ============================================================
# START
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("  SMART ROOM DIGITAL TWIN")
    print("=" * 60)
    print("  Connect the laptop to the ESP32_DIGITAL_TWIN Wi-Fi")
    print("  Open  http://127.0.0.1:%d" % FLASK_PORT)
    print("  ESP32 http://%s/data" % ESP32_IP)
    ip = lan_ip()
    if ip:
        print("  Phone on the same Wi-Fi: http://%s:%d" % (ip, FLASK_PORT))
    print("=" * 60)
    if not os.path.exists(os.path.join(BASE, "static", "vendor", "three.min.js")):
        print("  !! static/vendor/three.min.js is missing - run:  python get_vendor.py")
        print("     (needs internet once; the twin works fully offline afterwards)")
    threading.Thread(target=sensor_loop, daemon=True).start()
    threading.Thread(target=sim_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False, threaded=True)