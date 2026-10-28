"""
Live 3D reconstruction: frames arrive one by one (camera or video) and a sparse 3D map grows
while you film. Pure OpenCV + numpy, no COLMAP needed.

How it works (monocular visual SLAM, kept simple):
  1. ORB features in every frame.
  2. Two views with enough sideways movement start the map (essential matrix, triangulation).
  3. Each new frame is located against the last keyframe's 3D points (PnP + RANSAC).
  4. When the camera has moved enough, the frame becomes a keyframe and adds new 3D points.
No bundle adjustment and no loop closure, so it drifts a little and is rougher than COLMAP.
Scale is unknown until you give one real measurement (the longest wall).
"""
import math
import os
import sys
import threading
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_tools"))
import fit_room  # noqa: E402


class LiveRecon:
    def __init__(self, fov=65.0, max_side=640):
        self.lock = threading.RLock()
        self.fov = fov
        self.max_side = max_side
        self.orb = cv2.ORB_create(nfeatures=1500, scaleFactor=1.2, nlevels=8, fastThreshold=12)
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING)
        self.reset()

    # ------------------------------------------------------------ state
    def reset(self):
        with self.lock:
            self.K = None
            self.kfs = []
            self.initialized = False
            self.P = np.zeros((2048, 3))
            self.col = np.zeros((2048, 3), np.uint8)
            self.nobs = np.zeros(2048, np.int32)
            self.n = 0
            self.cams = []
            self.frames = 0
            self.lost = 0
            self.state = "waiting"
            self.message = "Waiting for frames"
            self.version = 0

    def _add_points(self, X, colors):
        k = len(X)
        while self.n + k > len(self.P):
            self.P = np.vstack([self.P, np.zeros_like(self.P)])
            self.col = np.vstack([self.col, np.zeros_like(self.col)])
            self.nobs = np.concatenate([self.nobs, np.zeros_like(self.nobs)])
        ids = np.arange(self.n, self.n + k)
        self.P[ids], self.col[ids], self.nobs[ids] = X, colors, 2
        self.n += k
        self.version += 1
        return ids

    def _match(self, dq, dt, ratio=0.75):
        out = []
        for pair in self.bf.knnMatch(dq, dt, k=2):
            if len(pair) == 2 and pair[0].distance < ratio * pair[1].distance:
                out.append(pair[0])
        return out

    def _triangulate(self, r, q, R1, t1, R2, t2, min_angle_deg):
        P1 = self.K @ np.hstack([R1, t1.reshape(3, 1)])
        P2 = self.K @ np.hstack([R2, t2.reshape(3, 1)])
        Xh = cv2.triangulatePoints(P1, P2, r.T.astype(np.float64), q.T.astype(np.float64))
        X = (Xh[:3] / Xh[3]).T
        c1, c2 = -R1.T @ t1, -R2.T @ t2
        z1 = (X @ R1.T + t1)[:, 2]
        z2 = (X @ R2.T + t2)[:, 2]

        def reproj(P, pts):
            h = np.hstack([X, np.ones((len(X), 1))]) @ P.T
            return np.linalg.norm(h[:, :2] / h[:, 2:3] - pts, axis=1)

        a, b = X - c1, X - c2
        cosang = np.sum(a * b, 1) / (np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1) + 1e-12)
        ang = np.degrees(np.arccos(np.clip(cosang, -1, 1)))
        ok = (z1 > 0) & (z2 > 0) & (reproj(P1, r) < 2.5) & (reproj(P2, q) < 2.5) & (ang > min_angle_deg)
        if ok.sum() > 10:
            ok &= z1 < 25 * np.median(z1[ok])
        return X, ok

    def _colors(self, img, pts):
        h, w = img.shape[:2]
        xi = np.clip(np.round(pts[:, 0]).astype(int), 0, w - 1)
        yi = np.clip(np.round(pts[:, 1]).astype(int), 0, h - 1)
        return img[yi, xi][:, ::-1]  # BGR -> RGB

    def _status(self):
        return {"state": self.state, "message": self.message, "frames": self.frames,
                "keyframes": len(self.kfs), "points": int(self.n), "version": self.version}

    # ------------------------------------------------------------ frames
    def add_frame(self, img):
        with self.lock:
            self.frames += 1
            h, w = img.shape[:2]
            sc = self.max_side / max(h, w)
            if sc < 1:
                img = cv2.resize(img, (int(w * sc), int(h * sc)), interpolation=cv2.INTER_AREA)
                h, w = img.shape[:2]
            if self.K is None:
                f = (w / 2) / math.tan(math.radians(self.fov) / 2)
                self.K = np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1.0]])
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            if cv2.Laplacian(gray, cv2.CV_64F).var() < 20:
                self.message = "Too blurry - move slower"
                return self._status()
            kp, des = self.orb.detectAndCompute(gray, None)
            if des is None or len(kp) < 150:
                self.message = "Not enough detail - aim at textured surfaces, not a blank wall"
                return self._status()
            cur = {"kp": np.float32([k.pt for k in kp]), "des": des,
                   "p3": np.full(len(kp), -1, np.int32), "img": img}
            if not self.kfs:
                cur.update(R=np.eye(3), t=np.zeros(3))
                self.kfs.append(cur)
                self.cams.append((np.eye(3), np.zeros(3)))
                self.state, self.message = "initializing", "Move sideways slowly so the map can start"
            elif not self.initialized:
                self._init(cur)
            else:
                self._track(cur)
            cur.pop("img", None)
            return self._status()

    def _init(self, cur):
        ref = self.kfs[0]
        m = self._match(cur["des"], ref["des"])
        if len(m) < 60:
            ref["kp"], ref["des"] = cur["kp"], cur["des"]
            ref["img"] = cur["img"]
            self.message = "Scene changed - starting again from this view"
            return
        q = cur["kp"][[x.queryIdx for x in m]]
        r = ref["kp"][[x.trainIdx for x in m]]
        if np.median(np.linalg.norm(q - r, axis=1)) < 22:
            self.message = "Move sideways a little (walk, don't just turn on the spot)"
            return
        E, mask = cv2.findEssentialMat(r, q, self.K, method=cv2.RANSAC, prob=0.999, threshold=1.0)
        if E is None or E.shape[0] < 3:
            return
        E = E[:3]
        n, R, t, mask2 = cv2.recoverPose(E, r, q, self.K, mask=mask)
        if n < 50:
            self.message = "Need more sideways movement"
            return
        inl = mask2.ravel() > 0
        X, ok = self._triangulate(r[inl], q[inl], np.eye(3), np.zeros(3), R, t.ravel(), 1.5)
        if ok.sum() < 40:
            self.message = "Need more sideways movement"
            return
        X = X[ok]
        s = 1.0 / np.median(X[:, 2])
        X, t = X * s, t.ravel() * s
        mi = np.where(inl)[0][ok]
        ids = self._add_points(X, self._colors(cur["img"], q[inl][ok]))
        ref["p3"][[m[i].trainIdx for i in mi]] = ids
        cur["p3"][[m[i].queryIdx for i in mi]] = ids
        cur.update(R=R, t=t)
        self.kfs.append(cur)
        self.cams.append((R, t))
        self.initialized = True
        self.state, self.message = "tracking", "Tracking - keep walking slowly along the walls"

    def _lose(self, why):
        self.lost += 1
        self.state = "lost"
        self.message = why

    def _pnp(self, cur, ref):
        """Locate the current frame against one keyframe's 3D points. Returns None if it fails."""
        idx = np.where(ref["p3"] >= 0)[0]
        if len(idx) < 15:
            return None
        m = self._match(cur["des"], ref["des"][idx], 0.8)
        if len(m) < 15:
            return None
        ids = ref["p3"][idx[[x.trainIdx for x in m]]]
        obj = self.P[ids].astype(np.float64)
        pts = cur["kp"][[x.queryIdx for x in m]].astype(np.float64)
        ok, rvec, tvec, inl = cv2.solvePnPRansac(obj, pts, self.K, None, iterationsCount=250, reprojectionError=3.0,
                                                 confidence=0.99, flags=cv2.SOLVEPNP_AP3P)
        if not ok or inl is None or len(inl) < 12:
            return None
        inl = inl.ravel()
        rvec, tvec = cv2.solvePnPRefineLM(obj[inl], pts[inl], self.K, None, rvec, tvec)
        R, _ = cv2.Rodrigues(rvec)
        if not (np.isfinite(R).all() and np.isfinite(tvec).all()):
            return None
        return R, tvec.ravel(), inl, ids, m, obj, len(idx)

    def _track(self, cur):
        res, ref = None, None
        for cand in reversed(self.kfs[-6:]):
            if cand["des"] is None:
                continue
            res = self._pnp(cur, cand)
            if res:
                ref = cand
                break
        if not res:
            return self._lose("Lost tracking - go back to a spot you already scanned")
        R, t, inl, ids, m, obj, nref = res
        self.lost = 0
        self.state, self.message = "tracking", "Tracking - keep walking slowly along the walls"
        self.nobs[ids[inl]] += 1
        for i in inl:
            cur["p3"][m[i].queryIdx] = ids[i]
        self.cams.append((R, t))

        zref = (obj[inl] @ ref["R"].T + ref["t"])[:, 2]
        base = np.linalg.norm((-R.T @ t) - (-ref["R"].T @ ref["t"]))
        ratio = base / max(1e-6, float(np.median(zref)))
        newest = ref is self.kfs[-1]
        if (not newest) or ratio > 0.03 or len(inl) < 0.85 * nref or len(inl) < 120:
            cur.update(R=R, t=t)
            # add new 3D points by triangulating against the last few keyframes (older ones give more parallax)
            for other in reversed(self.kfs[-3:]):
                if other["des"] is None:
                    continue
                m2 = self._match(cur["des"], other["des"])
                sel = [x for x in m2 if other["p3"][x.trainIdx] < 0 and cur["p3"][x.queryIdx] < 0]
                if len(sel) < 8:
                    continue
                r = other["kp"][[x.trainIdx for x in sel]]
                q = cur["kp"][[x.queryIdx for x in sel]]
                X, ok2 = self._triangulate(r, q, other["R"], other["t"], R, t, 1.0)
                if ok2.sum():
                    new = self._add_points(X[ok2], self._colors(cur["img"], q[ok2]))
                    for j, nid in zip(np.where(ok2)[0], new):
                        other["p3"][sel[j].trainIdx] = nid
                        cur["p3"][sel[j].queryIdx] = nid
            self.kfs.append(cur)
            if len(self.kfs) > 8:  # free old keyframes, keep the last few for recovery
                old = self.kfs[-9]
                old["des"] = old["kp"] = old["p3"] = None
                old.pop("img", None)

    # ------------------------------------------------------------ outputs
    def _arrays(self, min_obs):
        keep = np.where(self.nobs[:self.n] >= min_obs)[0]
        if len(keep) < 300:
            keep = np.arange(self.n)
        Rs = [c[0] for c in self.cams]
        centers = np.array([-c[0].T @ c[1] for c in self.cams])
        good = np.isfinite(centers).all(axis=1) & (np.abs(np.nan_to_num(centers)).max(axis=1) < 500)
        centers = centers[good]
        Rs = [R for R, g in zip(Rs, good) if g]
        return self.P[keep].copy(), self.col[keep].copy(), fit_room.ups_from_poses(Rs), centers

    def snapshot(self, room_width, max_points=5000):
        """Status + the cloud, levelled and scaled so it can be drawn in the page."""
        with self.lock:
            st = self._status()
            if self.n < 200 or len(self.cams) < 4:
                return dict(st, cloud=None)
            P, C, ups, centers = self._arrays(2)
        try:
            Q, C2, cam, (W, D, H) = fit_room.preview(P, C, ups, centers, room_width, max_points)
        except (ValueError, SystemExit, np.linalg.LinAlgError):
            return dict(st, cloud=None)
        step = max(1, len(cam) // 80)
        return dict(st, cloud={
            "dims": [round(float(W), 2), round(float(D), 2), round(float(H), 2)],
            "p": np.round(Q * 100).astype(int).ravel().tolist(),
            "c": C2.ravel().tolist(),
            "cam": np.round(cam[::step] * 100).astype(int).ravel().tolist(),
            "now": np.round(cam[-1] * 100).astype(int).tolist(),
        })

    def finish(self, room_width, out_dir=None):
        with self.lock:
            if self.n < 200 or len(self.cams) < 4:
                raise RuntimeError("Not enough points yet. Keep filming: walk slowly along the walls.")
            P, C, ups, _ = self._arrays(3)
        kw = {"out_dir": out_dir} if out_dir else {}
        return fit_room.run_arrays(P, C, None, None, ups, room_width, verbose=False, **kw)


# ------------------------------------------------------------ video feeder
def feed_video(rec, path, on_progress, should_stop, fps_target=6.0):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("Cannot read that video file.")
    total = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    stride = max(1, int(round(fps / fps_target)))
    i = 0
    while True:
        if should_stop():
            break
        if not cap.grab():
            break
        if i % stride == 0:
            ok, frame = cap.retrieve()
            if ok:
                rec.add_frame(frame)
        i += 1
        if total and i % 10 == 0:
            on_progress(min(0.99, i / total))
    cap.release()
