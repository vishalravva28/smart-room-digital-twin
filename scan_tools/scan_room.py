"""
One command: phone camera -> sharp photos -> COLMAP -> measured room inside the digital twin.

    python scan_tools/scan_room.py http://192.168.1.50:8080 --room-width 6.2

  --room-width   real length (m) of the room's LONGEST side, measured with a tape
  --photos DIR   use photos you already took instead of the phone stream
  --colmap PATH  full path to colmap.exe / COLMAP.bat if it is not on PATH
  --gpu          let COLMAP use the GPU for feature matching (if you have one)
  --dense        also try dense reconstruction (needs an NVIDIA GPU build of COLMAP)

Works without a GPU: it uses COLMAP's sparse model, which is enough to measure the room.
Needs: COLMAP installed (https://colmap.github.io), pip install numpy opencv-python
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def find_colmap(given):
    if given:
        return given if os.path.exists(given) else None
    for name in ("colmap", "COLMAP.bat", "colmap.exe"):
        p = shutil.which(name)
        if p:
            return p
    for pattern in (r"C:\colmap*\COLMAP.bat", r"C:\colmap*\bin\colmap.exe",
                    r"C:\Program Files\COLMAP*\COLMAP.bat", os.path.expanduser(r"~\colmap*\COLMAP.bat")):
        hits = glob.glob(pattern)
        if hits:
            return hits[0]
    return None


def best_sparse_model(ws):
    models = glob.glob(os.path.join(ws, "sparse", "*", "points3D.bin"))
    return os.path.dirname(max(models, key=os.path.getsize)) if models else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", help="0 = laptop webcam, a phone address (http://192.168.1.50:8080), or a video file")
    ap.add_argument("--room-width", type=float, required=True)
    ap.add_argument("--room-depth", type=float, help="shorter side in m (optional sanity check)")
    ap.add_argument("--photos", help="folder of photos to use instead of the phone stream")
    ap.add_argument("--frames", type=int, default=120)
    ap.add_argument("--workspace", default="scan_ws")
    ap.add_argument("--colmap")
    ap.add_argument("--gpu", action="store_true")
    ap.add_argument("--dense", action="store_true")
    ap.add_argument("--sequential", action="store_true", help="photos are a continuous walk-through (video-like)")
    a = ap.parse_args()

    colmap = find_colmap(a.colmap)
    if not colmap:
        sys.exit("COLMAP not found.\n"
                 "  1. Download it from https://colmap.github.io (Windows: the 'nocuda' zip is fine)\n"
                 "  2. Unzip to C:\\colmap and run again with  --colmap C:\\colmap\\COLMAP.bat")
    print("COLMAP:", colmap)

    ws = a.workspace
    photos = a.photos or os.path.join(ws, "photos")
    if not a.photos:
        if not a.url:
            sys.exit("Give the camera address, or use --photos FOLDER.")
        import grab_frames
        if grab_frames.grab(a.url, photos, a.frames) < 20:
            sys.exit("Fewer than 20 sharp photos. Move slower, add light, and retry.")
    n = len(glob.glob(os.path.join(photos, "*.jp*g")) + glob.glob(os.path.join(photos, "*.png")))
    print("Photos:", n)
    if n < 20:
        sys.exit("Need at least 20 photos.")

    os.makedirs(ws, exist_ok=True)
    cmd = [colmap, "automatic_reconstructor", "--workspace_path", ws, "--image_path", photos,
           "--data_type", "video" if a.sequential else "individual", "--quality", "medium",
           "--single_camera", "1", "--use_gpu", "1" if a.gpu else "0", "--dense", "1" if a.dense else "0"]
    print("Running COLMAP (10-30 min on a laptop)...\n ", " ".join(cmd))
    subprocess.call(cmd)

    model = best_sparse_model(ws)
    if not model:
        sys.exit("COLMAP could not reconstruct the room. Common causes: too few overlapping photos, "
                 "blank white walls, dim light, or blurry frames. Take 100+ photos with 70% overlap.")
    print("Using sparse model:", model)

    import fit_room
    fit_room.run(model, a.room_width, room_depth=a.room_depth)

    fused = os.path.join(ws, "dense", "0", "fused.ply")
    if a.dense and os.path.exists(fused):
        try:
            import open3d  # noqa: F401
            subprocess.call([sys.executable, os.path.join(HERE, "process_scan.py"), fused,
                             "--room-width", str(a.room_width)])
        except ImportError:
            print("Dense cloud found, but open3d is not installed, so no mesh was made.")

    print("\nDone. Refresh the twin. Open scan_preview.png in static/models to check the room looks right.")


if __name__ == "__main__":
    main()
