"""
Grab sharp, well-spaced photos for the room scan (the "quality check" step).
Blurry frames are skipped. The source can be:

    0                          laptop webcam (try 1 if 0 is the wrong camera)
    http://192.168.1.50:8080   phone running the "IP Webcam" app
    C:\\videos\\room.mp4         a video you recorded earlier

    pip install opencv-python
    python scan_tools/grab_frames.py 0
A preview window shows how many photos are saved. Press Q to stop early.
"""
import argparse
import os
import time

import cv2


def sharpness(img):
    return cv2.Laplacian(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()


def stream_url(base):
    base = base.rstrip("/")
    if base.startswith("rtsp") or base.endswith(("/video", "/videofeed")):
        return base
    return base + "/video"  # IP Webcam app


def open_source(src):
    s = str(src).strip().strip('"')
    if s.isdigit():
        cap = cv2.VideoCapture(int(s), cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(int(s))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        return cap, "webcam"
    if os.path.isfile(s):
        return cv2.VideoCapture(s), "file"
    return cv2.VideoCapture(stream_url(s)), "stream"


def grab(src, out="photos", count=120, interval=0.7, min_sharp=60.0, preview=True):
    os.makedirs(out, exist_ok=True)
    cap, kind = open_source(src)
    if not cap.isOpened():
        raise SystemExit({
            "webcam": "Cannot open the laptop camera. Close other apps using it, or try camera number 1.",
            "file": "Cannot read that video file.",
            "stream": "Cannot open the stream. Check the address and that the phone and laptop share a network.",
        }[kind])
    if kind == "file":  # spread the photos over the whole video
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        dur = cap.get(cv2.CAP_PROP_FRAME_COUNT) / fps
        interval = max(0.2, dur / count) if dur > 0 else interval
    else:
        print("Carry the camera slowly along the walls (keep it upright), then do a second loop at another height.")
        print("Press Q in the preview window, or Ctrl+C, to stop.")
    saved = skipped = fails = 0
    last = -1e9
    try:
        while saved < count:
            ok, frame = cap.read()
            if not ok:
                if kind == "file":
                    break
                fails += 1
                if fails > 50:
                    print("\nCamera stopped.")
                    break
                time.sleep(0.1)
                continue
            fails = 0
            now = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0 if kind == "file" else time.time()
            sharp = sharpness(frame)
            if now - last >= interval:
                last = now
                if sharp < min_sharp:
                    skipped += 1
                else:
                    cv2.imwrite(os.path.join(out, "img_%04d.jpg" % saved), frame)
                    saved += 1
                    print("\rsaved %d/%d  (blurry skipped: %d)" % (saved, count, skipped), end="")
            if preview:
                try:
                    view = frame.copy()
                    good = sharp >= min_sharp
                    cv2.putText(view, "saved %d/%d   %s" % (saved, count, "sharp" if good else "TOO BLURRY - slow down"),
                                (15, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (80, 220, 100) if good else (60, 60, 240), 2)
                    cv2.imshow("Room scan - press Q to stop", view)
                    if cv2.waitKey(1) & 0xFF in (ord("q"), ord("Q")):
                        break
                except cv2.error:
                    preview = False  # opencv without GUI support
    except KeyboardInterrupt:
        pass
    cap.release()
    try:
        cv2.destroyAllWindows()
    except cv2.error:
        pass
    print("\nDone: %d photos in %s" % (saved, out))
    return saved


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="0 (laptop webcam), a phone address, or a video file")
    ap.add_argument("--out", default="photos")
    ap.add_argument("--count", type=int, default=120)
    ap.add_argument("--interval", type=float, default=0.7, help="seconds between photos")
    ap.add_argument("--min-sharp", type=float, default=60.0)
    ap.add_argument("--no-preview", action="store_true")
    a = ap.parse_args()
    grab(a.source, a.out, a.count, a.interval, a.min_sharp, not a.no_preview)
