"""
Turn a COLMAP reconstruction into real room data for the digital twin.
Works with the SPARSE model, so no NVIDIA GPU or dense reconstruction is needed.
Only needs numpy (opencv is optional, for the preview picture).

What it does
  1. reads points3D.bin and (for the 'up' direction) images.bin from a COLMAP sparse model
     (also accepts a .ply point cloud or points3D.txt)
  2. finds which way is up (average camera orientation), levels the room, finds the walls' direction
  3. measures width, depth, height, and sets real scale from one tape measurement
  4. writes static/models/room_layout.json   (room size - the twin loads it automatically)
             static/models/room_points.json  (the scanned points, shown inside the twin)
             static/models/scan_preview.png  (top + side view, check it looks right)

    python scan_tools/fit_room.py scan_ws/sparse/0 --room-width 6.2
"""
import argparse
import json
import os
import struct
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.normpath(os.path.join(HERE, "..", "static", "models"))


# ------------------------------------------------------------------ readers
def read_points3d_bin(path):
    pts, col, err, trk = [], [], [], []
    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        for _ in range(n):
            f.read(8)
            pts.append(struct.unpack("<3d", f.read(24)))
            col.append(struct.unpack("<3B", f.read(3)))
            err.append(struct.unpack("<d", f.read(8))[0])
            t = struct.unpack("<Q", f.read(8))[0]
            trk.append(t)
            f.seek(8 * t, 1)
    return np.array(pts), np.array(col, dtype=np.uint8), np.array(err), np.array(trk)


