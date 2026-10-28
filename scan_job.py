"""
Background job that turns uploaded photos / video into a measured room, so the whole
3D reconstruction can be started from the web page.

upload -> sharp frames -> COLMAP (sparse) -> fit_room -> twin reloads with the real room
"""
import contextlib
import glob
import os
import shutil
import subprocess
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.join(BASE, "scan_ws")
MODELS = os.path.join(BASE, "static", "models")
sys.path.insert(0, os.path.join(BASE, "scan_tools"))

VIDEO = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v"}
IMAGE = {".jpg", ".jpeg", ".png"}
ACTIVE = ("extracting", "colmap", "fitting")

_lock = threading.Lock()
_job = {"state": "idle", "message": "", "progress": 0, "log": [], "result": None, "finished": 0}
_proc = None
_cancel = False


def _set(**kw):
    with _lock:
        _job.update(kw)


def _log(line):
    line = line.rstrip()
    if line:
        with _lock:
            _job["log"].append(line[:300])
            del _job["log"][:-80]


class _LogWriter:
    def write(self, text):
        for part in text.replace("\r", "\n").split("\n"):
            if part.strip() and not part.startswith("saved "):
                _log(part)
        return len(text)

    def flush(self):
        pass


def status():
    with _lock:
        return dict(_job, log=list(_job["log"][-25:]))


def busy():
    return _job["state"] in ACTIVE


def _clear_folder(path):
    os.makedirs(path, exist_ok=True)
    for name in os.listdir(path):
        p = os.path.join(path, name)
        try:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
        except OSError:
            pass


def save_uploads(files):
    """files: werkzeug FileStorage list. Returns number saved."""
    from werkzeug.utils import secure_filename
    up = os.path.join(WS, "uploads")
    _clear_folder(up)
    n = 0
    for f in files:
        name = secure_filename(f.filename or "")
        if os.path.splitext(name)[1].lower() in VIDEO | IMAGE:
            f.save(os.path.join(up, "%03d_%s" % (n, name)))
            n += 1
    return n


def clear_uploads():
    _clear_folder(os.path.join(WS, "uploads"))


def upload_count():
    """(videos, images) currently waiting in the uploads folder."""
    files = glob.glob(os.path.join(WS, "uploads", "*"))
    ext = lambda f: os.path.splitext(f)[1].lower()
    return sum(1 for f in files if ext(f) in VIDEO), sum(1 for f in files if ext(f) in IMAGE)


def cancel():
    global _cancel
    _cancel = True
    if _proc and _proc.poll() is None:
        # COLMAP.bat starts colmap.exe as a child, so stop the whole process tree, not just the .bat
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(_proc.pid)], capture_output=True)
        else:
            _proc.kill()


def start(room_width, room_depth, colmap_path, on_done):
    if busy():
        return False
    threading.Thread(target=_run, args=(room_width, room_depth, colmap_path, on_done), daemon=True).start()
    return True


def _run(width, depth, colmap_path, on_done):
    global _proc, _cancel
    _cancel = False
    _set(state="extracting", message="Preparing photos", progress=5, log=[], result=None)
    try:
        up, photos = os.path.join(WS, "uploads"), os.path.join(WS, "photos")
        for name in ("photos", "sparse", "dense"):
            shutil.rmtree(os.path.join(WS, name), ignore_errors=True)
        for name in ("database.db",):
            try:
                os.remove(os.path.join(WS, name))
            except OSError:
                pass
        os.makedirs(photos)
        files = sorted(glob.glob(os.path.join(up, "*")))
        vids = [f for f in files if os.path.splitext(f)[1].lower() in VIDEO]
        imgs = [f for f in files if os.path.splitext(f)[1].lower() in IMAGE]
        if not files:
            raise RuntimeError("No uploaded files found. Upload a video or photos first.")

        if vids:
            try:
                import grab_frames
            except ImportError:
                raise RuntimeError("opencv is missing. Run: pip install opencv-python")
            per = max(30, 300 // len(vids))
            for i, v in enumerate(vids):
                _log("Picking sharp frames from " + os.path.basename(v))
                tmp = os.path.join(WS, "frames_%d" % i)
                shutil.rmtree(tmp, ignore_errors=True)
                with contextlib.redirect_stdout(_LogWriter()):
                    grab_frames.grab(v, tmp, count=per, min_sharp=25.0, preview=False)
                for fn in sorted(os.listdir(tmp)):
                    shutil.move(os.path.join(tmp, fn), os.path.join(photos, "v%d_%s" % (i, fn)))
                shutil.rmtree(tmp, ignore_errors=True)
        for k, im in enumerate(imgs):
            shutil.copy(im, os.path.join(photos, "p%04d%s" % (k, os.path.splitext(im)[1].lower())))
        n = len(os.listdir(photos))
        _log("%d photos ready" % n)
        if n < 20:
            raise RuntimeError("Only %d usable photos. Record slower, add light, and keep the camera steady "
                               "(at least 20, ideally 100+)." % n)

        import scan_room
        colmap = scan_room.find_colmap(colmap_path or None)
        if not colmap:
            raise RuntimeError("COLMAP not found. Install it (colmap.github.io), then type the full path to "
                               "COLMAP.bat in the COLMAP path box, for example C:\\colmap\\COLMAP.bat")
        _log("COLMAP: " + colmap)

        _set(state="colmap", message="COLMAP is reconstructing (about 10 to 30 minutes)", progress=15)
        # COLMAP.bat breaks on paths with spaces, so use short relative paths run from the project folder
        cmd = [colmap, "automatic_reconstructor", "--workspace_path", "scan_ws",
               "--image_path", os.path.join("scan_ws", "photos"),
               "--data_type", "video", "--quality", "medium", "--single_camera", "1",
               "--use_gpu", "0", "--dense", "0"]
        _proc = subprocess.Popen(cmd, cwd=BASE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 bufsize=1, encoding="utf-8", errors="replace")
        for line in _proc.stdout:
            _log(line)
            low = line.lower()
            if "feature extraction" in low or "extracting" in low:
                _set(progress=25, message="COLMAP: finding features in the photos")
            elif "matching" in low:
                _set(progress=45, message="COLMAP: matching the photos")
            elif "register" in low or "bundle" in low or "mapper" in low or "reconstruct" in low:
                _set(progress=70, message="COLMAP: building the 3D points")
        _proc.wait()
        if _cancel:
            _set(state="cancelled", message="Cancelled", progress=0, finished=time.time())
            return

        model = scan_room.best_sparse_model(WS)
        if not model:
            raise RuntimeError("COLMAP could not reconstruct the room. Use 100+ photos with 70% overlap, "
                               "good light, slow movement, and textured surfaces (not blank walls).")

        _set(state="fitting", message="Measuring the room", progress=92)
        import fit_room
        with contextlib.redirect_stdout(_LogWriter()):
            res = fit_room.run(model, width, MODELS, depth)
        on_done()
        res = {k: round(float(v), 2) for k, v in res.items()}
        _set(state="done", progress=100, finished=time.time(), result=res,
             message="Room measured: %.1f x %.1f m, height %.1f m. Reloading the twin..." %
                     (res["width"], res["depth"], res["height"]))
    except (Exception, SystemExit) as e:
        _set(state="error", message=str(e) or e.__class__.__name__, finished=time.time())