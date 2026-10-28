"""Download the Three.js files once (needs internet). After that the twin runs fully offline."""
import os
import sys
import urllib.request

FILES = {
    "three.min.js": "https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js",
    "GLTFLoader.js": "https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/loaders/GLTFLoader.js",
}
dest = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "vendor")
os.makedirs(dest, exist_ok=True)
ok = True
for name, url in FILES.items():
    path = os.path.join(dest, name)
    try:
        print("Downloading", name, "...", end=" ", flush=True)
        data = urllib.request.urlopen(url, timeout=30).read()
        if len(data) < 5000:
            raise ValueError("file too small")
        open(path, "wb").write(data)
        print("%d KB" % (len(data) // 1024))
    except Exception as e:
        ok = False
        print("FAILED (%s)" % e)
        print("  Open this link in a browser and save it as", path, "\n ", url)
print("Done." if ok else "Some files failed - see above.")
sys.exit(0 if ok else 1)