def read_points3d_txt(path):
    pts, col, err, trk = [], [], [], []
    for line in open(path):
        if line.startswith("#") or not line.strip():
            continue
        v = line.split()
        pts.append([float(v[1]), float(v[2]), float(v[3])])
        col.append([int(v[4]), int(v[5]), int(v[6])])
        err.append(float(v[7]))
        trk.append((len(v) - 8) // 2)
    return np.array(pts), np.array(col, dtype=np.uint8), np.array(err), np.array(trk)


PLY_TYPES = {"float": "f4", "float32": "f4", "double": "f8", "float64": "f8", "uchar": "u1", "uint8": "u1",
             "char": "i1", "int8": "i1", "short": "i2", "int16": "i2", "ushort": "u2", "uint16": "u2",
             "int": "i4", "int32": "i4", "uint": "u4", "uint32": "u4"}


def read_ply(path):
    with open(path, "rb") as f:
        fmt, props, count, in_vertex = None, [], 0, False
        while True:
            line = f.readline().decode("ascii", "ignore").strip()
            if line.startswith("format"):
                fmt = line.split()[1]
            elif line.startswith("element"):
                parts = line.split()
                in_vertex = parts[1] == "vertex"
                if in_vertex:
                    count = int(parts[2])
            elif line.startswith("property") and in_vertex:
                _, t, name = line.split()[:3]
                props.append((name, PLY_TYPES[t]))
            elif line == "end_header":
                break
            elif line == "":
                raise ValueError("bad PLY header")
        if fmt == "ascii":
            data = np.loadtxt(f, max_rows=count)
            arr = {n: data[:, i] for i, (n, _) in enumerate(props)}
        elif fmt == "binary_little_endian":
            raw = np.fromfile(f, dtype=np.dtype(props), count=count)
            arr = {n: raw[n] for n, _ in props}
        else:
            raise ValueError("PLY format %s not supported" % fmt)
    pts = np.stack([arr["x"], arr["y"], arr["z"]], axis=1).astype(float)
    if "red" in arr:
        col = np.stack([arr["red"], arr["green"], arr["blue"]], axis=1).astype(np.uint8)
    else:
        col = np.full((len(pts), 3), 200, dtype=np.uint8)
    return pts, col, np.zeros(len(pts)), np.full(len(pts), 99)


def quat_to_rot(q):
    w, x, y, z = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def read_camera_ups(path):
    """World-space 'up' of every registered photo (COLMAP's camera y axis points down)."""
    ups = []
    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        for _ in range(n):
            f.read(4)
            q = struct.unpack("<4d", f.read(32))
            f.read(24)
            f.read(4)
            while True:
                c = f.read(1)
                if c in (b"\x00", b""):
                    break
            m = struct.unpack("<Q", f.read(8))[0]
            f.seek(24 * m, 1)
            ups.append(-quat_to_rot(q).T @ np.array([0.0, 1.0, 0.0]))
    return np.array(ups)


# ------------------------------------------------------------------ geometry
def rotation_between(a, b):
    a, b = a / np.linalg.norm(a), b / np.linalg.norm(b)
    v, c = np.cross(a, b), float(a @ b)
    if c < -0.999999:
        axis = np.cross(a, [1.0, 0, 0])
        if np.linalg.norm(axis) < 1e-6:
            axis = np.cross(a, [0, 1.0, 0])
        axis /= np.linalg.norm(axis)
        return 2 * np.outer(axis, axis) - np.eye(3)
    k = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + k + k @ k / (1 + c)


def ransac_normal(P, iters=1500, seed=0):
    rng = np.random.default_rng(seed)
    thr = 0.012 * np.ptp(P, axis=0).max()
    best, best_n = -1, None
    for _ in range(iters):
        a, b, c = P[rng.choice(len(P), 3, replace=False)]
        n = np.cross(b - a, c - a)
        L = np.linalg.norm(n)
        if L < 1e-12:
            continue
        n /= L
        inl = int((np.abs((P - a) @ n) < thr).sum())
        if inl > best:
            best, best_n = inl, n
    return best_n


def trim(P, k=5.0):
    keep = np.ones(len(P), bool)
    for _ in range(2):
        for ax in range(3):
            v = P[keep, ax]
            med = np.median(v)
            mad = np.median(np.abs(v - med)) * 1.4826 + 1e-9
            keep &= np.abs(P[:, ax] - med) < k * mad
    return keep


def wall_angle(xz):
    best, best_deg = -1.0, 0
    span = np.ptp(xz, axis=0).max()
    for deg in range(90):
        t = np.radians(deg)
        c, s = np.cos(t), np.sin(t)
        x = xz[:, 0] * c - xz[:, 1] * s
        z = xz[:, 0] * s + xz[:, 1] * c
        score = 0.0
        for v in (x, z):
            h, _ = np.histogram(v, bins=max(40, int(span / (span / 160))))
            score += float((h.astype(float) ** 2).sum())
        if score > best:
            best, best_deg = score, deg
    return np.radians(best_deg)


# ------------------------------------------------------------------ main logic
def ups_from_poses(Rs):
    """World-space 'up' for each camera pose R (world->camera, OpenCV/COLMAP convention)."""
    return np.array([-R.T @ np.array([0.0, 1.0, 0.0]) for R in Rs])


def estimate_alignment(P, cam_up, room_width, room_depth=None, flip=False, say=print):
    """Find the transform that levels the room, squares the walls, and sets real scale.
    Returns (T, (W, D, H)); T is a dict usable with apply_alignment()."""
    up = None
    if cam_up is not None and len(cam_up) >= 3:
        m = cam_up.mean(axis=0)
        if np.linalg.norm(m) > 0.4:
            up = m / np.linalg.norm(m)
            say("up direction from %d camera poses" % len(cam_up))
    if up is None:
        up = ransac_normal(P)
        say("WARNING: up direction guessed from the biggest flat surface - check the preview image.")
    if flip:
        up = -up
    Ru = rotation_between(up, np.array([0.0, 1.0, 0.0]))
    P1 = P @ Ru.T
    y_lo, y_hi = np.percentile(P1[:, 1], [1.5, 98.5])
    P1[:, 1] -= y_lo
    height = y_hi - y_lo

    t = wall_angle(P1[:, [0, 2]])
    c, s = np.cos(t), np.sin(t)
    x, z = P1[:, 0] * c - P1[:, 2] * s, P1[:, 0] * s + P1[:, 2] * c
    x_lo, x_hi = np.percentile(x, [1.5, 98.5])
    z_lo, z_hi = np.percentile(z, [1.5, 98.5])
    ex, ez = x_hi - x_lo, z_hi - z_lo

    scale = room_width / max(ex, ez)
    W, D, H = ex * scale, ez * scale, height * scale
    if room_depth:
        sd = room_depth / min(ex, ez)
        if abs(sd - scale) / scale > 0.10:
            say("WARNING: your width and depth measurements disagree by %.0f%% - re-measure or re-scan."
                % (100 * abs(sd - scale) / scale))
    if not (2.0 <= H <= 5.0):
        say("WARNING: height %.2f m looks wrong - the up direction may be off. Try --flip or check the preview." % H)
    T = {"Ru": Ru, "y_lo": y_lo, "c": c, "s": s, "x_lo": x_lo, "z_lo": z_lo, "scale": scale}
    return T, (W, D, H)


def apply_alignment(T, X):
    X = np.atleast_2d(X) @ T["Ru"].T
    y = X[:, 1] - T["y_lo"]
    x = X[:, 0] * T["c"] - X[:, 2] * T["s"]
    z = X[:, 0] * T["s"] + X[:, 2] * T["c"]
    return np.stack([(x - T["x_lo"]) * T["scale"], y * T["scale"], (z - T["z_lo"]) * T["scale"]], axis=1)


def clean(P, C, E=None, T=None):
    if E is not None and T is not None:
        good = (T >= 3) & (E < 2.0)
        if good.sum() > 300:
            P, C = P[good], C[good]
    k = trim(P)
    return P[k], C[k]


def preview(P, C, cam_up, cam_centers, room_width, max_points=6000):
    """Quick aligned view for the live mode (nothing is written)."""
    P, C = clean(P, C)
    if len(P) < 200:
        raise ValueError("too few points")
    T, dims = estimate_alignment(P, cam_up, room_width, say=lambda *a, **k: None)
    Q = apply_alignment(T, P)
    if len(Q) > max_points:
        idx = np.random.default_rng(1).choice(len(Q), max_points, replace=False)
        Q, C = Q[idx], C[idx]
    return Q, C, apply_alignment(T, cam_centers), dims


def run_arrays(P, C, E, Tk, cam_up, room_width, out_dir=DEFAULT_OUT, room_depth=None, flip=False,
               max_points=25000, verbose=True):
    say = print if verbose else (lambda *a, **k: None)
    say("points read:", len(P))
    P, C = clean(P, C, E, Tk)
    say("points after cleaning:", len(P))
    if len(P) < 200:
        raise SystemExit("Too few points to measure a room. Re-shoot with more overlap and light.")
    T, (W, D, H) = estimate_alignment(P, cam_up, room_width, room_depth, flip, say)
    say("Measured room: %.2f m x %.2f m, height %.2f m" % (W, D, H))

    Q = apply_alignment(T, P)
    inside = (Q[:, 0] > -0.3) & (Q[:, 0] < W + 0.3) & (Q[:, 1] > -0.3) & (Q[:, 1] < H + 0.3) & \
             (Q[:, 2] > -0.3) & (Q[:, 2] < D + 0.3)
    Q, C = Q[inside], C[inside]
    if len(Q) > max_points:
        idx = np.random.default_rng(1).choice(len(Q), max_points, replace=False)
        Q, C = Q[idx], C[idx]

    os.makedirs(out_dir, exist_ok=True)
    layout = {
        "width": round(W, 2), "depth": round(D, 2), "height": round(H, 2),
        "exits": [
            {"id": "A", "name": "Door A", "side": "south", "pos": round(W * 0.2, 2), "width": 1.0},
            {"id": "B", "name": "Door B", "side": "east", "pos": round(D * 0.5, 2), "width": 1.0},
        ],
        "furniture": [],
        "sensors": {"esp32": {"x": round(max(0.5, W - 1.0), 2), "z": 0.8},
                    "pir": {"x": round(W * 0.7, 2), "z": round(D * 0.4, 2), "radius": 1.4}},
    }
    json.dump(layout, open(os.path.join(out_dir, "room_layout.json"), "w"), indent=2)
    json.dump({"unit": "cm", "p": np.round(Q * 100).astype(int).ravel().tolist(), "c": C.ravel().tolist()},
              open(os.path.join(out_dir, "room_points.json"), "w"))
    _preview(Q, C, W, D, H, os.path.join(out_dir, "scan_preview.png"), say)
    say("Saved room_layout.json, room_points.json in", out_dir)
    say("Next: refresh the twin, then set your real door positions in 'Room setup'.")
    return {"width": W, "depth": D, "height": H, "points": len(Q)}


def run(model, room_width, out_dir=DEFAULT_OUT, room_depth=None, flip=False, max_points=25000, verbose=True):
    cam_up = None
    if os.path.isdir(model):
        pb, pt = os.path.join(model, "points3D.bin"), os.path.join(model, "points3D.txt")
        ib = os.path.join(model, "images.bin")
        if os.path.exists(pb):
            P, C, E, T = read_points3d_bin(pb)
        elif os.path.exists(pt):
            P, C, E, T = read_points3d_txt(pt)
        else:
            raise SystemExit("No points3D.bin / points3D.txt in " + model)
        if os.path.exists(ib):
            cam_up = read_camera_ups(ib)
    elif model.lower().endswith(".ply"):
        P, C, E, T = read_ply(model)
    else:
        raise SystemExit("Give a COLMAP sparse folder (e.g. scan_ws/sparse/0) or a .ply file")
    return run_arrays(P, C, E, T, cam_up, room_width, out_dir, room_depth, flip, max_points, verbose)


def _preview(Q, C, W, D, H, path, say):
    try:
        import cv2
    except ImportError:
        return
    S = 520.0 / max(W, D)
    pad = 30
    top = np.full((int(D * S) + 2 * pad, int(W * S) + 2 * pad, 3), 20, np.uint8)
    side = np.full((int(H * S) + 2 * pad, int(W * S) + 2 * pad, 3), 20, np.uint8)
    for (q, col) in zip(Q, C):
        bgr = (int(col[2]), int(col[1]), int(col[0]))
        cv2.circle(top, (int(q[0] * S) + pad, int(q[2] * S) + pad), 1, bgr, -1)
        cv2.circle(side, (int(q[0] * S) + pad, int((H - q[1]) * S) + pad), 1, bgr, -1)
    cv2.rectangle(top, (pad, pad), (int(W * S) + pad, int(D * S) + pad), (80, 200, 100), 1)
    cv2.rectangle(side, (pad, pad), (int(W * S) + pad, int(H * S) + pad), (80, 200, 100), 1)
    cv2.putText(top, "TOP VIEW  %.1f x %.1f m" % (W, D), (pad, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (230, 230, 230), 1)
    cv2.putText(side, "SIDE VIEW  height %.1f m" % H, (pad, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (230, 230, 230), 1)
    w = max(top.shape[1], side.shape[1])
    pad_to = lambda im: np.pad(im, ((0, 0), (0, w - im.shape[1]), (0, 0)), constant_values=20)
    cv2.imwrite(path, np.vstack([pad_to(top), pad_to(side)]))
    say("Preview picture:", path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("model", help="COLMAP sparse folder (scan_ws/sparse/0) or a .ply file")
    ap.add_argument("--room-width", type=float, required=True, help="real length in m of the room's longest side")
    ap.add_argument("--room-depth", type=float, help="real length of the shorter side (optional, sanity check)")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--flip", action="store_true", help="use if the preview shows the room upside down")
    a = ap.parse_args()
    run(a.model, a.room_width, a.out, a.room_depth, a.flip)
