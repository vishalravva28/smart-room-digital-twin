# Smart Room Digital Twin

A browser-based 3D digital twin of a room. An ESP32 sensor node streams temperature, humidity, motion, light and gas readings to a Python server. The twin shows the live readings, starts emergencies automatically from sensor thresholds, and simulates fire, gas leaks, smoke, flooding and earthquake debris with occupants evacuating through the exits. Each run produces a CSV evacuation report.

The system works offline once its Python packages and the Three.js library are installed.

## Features

- Live 3D room view in the browser (Three.js)
- ESP32 sensor input: temperature, humidity, PIR motion, LDR light, gas
- Demo mode with simulated sensor values, for testing without hardware
- Automatic emergencies: gas reading of 2500 or more, temperature of 50 °C or more
- Hazard simulation: fire, gas, smoke, water, earthquake debris
- Evacuation routes that change when exits are blocked
- Room set-up panel for room size, doors and sensor positions
- "Will it fit?" check for furniture placement
- Sensor log saved to `logs/sensors.csv`
- Evacuation report download (CSV)
- Optional room measurement from a phone video (COLMAP) or a live camera panel

## Hardware (optional)

- ESP32 with temperature/humidity, PIR, LDR and gas sensors. The sketch serves JSON at `http://192.168.4.1/data`, with the fields `temperature`, `humidity`, `pir`, `ldr` and `gas`.
- A Windows laptop with Python 3.10 or newer. No dedicated graphics card is needed.

## Quick start (Windows)

Open Command Prompt in the project folder:

```
cd digital_twin
py -m venv .venv
.venv\Scripts\activate
pip install flask requests numpy opencv-python
py get_vendor.py
py app.py
```

Then open http://127.0.0.1:5000 in Chrome or Edge.

- Click **Demo** under *Sensor source* to test without hardware.
- To use the ESP32, connect the laptop to the `ESP32_DIGITAL_TWIN` Wi-Fi network and click **ESP32**.

`get_vendor.py` needs internet once to download Three.js. After that the project runs offline.

## Setting up the room

1. Open **Room setup** in the side panel.
2. Enter the room's width, depth and height in metres.
3. Add each door: its wall, its position along that wall, and its width.
4. Enter the positions of the ESP32 and PIR sensors.
5. Click **Save room**.

Positions are measured in metres from the north-west corner. Sizes are rounded up to the nearest 0.5 m.

## Optional: measuring the room from video

The **Accurate scan** section can build a room model from a walk-through video or photos using COLMAP, a separate program. Install it from https://colmap.github.io (the Windows "nocuda" build works without an NVIDIA graphics card), and type its path, for example `C:\colmap\COLMAP.bat`, in the COLMAP path box.

Photo-based reconstruction is slow on a CPU-only laptop and needs a steady, well-lit recording with plenty of texture. The manual room set-up is the recommended method.

## Project structure

```
app.py              Flask server: sensor reader, simulation, web API
engine.py           Room grid, hazards, occupants, evacuation routes
get_vendor.py       Downloads Three.js for offline use
live_recon.py       Live camera / video tracking (OpenCV)
scan_job.py         Background job for the COLMAP scan
scan_tools/         Scan utilities: frame extraction, room fitting
static/             Web page (index.html, app.js, style.css)
static/models/      Room layout and scan results
logs/               Sensor log (created automatically)
```

## Limitations

- The room is a simplified box with doors set by hand. Doors and furniture are not detected automatically.
- The live camera measurement is a rough estimate.
- Simulated smoke and heat are simplified models, not a physical fire simulation.
- This is a student project and is not a safety certification.

## Author

Vishal, Computer Science and Engineering, VIT-AP University.
