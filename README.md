# Smart Room Digital Twin

Hardware and the Arduino/ESP32 sketch are unchanged. The ESP32 still serves
`http://192.168.4.1/data` -> `{temperature, humidity, pir, ldr, gas}`.

## 1. One-time setup (needs internet once)

```
pip install flask requests
```

Then download Three.js once (the laptop has no internet while it is on the ESP32 Wi-Fi):

```
python get_vendor.py
```
This saves `three.min.js` (and `GLTFLoader.js`) into `static/vendor/`. If it fails, the script prints
the links to save by hand. After this one step the twin runs fully offline: the page, the 3D room,
the simulation and the sensor log never touch the internet.

## 2. Run

```
python app.py
```
Open http://127.0.0.1:5000

No hardware at hand? Click **Demo** under "Sensor source". The "Spike gas"
and "Spike heat" buttons test the automatic emergency trigger.

## 3. Using it

- Pick Fire, Gas leak or Flood, then click the floor to place it. Earthquake fires instantly.
- Green lines are the live evacuation routes. They re-route as fire, smoke, gas, water and debris change.
- "Block" closes an exit by hand. Speed 1x/2x/4x speeds up the simulation.
- Auto-trigger: gas >= 2500 starts a gas leak at the ESP32, temperature >= 50 C starts a fire (change in engine.py).
- A PIR detection adds a cyan "live person" who evacuates with everyone else.

## 4. Plug in the COLMAP / Open3D scan

1. Export the cleaned, scaled mesh as `static/models/room.glb` (1 unit = 1 m).
2. Edit `LAYOUT["model"]` in engine.py (scale, offset, rotation_y) until it lines up with the wall outline.
3. Edit `LAYOUT` (width, depth, exits, furniture, sensors) to match the real room.
   The evacuation grid is built from this layout, not from the mesh.

When the model loads, the procedural walls and furniture hide; fire, smoke,
people and sensors stay on top (the hybrid model).

## Files

- app.py          Flask server, ESP32 collector, demo sensors, API
- engine.py       Layout, hazards, occupants, evacuation routing
- static/         index.html, style.css, app.js (Three.js scene and dashboard)

## Scan tool

`scan_tools/process_scan.py` turns COLMAP's `fused.ply` into `static/models/room.glb`
(cleans noise, levels the floor, sets real scale, meshes, shrinks for the browser).
Usage is in the file header. Needs `pip install open3d numpy`.

## Will it fit? (object check)

In the side panel: choose a photo, type its real size in cm, click "Place in room", then
click the floor to position it (R rotates). It turns red when it overlaps a wall or furniture,
blocks an exit, or cuts people off from an exit, and shows how much the longest escape route grows.
Placed objects also block the evacuation routes and burn in fire like the other furniture.

## Phone / IP camera scan in one command

    pip install opencv-python open3d numpy        (and install COLMAP)
    python scan_tools/scan_room.py http://PHONE_IP:8080 --room-width 6.2

This is not a live render. It grabs sharp photos while you walk the room, then runs COLMAP
(10-30 min). Without an NVIDIA GPU only the sparse cloud is available, so the model is rough.


## Room setup (no code editing)

Side panel -> **Room setup**: enter the real width, depth and height, set each door (wall, position
along the wall, width), and the ESP32 / PIR positions, then **Save room**. It is stored in
`static/models/room_layout.json`. "Example room" restores the built-in room. Add real furniture
with "Will it fit?".

## Real room from COLMAP (works without a GPU)

```
python scan_tools/scan_room.py http://PHONE_IP:8080 --room-width 6.2
```
(or `--photos FOLDER` with photos you already took). It runs COLMAP's sparse reconstruction,
`fit_room.py` finds which way is up from the camera poses, levels the room, finds the walls, measures
width / depth / height, and writes the room size plus the scanned points (shown in the twin; toggle
"Scan points"). Open `static/models/scan_preview.png` to check the top and side views look right.
Doors are NOT detected: set them in Room setup. If the preview is upside down, add `--flip` to
`python scan_tools/fit_room.py scan_ws/sparse/0 --room-width 6.2 --flip`.

## Other features

- Scenario presets (fire, gas, earthquake, flood, blocked exit) in the Disaster engine panel.
- Alarm sound while an evacuation is running (checkbox).
- "Download report (CSV)": evacuation time, exits used, each occupant's outcome, event log.
- Every sensor reading is appended to `logs/sensors.csv`.

## Scanning with the laptop webcam or a video file

    python scan_tools/scan_room.py 0 --room-width 6.2 --colmap C:\colmap\COLMAP.bat
    python scan_tools/scan_room.py C:\videos\room.mp4 --room-width 6.2 --colmap C:\colmap\COLMAP.bat

`0` is the laptop camera (try `1` if it opens the wrong one). A preview window shows the photo count and
warns when the picture is blurry. Press Q to stop. A video file works too: photos are spread over the whole clip.

## 3D reconstruction from the web page (no command line)

Side panel -> **Scan the real room**: choose a video or photos (or record with the camera in the page, or
with the phone's camera app), type the longest wall in metres, click **Start 3D reconstruction**.
The server picks sharp frames, runs COLMAP (sparse), measures the room, and the page reloads with the
real room and scan points. Progress and the COLMAP log are shown live; Cancel stops it.
If COLMAP is not found automatically, type its full path (for example C:\colmap\COLMAP.bat) in the box.

From a phone: start the twin, and open the address printed at start-up ("Phone on the same Wi-Fi: ...") in the
phone's browser. Use "Record with this device's camera app" to film the room and upload it. Allow Python through
the Windows firewall if asked. Recording inside the browser only works on the laptop itself (127.0.0.1).

## Live 3D reconstruction in the page (camera or video, one flow)

Side panel -> **Live 3D reconstruction**. Type the longest wall in metres, then either click
**Start live camera** (laptop webcam, at http://127.0.0.1:5000) or upload a video. The 3D view switches to a
live point cloud that grows while you film (yellow = camera path, green box = measured room).
Needs `pip install opencv-python`.

- **Finish and measure**: if COLMAP is installed, the same footage is re-built with COLMAP for the accurate
  result and the page reloads with the real room. If COLMAP is missing, the rough live map is used instead.
- The live view is a quick preview (monocular tracking with no loop closure), so it drifts. On my synthetic
  test room it was off by up to about 30% in size, which is why Finish re-builds with COLMAP.
- Tips: translate (walk) instead of only turning on the spot, move slowly, keep the camera upright, aim at
  textured surfaces. If it says "Lost tracking", go back to a place you already scanned.
- From a phone over Wi-Fi the browser blocks live camera access on plain http; record a video with the phone
  and upload it in the same panel (it is processed live in the view).
