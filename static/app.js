(() => {
"use strict";
const $ = (id) => document.getElementById(id);
if (typeof THREE === "undefined") { $("fatal").classList.add("show"); return; }

const api = {
  get: (u) => fetch(u, { cache: "no-store" }).then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); }),
  del: (u) => fetch(u, { method: "DELETE" }).then((r) => r.json()),
  post: (u, b) => fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) }).then((r) => r.json()),
};
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));

let L = null, W = 12, D = 8, H = 3, cols = 24, rows = 16, cell = 0.5;
let armed = null, last = null, occN = 6;
const wx = (x) => x - W / 2, wz = (z) => z - D / 2;

/* ---------------- renderer / scene ---------------- */
const canvas = $("view");
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0a1220);
scene.fog = new THREE.Fog(0x0a1220, 24, 60);
const camera = new THREE.PerspectiveCamera(48, 1, 0.1, 200);
const root = new THREE.Group();
scene.add(root);
const procedural = new THREE.Group(); // floor, walls, furniture (hidden when a scan is loaded)
root.add(procedural);

scene.add(new THREE.HemisphereLight(0x9bb8ff, 0x0b1020, 0.6));
const sun = new THREE.DirectionalLight(0xffffff, 0.55);
sun.position.set(6, 12, 5);
scene.add(sun);
const roomLight = new THREE.PointLight(0xffe2a0, 0, 22);
roomLight.position.set(0, 2.6, 0);
root.add(roomLight);
const fireLight = new THREE.PointLight(0xff6a1a, 0, 12);
root.add(fireLight);

/* ---------------- orbit camera ---------------- */
const orbit = { theta: 0.75, phi: 0.95, r: 17, ty: 0.6 };
function applyCamera() {
  const sp = Math.sin(orbit.phi), cp = Math.cos(orbit.phi);
  camera.position.set(orbit.r * sp * Math.sin(orbit.theta), orbit.ty + orbit.r * cp, orbit.r * sp * Math.cos(orbit.theta));
  camera.lookAt(0, orbit.ty, 0);
}
const ptrs = new Map();
let moved = 0, pinch0 = 1;
const pinchDist = () => { const p = [...ptrs.values()]; return Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y) || 1; };
canvas.addEventListener("pointerdown", (e) => {
  canvas.setPointerCapture(e.pointerId);
  ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
  moved = 0;
  if (ptrs.size === 2) pinch0 = pinchDist();
});
canvas.addEventListener("pointermove", (e) => {
  const p = ptrs.get(e.pointerId);
  if (!p) return;
  const dx = e.clientX - p.x, dy = e.clientY - p.y;
  p.x = e.clientX; p.y = e.clientY;
  moved += Math.abs(dx) + Math.abs(dy);
  if (ptrs.size === 1) {
    orbit.theta -= dx * 0.006;
    orbit.phi = clamp(orbit.phi - dy * 0.006, 0.15, 1.5);
  } else if (ptrs.size === 2) {
    const d = pinchDist();
    orbit.r = clamp((orbit.r * pinch0) / d, 5, 40);
    pinch0 = d;
  }
});
canvas.addEventListener("pointerup", (e) => {
  if (ptrs.has(e.pointerId)) { if (ptrs.size === 1 && moved < 6) onClick(e); ptrs.delete(e.pointerId); }
});
canvas.addEventListener("pointercancel", (e) => ptrs.delete(e.pointerId));
canvas.addEventListener("wheel", (e) => { e.preventDefault(); orbit.r = clamp(orbit.r * (1 + Math.sign(e.deltaY) * 0.08), 5, 40); }, { passive: false });

function resize() {
  const r = canvas.parentElement.getBoundingClientRect();
  renderer.setSize(r.width, r.height, false);
  camera.aspect = r.width / r.height;
  camera.updateProjectionMatrix();
}
window.addEventListener("resize", resize);

/* ---------------- helpers ---------------- */
function label(text, color, scale) {
  const c = document.createElement("canvas");
  c.width = 256; c.height = 64;
  const tex = new THREE.CanvasTexture(c);
  const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false }));
  s.scale.set(scale || 1.6, (scale || 1.6) / 4, 1);
  s.renderOrder = 10;
  s.userData.draw = (t, col) => {
    const g = c.getContext("2d");
    g.clearRect(0, 0, 256, 64);
    g.fillStyle = "rgba(10,18,32,.85)"; g.fillRect(0, 0, 256, 64);
    g.strokeStyle = col; g.lineWidth = 3; g.strokeRect(2, 2, 252, 60);
    g.fillStyle = col; g.font = "bold 28px 'Segoe UI',Arial,sans-serif"; g.textAlign = "center"; g.textBaseline = "middle";
    g.fillText(t, 128, 34);
    tex.needsUpdate = true;
  };
  s.userData.draw(text, color || "#e7edf8");
  return s;
}

function makeLayer(y, rgb, opacity) {
  const data = new Uint8Array(cols * rows * 4);
  for (let i = 0; i < cols * rows; i++) { data[i * 4] = rgb[0]; data[i * 4 + 1] = rgb[1]; data[i * 4 + 2] = rgb[2]; }
  const tex = new THREE.DataTexture(data, cols, rows, THREE.RGBAFormat);
  tex.magFilter = THREE.LinearFilter; tex.minFilter = THREE.LinearFilter; tex.needsUpdate = true;
  const mat = new THREE.MeshBasicMaterial({ map: tex, transparent: true, opacity, depthWrite: false, side: THREE.DoubleSide });
  const mesh = new THREE.Mesh(new THREE.PlaneGeometry(W, D), mat);
  mesh.rotation.x = -Math.PI / 2;
  mesh.position.y = y;
  mesh.renderOrder = 5;
  root.add(mesh);
  return { data, tex };
}
function setLayer(layer, arr, scale) {
  const d = layer.data;
  for (let r = 0; r < rows; r++) {
    const tr = rows - 1 - r; // texture row 0 is the +z edge
    for (let c = 0; c < cols; c++) d[(tr * cols + c) * 4 + 3] = Math.min(255, arr[r * cols + c] * scale);
  }
  layer.tex.needsUpdate = true;
}

/* ---------------- room build ---------------- */
const exitObjs = {};
const layers = {};
let flames, debrisMesh, floorPick, debrisSig = "";
const dummy = new THREE.Object3D();

function build() {
  const wallMat = new THREE.MeshStandardMaterial({ color: 0x7a8db0, transparent: true, opacity: 0.26, depthWrite: false });
  const edgeMat = new THREE.LineBasicMaterial({ color: 0x9db3d8, transparent: true, opacity: 0.55 });

  const floor = new THREE.Mesh(new THREE.BoxGeometry(W, 0.1, D), new THREE.MeshStandardMaterial({ color: 0x131f36, roughness: 0.9 }));
  floor.position.y = -0.05;
  procedural.add(floor);
  const pts = [];
  for (let x = 0; x <= W + 1e-6; x += 1) pts.push(wx(x), 0.01, wz(0), wx(x), 0.01, wz(D));
  for (let z = 0; z <= D + 1e-6; z += 1) pts.push(wx(0), 0.01, wz(z), wx(W), 0.01, wz(z));
  const gg = new THREE.BufferGeometry();
  gg.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
  procedural.add(new THREE.LineSegments(gg, new THREE.LineBasicMaterial({ color: 0x2a3c5f, transparent: true, opacity: 0.6 })));

  function addWall(x0, z0, x1, z1) {
    const t = 0.2, horiz = Math.abs(z1 - z0) < 1e-6;
    const len = horiz ? Math.abs(x1 - x0) : Math.abs(z1 - z0);
    if (len < 0.05) return;
    const geo = new THREE.BoxGeometry(horiz ? len : t, H, horiz ? t : len);
    const m = new THREE.Mesh(geo, wallMat);
    m.position.set(wx((x0 + x1) / 2), H / 2, wz((z0 + z1) / 2));
    procedural.add(m);
    const e = new THREE.LineSegments(new THREE.EdgesGeometry(geo), edgeMat);
    e.position.copy(m.position);
    procedural.add(e);
  }
  function segments(side) {
    const len = side === "north" || side === "south" ? W : D;
    const gaps = L.exits.filter((e) => e.side === side).map((e) => [e.pos - e.width / 2, e.pos + e.width / 2]).sort((a, b) => a[0] - b[0]);
    const segs = []; let cur = 0;
    gaps.forEach((g) => { if (g[0] > cur) segs.push([cur, g[0]]); cur = g[1]; });
    if (cur < len) segs.push([cur, len]);
    return segs;
  }
  segments("north").forEach(([a, b]) => addWall(a, 0, b, 0));
  segments("south").forEach(([a, b]) => addWall(a, D, b, D));
  segments("west").forEach(([a, b]) => addWall(0, a, 0, b));
  segments("east").forEach(([a, b]) => addWall(W, a, W, b));

  const fcol = { table: 0x3d4f73, desk: 0x46587d, cabinet: 0x52627f, shelf: 0x384868 };
  L.furniture.forEach((f) => {
    const geo = new THREE.BoxGeometry(f.w, f.h, f.d);
    const m = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: fcol[f.type] || 0x46587d, roughness: 0.7 }));
    m.position.set(wx(f.x), f.h / 2, wz(f.z));
    procedural.add(m);
    const e = new THREE.LineSegments(new THREE.EdgesGeometry(geo), new THREE.LineBasicMaterial({ color: 0x8fa6cf, transparent: true, opacity: 0.5 }));
    e.position.copy(m.position);
    procedural.add(e);
  });

  // exits
  L.exits.forEach((ex) => {
    const mat = new THREE.MeshBasicMaterial({ color: 0x2dd4bf, transparent: true, opacity: 0.85 });
    let sx, sz, px, pz;
    if (ex.side === "south") { sx = ex.width; sz = 0.5; px = ex.pos; pz = D - 0.25; }
    else if (ex.side === "north") { sx = ex.width; sz = 0.5; px = ex.pos; pz = 0.25; }
    else if (ex.side === "east") { sx = 0.5; sz = ex.width; px = W - 0.25; pz = ex.pos; }
    else { sx = 0.5; sz = ex.width; px = 0.25; pz = ex.pos; }
    const strip = new THREE.Mesh(new THREE.BoxGeometry(sx, 0.04, sz), mat);
    strip.position.set(wx(px), 0.03, wz(pz));
    root.add(strip);
    const lab = label("Exit " + ex.id, "#2dd4bf", 1.5);
    lab.position.set(wx(px), 2.3, wz(pz));
    root.add(lab);
    exitObjs[ex.id] = { mat, lab, status: "open" };
  });

  // sensors
  const e32 = L.sensors.esp32, pir = L.sensors.pir;
  const node = new THREE.Mesh(new THREE.BoxGeometry(0.28, 0.2, 0.28), new THREE.MeshBasicMaterial({ color: 0x5aa9ff }));
  node.position.set(wx(e32.x), 1.2, wz(e32.z));
  root.add(node);
  const nl = label("ESP32", "#5aa9ff", 1.2);
  nl.position.set(wx(e32.x), 1.65, wz(e32.z));
  root.add(nl);
  const ring = new THREE.Mesh(new THREE.RingGeometry(pir.radius - 0.06, pir.radius, 40), new THREE.MeshBasicMaterial({ color: 0x2dd4bf, transparent: true, opacity: 0.35, side: THREE.DoubleSide }));
  ring.rotation.x = -Math.PI / 2;
  ring.position.set(wx(pir.x), 0.02, wz(pir.z));
  root.add(ring);
  const pl = label("PIR zone", "#2dd4bf", 1.2);
  pl.position.set(wx(pir.x), 0.6, wz(pir.z) - pir.radius);
  root.add(pl);

  // hazard layers
  layers.smoke = [0.6, 1.3, 2.0].map((y) => makeLayer(y, [188, 196, 210], 0.5));
  layers.gas = [0.35, 0.9].map((y) => makeLayer(y, [110, 255, 140], 0.45));
  layers.water = [makeLayer(0.07, [59, 140, 248], 0.8)];
  layers.fire = [makeLayer(0.04, [255, 120, 30], 0.85)];

  flames = new THREE.InstancedMesh(new THREE.ConeGeometry(0.22, 1, 8), new THREE.MeshBasicMaterial({ color: 0xff7a1a, transparent: true, opacity: 0.92 }), cols * rows);
  flames.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  flames.frustumCulled = false;
  root.add(flames);
  debrisMesh = new THREE.InstancedMesh(new THREE.BoxGeometry(0.42, 0.3, 0.42), new THREE.MeshStandardMaterial({ color: 0x6b7280, roughness: 1 }), cols * rows);
  debrisMesh.count = 0;
  debrisMesh.frustumCulled = false;
  root.add(debrisMesh);

  floorPick = new THREE.Mesh(new THREE.PlaneGeometry(W, D), new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false }));
  floorPick.rotation.x = -Math.PI / 2;
  root.add(floorPick);
}

/* ---------------- optional scanned model ---------------- */
const exists = (u) => fetch(u, { method: "HEAD" }).then((r) => r.ok).catch(() => false);
function loadScript(src) { return new Promise((ok, no) => { const s = document.createElement("script"); s.src = src; s.onload = ok; s.onerror = no; document.head.appendChild(s); }); }
async function tryLoadModel() {
  if (!L.model || !(await exists(L.model.url))) return;
  if (!THREE.GLTFLoader) {
    if (!(await exists("/static/vendor/GLTFLoader.js"))) { $("modelTag").textContent = "Model found, add vendor/GLTFLoader.js"; return; }
    await loadScript("/static/vendor/GLTFLoader.js");
  }
  new THREE.GLTFLoader().load(L.model.url, (g) => {
    const m = g.scene, o = L.model.offset || [0, 0, 0];
    m.scale.setScalar(L.model.scale || 1);
    m.position.set(o[0], o[1], o[2]);
    m.rotation.y = L.model.rotation_y || 0;
    root.add(m);
    procedural.visible = false;
    $("modelTag").textContent = "Hybrid: scanned room + synthetic objects";
  });
}

/* ---------------- agents ---------------- */
const agentObjs = new Map();
const COL = { idle: 0x5aa9ff, reacting: 0xf5b942, evacuating: 0xf5b942, stuck: 0xef6461, incapacitated: 0x7f1d1d, evacuated: 0x2dd4bf };
function makeAgent() {
  const g = new THREE.Group();
  const mat = new THREE.MeshStandardMaterial({ color: 0x5aa9ff, roughness: 0.6 });
  const body = new THREE.Mesh(new THREE.CylinderGeometry(0.15, 0.19, 0.85, 14), mat);
  body.position.y = 0.45;
  const head = new THREE.Mesh(new THREE.SphereGeometry(0.14, 14, 12), mat);
  head.position.y = 1.03;
  const ring = new THREE.Mesh(new THREE.RingGeometry(0.28, 0.34, 24), new THREE.MeshBasicMaterial({ color: 0x5aa9ff, transparent: true, opacity: 0.8, side: THREE.DoubleSide }));
  ring.rotation.x = -Math.PI / 2;
  ring.position.y = 0.02;
  g.add(body, head, ring);
  g.userData = { mat, ringMat: ring.material, target: new THREE.Vector3(), body, state: "idle", phase: Math.random() * 6 };
  root.add(g);
  return g;
}
const routeObjs = new Map();

function syncAgents(list) {
  const seen = new Set();
  list.forEach((a) => {
    seen.add(a.id);
    let o = agentObjs.get(a.id);
    const fresh = !o;
    if (fresh) { o = makeAgent(); agentObjs.set(a.id, o); }
    const u = o.userData;
    u.target.set(wx(a.x), 0, wz(a.z));
    if (fresh) o.position.copy(u.target);
    u.state = a.state;
    const col = a.live && a.state !== "incapacitated" ? 0x2dd4bf : COL[a.state] || 0x5aa9ff;
    u.mat.color.setHex(col);
    u.ringMat.color.setHex(col);
    o.visible = a.state !== "evacuated";
    o.rotation.z = a.state === "incapacitated" ? Math.PI / 2 : 0;

    // route line
    let line = routeObjs.get(a.id);
    if (a.path && a.path.length > 1) {
      const pts = a.path.map((p) => new THREE.Vector3(wx(p[0]), 0.09, wz(p[1])));
      if (!line) {
        line = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({ color: 0x4ade80, transparent: true, opacity: 0.85 }));
        line.frustumCulled = false;
        root.add(line);
        routeObjs.set(a.id, line);
      }
      line.geometry.dispose();
      line.geometry = new THREE.BufferGeometry().setFromPoints(pts);
      line.visible = true;
    } else if (line) line.visible = false;
  });
  agentObjs.forEach((o, id) => { if (!seen.has(id)) { root.remove(o); agentObjs.delete(id); } });
  routeObjs.forEach((l, id) => { if (!seen.has(id)) { root.remove(l); routeObjs.delete(id); } });
}

/* ---------------- state -> scene ---------------- */
function applyHazards(hz) {
  layers.smoke.forEach((l) => setLayer(l, hz.smoke, 2.0));
  layers.gas.forEach((l) => setLayer(l, hz.gas, 1.8));
  layers.water.forEach((l) => setLayer(l, hz.water, 2.4));
  layers.fire.forEach((l) => setLayer(l, hz.fire, 2.2));
  const sig = hz.debris.join(",");
  if (sig !== debrisSig) {
    debrisSig = sig;
    hz.debris.forEach((i, k) => {
      const r = Math.floor(i / cols), c = i % cols;
      dummy.position.set(wx((c + 0.5) * cell), 0.15, wz((r + 0.5) * cell));
      dummy.rotation.set(0, i * 1.7, 0);
      dummy.scale.set(0.8 + ((i * 7) % 5) * 0.12, 0.6 + ((i * 3) % 4) * 0.25, 0.8 + ((i * 5) % 5) * 0.1);
      dummy.updateMatrix();
      debrisMesh.setMatrixAt(k, dummy.matrix);
    });
    debrisMesh.count = hz.debris.length;
    debrisMesh.instanceMatrix.needsUpdate = true;
  }
}

function animateFlames(time) {
  if (!last) return;
  const fire = last.hazards.fire;
  let n = 0, sx = 0, sz = 0;
  for (let i = 0; i < fire.length; i++) {
    const f = fire[i] / 100;
    if (f > 0) {
      const r = Math.floor(i / cols), c = i % cols;
      const h = (0.5 + f * 1.3) * (0.82 + 0.3 * Math.sin(time * 9 + i * 1.3));
      dummy.position.set(wx((c + 0.5) * cell), h / 2, wz((r + 0.5) * cell));
      dummy.rotation.set(0, 0, 0);
      dummy.scale.set(1 + 0.2 * Math.sin(time * 7 + i), h, 1 + 0.2 * Math.cos(time * 8 + i));
      sx += (c + 0.5) * cell; sz += (r + 0.5) * cell; n++;
    } else dummy.scale.set(0, 0, 0);
    dummy.updateMatrix();
    flames.setMatrixAt(i, dummy.matrix);
  }
  flames.instanceMatrix.needsUpdate = true;
  if (n) {
    fireLight.position.set(wx(sx / n), 1.2, wz(sz / n));
    fireLight.intensity = Math.min(3, 0.4 + n * 0.12) * (0.85 + 0.3 * Math.random());
  } else fireLight.intensity = 0;
}

let lastT = performance.now();
function frame(now) {
  requestAnimationFrame(frame);
  lastT = now;
  const time = now / 1000;
  agentObjs.forEach((o) => {
    const u = o.userData;
    o.position.lerp(u.target, 0.2);
    if (u.state === "idle") u.body.rotation.z = Math.sin(time * 1.6 + u.phase) * 0.03;
  });
  animateFlames(time);
  const dark = last && last.sensors && last.sensors.connected && Number(last.sensors.ldr) < 1200;
  roomLight.intensity += ((dark ? 1.2 : 0) - roomLight.intensity) * 0.08;
  if (last && last.quake && last.quake.shake) root.position.set((Math.random() - 0.5) * 0.12, 0, (Math.random() - 0.5) * 0.12);
  else root.position.set(0, 0, 0);
  applyCamera();
  renderer.render(scene, camera);
}

/* ---------------- placing a disaster ---------------- */
const ray = new THREE.Raycaster(), ndc = new THREE.Vector2();
function onClick(e) {
  if ((!armed && !placing) || !floorPick) return;
  const r = canvas.getBoundingClientRect();
  ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  ray.setFromCamera(ndc, camera);
  const hit = ray.intersectObject(floorPick)[0];
  if (!hit) return;
  const x = hit.point.x + W / 2, z = hit.point.z + D / 2;
  if (placing) { movePlacing(x, z); return; }
  start(armed, x, z);
  arm(null);
}
function start(kind, x, z) { api.post("/api/disaster", { type: kind, x, z }).then(poll); }
function arm(kind) {
  if (kind && placing) cancelPlacing();
  armed = kind;
  canvas.classList.toggle("armed", !!kind);
  document.querySelectorAll("#disasters button").forEach((b) => b.classList.toggle("on", b.dataset.kind === kind));
  const names = { fire: "Fire", gas: "Gas leak", flood: "Flood", earthquake: "Earthquake" };
  $("hint").className = "hint" + (kind ? " on" : "");
  $("hint").textContent = kind ? "Click the floor to place: " + names[kind] + "  (Esc cancels)" : "Drag to rotate, scroll or pinch to zoom";
}
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { arm(null); cancelPlacing(); }
  if ((e.key === "r" || e.key === "R") && placing && !/INPUT/.test(document.activeElement.tagName)) rotatePlacing();
});

/* ---------------- objects: will it fit? ---------------- */
let objectsRev = -1, placing = null, imgData = null, imgRatio = 0, heightTouched = false, checkTimer = null;
const objMeshes = new Map();
const texLoader = new THREE.TextureLoader();

function boxMesh(o, tint) {
  const geo = new THREE.BoxGeometry(o.w, o.h, o.d);
  const plain = new THREE.MeshStandardMaterial({ color: tint || 0x8b7355, roughness: 0.8, transparent: !!tint, opacity: tint ? 0.65 : 1 });
  let mats = plain;
  if (o.image && !tint) {
    const side = new THREE.MeshStandardMaterial({ map: texLoader.load(o.image), roughness: 0.8 });
    mats = [side, side, plain, plain, side, side];
  }
  const m = new THREE.Mesh(geo, mats);
  m.position.set(wx(o.x), o.h / 2, wz(o.z));
  m.add(new THREE.LineSegments(new THREE.EdgesGeometry(geo), new THREE.LineBasicMaterial({ color: tint || 0xe7c9a0 })));
  return m;
}

function syncObjects(list) {
  objMeshes.forEach((m) => root.remove(m));
  objMeshes.clear();
  list.forEach((o) => {
    const m = boxMesh(o);
    m.visible = !(placing && placing.id === o.id);
    root.add(m);
    objMeshes.set(o.id, m);
  });
  const box = $("objList");
  box.textContent = "";
  list.forEach((o) => {
    const row = document.createElement("div");
    row.className = "obj";
    const info = document.createElement("div");
    const nm = document.createElement("b"); nm.textContent = o.name;
    const sm = document.createElement("small"); sm.textContent = Math.round(o.w * 100) + " x " + Math.round(o.h * 100) + " x " + Math.round(o.d * 100) + " cm";
    info.append(nm, sm);
    const mv = document.createElement("button"); mv.textContent = "Move";
    mv.onclick = () => startPlacing({ id: o.id, name: o.name, x: o.x, z: o.z, w: o.w, d: o.d, h: o.h, image: o.image });
    const rm = document.createElement("button"); rm.textContent = "Remove";
    rm.onclick = () => api.del("/api/object/" + o.id).then(poll);
    const bt = document.createElement("div"); bt.append(mv, rm);
    row.append(info, bt);
    box.appendChild(row);
  });
}

function renderGhost() {
  if (placing.ghost) root.remove(placing.ghost);
  placing.ghost = boxMesh(placing, placing.ok === false ? 0xef6461 : 0x2dd4bf);
  root.add(placing.ghost);
}
function startPlacing(p) {
  cancelPlacing();
  arm(null);
  placing = p;
  const hid = p.id && objMeshes.get(p.id);
  if (hid) hid.visible = false;
  renderGhost();
  $("placeForm").classList.add("hidden");
  $("placeBar").classList.remove("hidden");
  $("hint").className = "hint on";
  $("hint").textContent = "Click the floor to place: " + p.name + "  (R rotates, Esc cancels)";
  canvas.classList.add("armed");
  runCheck();
}
function cancelPlacing() {
  if (!placing) return;
  if (placing.ghost) root.remove(placing.ghost);
  const m = placing.id && objMeshes.get(placing.id);
  if (m) m.visible = true;
  placing = null;
  $("placeForm").classList.remove("hidden");
  $("placeBar").classList.add("hidden");
  canvas.classList.remove("armed");
  arm(null);
}
function movePlacing(x, z) {
  placing.x = clamp(x, 0, W);
  placing.z = clamp(z, 0, D);
  renderGhost();
  clearTimeout(checkTimer);
  checkTimer = setTimeout(runCheck, 120);
}
function rotatePlacing() {
  const t = placing.w; placing.w = placing.d; placing.d = t;
  renderGhost();
  runCheck();
}
async function runCheck() {
  const p = placing;
  if (!p) return;
  const rep = await api.post("/api/object/check", { x: p.x, z: p.z, w: p.w, d: p.d, h: p.h, exclude: p.id || null });
  if (placing !== p || !rep || rep.error) return;
  p.ok = rep.fits;
  renderGhost();
  const box = $("fitReport");
  box.className = "fit " + (rep.fits ? (rep.warnings.length ? "warn" : "ok") : "bad");
  box.textContent = "";
  const h = document.createElement("b");
  h.textContent = rep.fits ? "Fits in the room" : "Does not fit here";
  const ul = document.createElement("ul");
  rep.issues.concat(rep.warnings).forEach((t) => { const li = document.createElement("li"); li.textContent = t; ul.appendChild(li); });
  const ev = document.createElement("li");
  ev.textContent = "Longest escape route: " + rep.evac.longest_before + " m to " + rep.evac.longest_after + " m";
  ul.appendChild(ev);
  box.append(h, ul);
  $("objConfirm").textContent = rep.fits ? "Place" : "Place anyway";
}

$("objImg").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const fr = new FileReader();
  fr.onload = () => {
    const im = new Image();
    im.onload = () => {
      const s = Math.min(1, 256 / Math.max(im.width, im.height));
      const c = document.createElement("canvas");
      c.width = Math.round(im.width * s); c.height = Math.round(im.height * s);
      c.getContext("2d").drawImage(im, 0, 0, c.width, c.height);
      imgData = c.toDataURL("image/jpeg", 0.8);
      imgRatio = im.height / im.width;
      $("objPreview").src = imgData;
      $("objPreview").style.display = "block";
      if (!$("objName").value) $("objName").value = f.name.replace(/\.[^.]+$/, "").slice(0, 40);
      autoHeight();
    };
    im.src = fr.result;
  };
  fr.readAsDataURL(f);
});
function autoHeight() {
  const w = parseFloat($("objW").value);
  if (imgRatio && w > 0 && !heightTouched) $("objH").value = Math.round(w * imgRatio);
}
$("objW").oninput = autoHeight;
$("objH").oninput = () => { heightTouched = true; };
$("objPlace").onclick = () => {
  const w = parseFloat($("objW").value), h = parseFloat($("objH").value), d = parseFloat($("objD").value);
  if (!(w > 0 && h > 0 && d > 0)) { $("hint").className = "hint on"; $("hint").textContent = "Enter width, height and depth in cm first"; return; }
  startPlacing({ id: null, name: $("objName").value.trim() || "Object", x: W / 2, z: D / 2, w: w / 100, d: d / 100, h: h / 100, image: imgData });
};
$("objRotate").onclick = () => placing && rotatePlacing();
$("objCancel").onclick = () => cancelPlacing();
$("objConfirm").onclick = async () => {
  const p = placing;
  if (!p) return;
  await api.post("/api/object", { id: p.id, name: p.name, x: p.x, z: p.z, w: p.w, d: p.d, h: p.h, image: p.image });
  cancelPlacing();
  poll();
};

/* ---------------- HUD ---------------- */
const hist = { t: [], h: [], g: [] };
function setStatus(id, text, cls) { const e = $(id); e.textContent = text; e.className = cls; }
const mmss = (s) => { s = Math.round(s); return String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0"); };

function renderSensors(s) {
  const on = s.connected;
  $("chipSensor").className = "chip " + (on ? "ok" : "off");
  $("chipSensorText").textContent = on ? (s.mode === "demo" ? "Demo sensors" : "ESP32 connected") : (s.mode === "demo" ? "Demo starting" : "ESP32 offline");
  if (!on) return;
  $("vTemp").textContent = Number(s.temperature).toFixed(1);
  Number(s.temperature) <= 35 ? setStatus("sTemp", "normal", "green") : setStatus("sTemp", "high", "red");
  $("vHum").textContent = Number(s.humidity).toFixed(1);
  const hu = Number(s.humidity);
  hu >= 30 && hu <= 70 ? setStatus("sHum", "normal", "green") : setStatus("sHum", "outside range", "yellow");
  $("vPir").textContent = Number(s.pir) === 1 ? "Motion" : "Clear";
  Number(s.pir) === 1 ? setStatus("sPir", "person in PIR zone", "red") : setStatus("sPir", "no motion", "green");
  $("vLdr").textContent = s.ldr;
  Number(s.ldr) < 1200 ? setStatus("sLdr", "dark, room light on", "yellow") : setStatus("sLdr", "bright", "green");
  $("vGas").textContent = s.gas;
  const g = Number(s.gas);
  g < 1200 ? setStatus("sGas", "normal", "green") : g < 2500 ? setStatus("sGas", "warning", "yellow") : setStatus("sGas", "danger", "red");
  hist.t.push(Number(s.temperature)); hist.h.push(hu); hist.g.push(g);
  Object.values(hist).forEach((a) => { if (a.length > 40) a.shift(); });
  drawGraph();
}

const gcv = $("graph");
function drawGraph() {
  const dpr = window.devicePixelRatio || 1, w = gcv.clientWidth, h = gcv.clientHeight;
  gcv.width = w * dpr; gcv.height = h * dpr;
  const g = gcv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  g.strokeStyle = "rgba(255,255,255,.07)";
  for (let y = 20; y < h; y += 28) { g.beginPath(); g.moveTo(0, y); g.lineTo(w, y); g.stroke(); }
  const line = (v, col, max) => {
    if (v.length < 2) return;
    g.strokeStyle = col; g.lineWidth = 2; g.beginPath();
    v.forEach((val, i) => { const x = (i * w) / (v.length - 1), y = h - clamp(val / max, 0, 1) * (h - 12) - 4; i ? g.lineTo(x, y) : g.moveTo(x, y); });
    g.stroke();
  };
  line(hist.t, "#38bdf8", 60); line(hist.h, "#4ade80", 100); line(hist.g, "#f97316", 3500);
}

function renderStatus(s) {
  const danger = s.alarm && (s.stats.incapacitated > 0 || s.exits.every((e) => e.status === "blocked"));
  let cls = "safe", txt = "Safe", chip = "ok";
  if (s.alarm) { cls = danger ? "danger" : "alert"; txt = s.stats.moving || s.stats.stuck ? "Evacuating" : "Emergency"; chip = danger ? "bad" : "warn"; }
  else if (s.sensors && s.sensors.connected && (Number(s.sensors.temperature) > 35 || Number(s.sensors.gas) >= 2500)) { cls = "alert"; txt = "Check environment"; chip = "warn"; }
  $("band").className = "band " + cls;
  $("chipState").className = "chip " + chip;
  $("chipStateText").textContent = txt;
}

function renderEvac(s) {
  const st = s.stats;
  $("stOut").textContent = st.evacuated + "/" + st.total;
  $("stMove").textContent = st.moving;
  $("stStuck").textContent = st.stuck;
  $("stHurt").textContent = st.incapacitated;
  if (st.clear_time !== null) { $("timeLabel").textContent = "Evacuation time"; $("stTime").textContent = mmss(st.clear_time); }
  else if (s.alarm) { $("timeLabel").textContent = "Estimated time left"; $("stTime").textContent = st.stuck && !st.moving ? "no route" : mmss(st.eta); }
  else { $("timeLabel").textContent = "Estimated time left"; $("stTime").textContent = "--"; }

  const box = $("exits");
  const sig = JSON.stringify(s.exits);
  if (box.dataset.sig !== sig) {
    box.dataset.sig = sig;
    box.innerHTML = "";
    s.exits.forEach((ex) => {
      const row = document.createElement("div");
      row.className = "exit";
      const info = document.createElement("div");
      info.innerHTML = "<b>" + ex.name + "</b> <small>exit " + ex.id + " &middot; " + ex.used + " used" + (ex.reason ? " &middot; " + ex.reason : "") + "</small>";
      const tag = document.createElement("span");
      tag.className = "tag " + ex.status;
      tag.textContent = ex.status;
      const btn = document.createElement("button");
      btn.textContent = ex.manual ? "Unblock" : "Block";
      btn.onclick = () => api.post("/api/exit/" + ex.id, { blocked: !ex.manual }).then(poll);
      row.append(info, tag, btn);
      box.appendChild(row);
    });
  }
  s.exits.forEach((ex) => {
    const o = exitObjs[ex.id];
    if (o && o.status !== ex.status) {
      o.status = ex.status;
      const c = ex.status === "open" ? "#2dd4bf" : "#ef6461";
      o.mat.color.set(c);
      o.lab.userData.draw("Exit " + ex.id + (ex.status === "open" ? "" : " blocked"), c);
    }
  });

  const log = $("log");
  const lsig = s.events.map((e) => e.t + e.msg).join("|");
  if (log.dataset.sig !== lsig) {
    log.dataset.sig = lsig;
    log.innerHTML = "";
    s.events.slice().reverse().slice(0, 10).forEach((e) => {
      const li = document.createElement("li");
      li.className = e.level;
      const t = document.createElement("time");
      t.textContent = mmss(e.t);
      li.append(t, document.createTextNode(e.msg));
      log.appendChild(li);
    });
  }
}

function applyState(s) {
  last = s;
  if (s.objects_rev !== objectsRev) { objectsRev = s.objects_rev; api.get("/api/objects").then(syncObjects); }
  applyHazards(s.hazards);
  syncAgents(s.agents);
  renderSensors(s.sensors);
  renderStatus(s);
  renderEvac(s);
  alarmSound(s);
  document.querySelectorAll("#segSpeed button").forEach((b) => b.classList.toggle("on", Number(b.dataset.s) === s.speed));
  document.querySelectorAll("#segMode button").forEach((b) => b.classList.toggle("on", b.dataset.mode === s.sensors.mode));
  $("demoBox").classList.toggle("hidden", s.sensors.mode !== "demo");
  $("auto").checked = s.auto;
  occN = s.stats.total - s.agents.filter((a) => a.live).length;
  $("occN").textContent = occN;
}

let busy = false;
async function poll() {
  if (busy) return;
  busy = true;
  try { applyState(await api.get("/api/state")); }
  catch (e) { $("chipSensor").className = "chip off"; $("chipSensorText").textContent = "Server offline"; }
  busy = false;
}

/* ---------------- controls ---------------- */
document.querySelectorAll("#disasters button").forEach((b) => b.addEventListener("click", () => {
  const k = b.dataset.kind;
  if (k === "earthquake") { arm(null); start("earthquake"); }
  else arm(armed === k ? null : k);
}));
document.querySelectorAll("#segSpeed button").forEach((b) => b.addEventListener("click", () => api.post("/api/speed", { speed: Number(b.dataset.s) }).then(poll)));
document.querySelectorAll("#segMode button").forEach((b) => b.addEventListener("click", () => api.post("/api/sensors/mode", { mode: b.dataset.mode }).then(poll)));
$("spikeGas").onclick = () => api.post("/api/sensors/spike", { kind: "gas" });
$("spikeTemp").onclick = () => api.post("/api/sensors/spike", { kind: "temp" });
$("auto").onchange = (e) => api.post("/api/auto", { enabled: e.target.checked });
$("reset").onclick = () => { arm(null); api.post("/api/reset").then(poll); };
$("occUp").onclick = () => api.post("/api/occupants", { n: Math.min(20, occN + 1) }).then(poll);
$("occDown").onclick = () => api.post("/api/occupants", { n: Math.max(0, occN - 1) }).then(poll);

/* ---------------- scenarios ---------------- */
const SCEN = {
  "Fire in a corner": async () => { await api.post("/api/disaster", { type: "fire", x: W * 0.2, z: D * 0.25 }); },
  "Gas leak at the ESP32": async () => { await api.post("/api/disaster", { type: "gas", x: L.sensors.esp32.x, z: L.sensors.esp32.z }); },
  "Fire, main exit blocked": async () => {
    await api.post("/api/exit/" + L.exits[0].id, { blocked: true });
    await api.post("/api/disaster", { type: "fire", x: W * 0.5, z: D * 0.5 });
  },
  "Earthquake": async () => { await api.post("/api/disaster", { type: "earthquake" }); },
  "Flood from the west wall": async () => { await api.post("/api/disaster", { type: "flood", x: 0.3, z: D / 2 }); },
  "Fire and gas together": async () => {
    await api.post("/api/disaster", { type: "fire", x: W * 0.3, z: D * 0.3 });
    await api.post("/api/disaster", { type: "gas", x: W * 0.7, z: D * 0.6 });
  },
};
Object.keys(SCEN).forEach((k) => { const o = document.createElement("option"); o.textContent = k; $("scenSel").appendChild(o); });
$("scenRun").onclick = async () => { arm(null); cancelPlacing(); await api.post("/api/reset"); await SCEN[$("scenSel").value](); poll(); };

/* ---------------- report (CSV) ---------------- */
function downloadReport() {
  if (!last) return;
  const q = (v) => '"' + String(v).replace(/"/g, '""') + '"';
  const st = last.stats;
  const rows = [
    ["Evacuation report"], ["Generated", new Date().toLocaleString()], ["Room (m)", W + " x " + D + " x " + H],
    ["Disasters", last.active.join(" + ") || "none"], ["Occupants", st.total], ["Evacuated", st.evacuated],
    ["Incapacitated", st.incapacitated], ["Stuck", st.stuck],
    ["Evacuation time (s)", st.clear_time !== null ? st.clear_time : (st.elapsed !== null ? st.elapsed + " (still running)" : "n/a")],
    [], ["Exit", "Status", "Reason", "People used"],
  ];
  last.exits.forEach((e) => rows.push([e.name + " (" + e.id + ")", e.status, e.reason, e.used]));
  rows.push([], ["Occupant", "State", "Exit", "Health"]);
  last.agents.forEach((a) => rows.push([a.live ? "Live person (PIR)" : "Occupant " + a.id, a.state, a.exit || "", a.health]));
  rows.push([], ["Time (s)", "Event"]);
  last.events.forEach((e) => rows.push([e.t, e.msg]));
  const csv = rows.map((r) => r.map(q).join(",")).join("\r\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  a.download = "evacuation_report.csv";
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}
$("report").onclick = downloadReport;

/* ---------------- alarm sound ---------------- */
let actx = null, lastBeep = 0;
function beep() {
  if (!$("sound").checked) return;
  try {
    actx = actx || new (window.AudioContext || window.webkitAudioContext)();
    if (actx.state === "suspended") actx.resume();
    [880, 660].forEach((f, i) => {
      const o = actx.createOscillator(), g = actx.createGain(), t0 = actx.currentTime + i * 0.25;
      o.frequency.value = f; o.connect(g); g.connect(actx.destination);
      g.gain.setValueAtTime(0.08, t0); g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.22);
      o.start(t0); o.stop(t0 + 0.24);
    });
  } catch (e) { /* audio not available */ }
}
function alarmSound(s) {
  const now = performance.now();
  if (s.alarm && s.stats.clear_time === null && now - lastBeep > 2500) { lastBeep = now; beep(); }
}

/* ---------------- room setup ---------------- */
function exitRow(e, i) {
  const row = document.createElement("div");
  row.className = "exrow";
  const id = document.createElement("b"); id.textContent = e.id || String.fromCharCode(65 + i);
  const side = document.createElement("select");
  ["north", "south", "east", "west"].forEach((s) => { const o = document.createElement("option"); o.value = o.textContent = s; if (s === e.side) o.selected = true; side.appendChild(o); });
  const pos = document.createElement("input"); pos.type = "number"; pos.step = "0.1"; pos.value = e.pos; pos.title = "position along the wall (m)";
  const wid = document.createElement("input"); wid.type = "number"; wid.step = "0.1"; wid.value = e.width; wid.title = "door width (m)";
  const rm = document.createElement("button"); rm.textContent = "x"; rm.title = "remove"; rm.onclick = () => row.remove();
  row.append(id, side, pos, wid, rm);
  return row;
}
function fillSetup() {
  $("setW").value = W; $("setD").value = D; $("setH").value = H;
  $("exitRows").textContent = "";
  L.exits.forEach((e, i) => $("exitRows").appendChild(exitRow(e, i)));
  $("e32x").value = L.sensors.esp32.x; $("e32z").value = L.sensors.esp32.z;
  $("pirx").value = L.sensors.pir.x; $("pirz").value = L.sensors.pir.z;
}
$("exitAdd").onclick = () => {
  const n = $("exitRows").children.length;
  if (n < 6) $("exitRows").appendChild(exitRow({ side: "south", pos: W / 2, width: 1.2 }, n));
};
$("setupSave").onclick = async () => {
  const exits = [...$("exitRows").children].map((r, i) => ({
    id: r.children[0].textContent, side: r.children[1].value,
    pos: parseFloat(r.children[2].value), width: parseFloat(r.children[3].value),
  }));
  await api.post("/api/layout", {
    width: parseFloat($("setW").value), depth: parseFloat($("setD").value), height: parseFloat($("setH").value), exits,
    furniture: L.furniture,
    sensors: { esp32: { x: parseFloat($("e32x").value), z: parseFloat($("e32z").value) },
               pir: { x: parseFloat($("pirx").value), z: parseFloat($("pirz").value), radius: L.sensors.pir.radius } },
  });
  location.reload();
};
$("setupDefault").onclick = async () => { await api.post("/api/layout/default"); location.reload(); };

/* ---------------- scanned point cloud (offline, no loader needed) ---------------- */
let pointsObj = null;
async function tryLoadPoints() {
  if (!(await exists("/static/models/room_points.json"))) return;
  const d = await api.get("/static/models/room_points.json");
  const n = d.p.length / 3, pos = new Float32Array(n * 3), col = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) {
    pos[i * 3] = wx(d.p[i * 3] / 100); pos[i * 3 + 1] = d.p[i * 3 + 1] / 100; pos[i * 3 + 2] = wz(d.p[i * 3 + 2] / 100);
    col[i * 3] = d.c[i * 3] / 255; col[i * 3 + 1] = d.c[i * 3 + 1] / 255; col[i * 3 + 2] = d.c[i * 3 + 2] / 255;
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  geo.setAttribute("color", new THREE.BufferAttribute(col, 3));
  pointsObj = new THREE.Points(geo, new THREE.PointsMaterial({ size: 0.035, vertexColors: true, sizeAttenuation: true }));
  pointsObj.frustumCulled = false;
  root.add(pointsObj);
  $("ptsWrap").classList.remove("hidden");
  $("modelTag").textContent = "Real scan points + synthetic objects";
}
$("ptsToggle").onchange = (e) => { if (pointsObj) pointsObj.visible = e.target.checked; };

/* ---------------- scan the real room from this page ---------------- */
let scanFiles = [], recorder = null, recStream = null, recChunks = [], scanTimer = null;
const note = (t) => { $("fileInfo").textContent = t; };
function setFiles(list) {
  scanFiles = [...list];
  const mb = scanFiles.reduce((a, f) => a + f.size, 0) / 1048576;
  note(scanFiles.length ? scanFiles.length + " file(s) selected, " + mb.toFixed(0) + " MB" : "");
}
$("scanFiles").onchange = (e) => setFiles(e.target.files);
$("scanCap").onchange = (e) => setFiles(e.target.files);
/* drag and drop onto the scan box (also stops a file dropped elsewhere from opening in the browser) */
const dz = $("scanDrop");
["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => {
  e.preventDefault();
  dz.classList.add("dragover");
}));
["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => {
  e.preventDefault();
  dz.classList.remove("dragover");
}));
dz.addEventListener("drop", (e) => {
  if (e.dataTransfer && e.dataTransfer.files.length) setFiles(e.dataTransfer.files);
});
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());
try { $("colmapPath").value = localStorage.getItem("colmapPath") || ""; } catch (e) { /* storage blocked */ }

// recording inside the browser needs a secure context: works on this laptop (127.0.0.1), not over plain http from a phone
if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder && window.isSecureContext)) {
  $("recRow").classList.add("hidden");
}
$("recStart").onclick = async () => {
  try { recStream = await navigator.mediaDevices.getUserMedia({ video: { width: 1280, height: 720 }, audio: false }); }
  catch (e) { note("The camera is blocked or in use by another app."); return; }
  const v = $("recVideo");
  v.srcObject = recStream; v.style.display = "block"; v.play();
  recChunks = [];
  const mime = MediaRecorder.isTypeSupported("video/webm;codecs=vp9") ? "video/webm;codecs=vp9" : "video/webm";
  recorder = new MediaRecorder(recStream, { mimeType: mime });
  recorder.ondataavailable = (e) => { if (e.data.size) recChunks.push(e.data); };
  recorder.onstop = () => {
    recStream.getTracks().forEach((t) => t.stop());
    v.style.display = "none"; v.srcObject = null;
    setFiles([new File([new Blob(recChunks, { type: "video/webm" })], "recording.webm", { type: "video/webm" })]);
    $("recStart").disabled = false; $("recStop").disabled = true;
  };
  recorder.start(1000);
  $("recStart").disabled = true; $("recStop").disabled = false;
  note("Recording... carry the camera slowly around the room, then press Stop.");
};
$("recStop").onclick = () => { if (recorder && recorder.state !== "inactive") recorder.stop(); };

function showScan(text, pct, log, err) {
  $("scanBox").classList.remove("hidden");
  const st = $("scanState");
  st.textContent = text; st.className = "scanstate" + (err ? " err" : "");
  $("scanBar").style.width = pct + "%";
  if (log) { const l = $("scanLog"); l.textContent = log.join("\n"); l.scrollTop = l.scrollHeight; }
}
function upload(files) {
  return new Promise((ok, no) => {
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f, f.name));
    const x = new XMLHttpRequest();
    x.open("POST", "/api/scan/upload");
    x.upload.onprogress = (e) => { if (e.lengthComputable) showScan("Uploading " + Math.round((100 * e.loaded) / e.total) + "%", Math.round((5 * e.loaded) / e.total)); };
    x.onload = () => {
      let j = {}; try { j = JSON.parse(x.responseText); } catch (e) { /* not json */ }
      x.status === 200 ? ok(j) : no(new Error(j.error || "Upload failed (" + x.status + ")"));
    };
    x.onerror = () => no(new Error("Upload failed"));
    x.send(fd);
  });
}
function renderScan(s) {
  if (s.state === "idle") return;
  const active = ["extracting", "colmap", "fitting"].includes(s.state);
  showScan(s.message || s.state, s.progress, s.log, s.state === "error");
  $("scanStart").disabled = active;
  $("scanCancel").style.display = active ? "" : "none";
  if (s.state === "done" && sessionStorage.getItem("scanSeen") !== String(s.finished)) {
    sessionStorage.setItem("scanSeen", String(s.finished));
    setTimeout(() => location.reload(), 2000);
  }
}
function pollScan() {
  clearTimeout(scanTimer);
  api.get("/api/scan/status").then((s) => {
    renderScan(s);
    if (["extracting", "colmap", "fitting"].includes(s.state)) scanTimer = setTimeout(pollScan, 1500);
  }).catch(() => { scanTimer = setTimeout(pollScan, 3000); });
}
$("scanStart").onclick = async () => {
  const w = parseFloat($("scanW").value);
  if (!scanFiles.length) { note("Choose or record a video or photos first."); return; }
  if (!(w > 0)) { note("Type the length of the room's longest wall in metres."); return; }
  try { localStorage.setItem("colmapPath", $("colmapPath").value); } catch (e) { /* storage blocked */ }
  $("scanStart").disabled = true;
  try {
    await upload(scanFiles);
    const r = await api.post("/api/scan/start", { room_width: w, colmap: $("colmapPath").value.trim() });
    if (r.error) throw new Error(r.error);
    pollScan();
  } catch (e) {
    showScan(String(e.message || e), 0, null, true);
    $("scanStart").disabled = false;
  }
};
$("scanCancel").onclick = () => api.post("/api/scan/cancel").then(pollScan);

/* ---------------- live 3D reconstruction ---------------- */
const liveGroup = new THREE.Group();
scene.add(liveGroup);
liveGroup.visible = false;
let liveSource = null, liveStream = null, liveTimer = null, liveCloud = null, liveBox = null, liveCamMark = null,
    livePath = null, liveFramed = false, liveHandled = false;
const lcv = document.createElement("canvas");
const liveNote = (t, err) => { $("liveBox").classList.remove("hidden"); const s = $("liveState"); s.textContent = t; s.className = "scanstate" + (err ? " err" : ""); };
try { $("liveColmap").value = localStorage.getItem("colmapPath") || ""; } catch (e) { /* storage blocked */ }

function liveView(on) {
  liveGroup.visible = on;
  root.visible = !on;
  if (!on) { liveFramed = false; }
}
function disposeObj(o) { if (o) { liveGroup.remove(o); o.geometry.dispose(); } }
function drawLive(c) {
  if (!c) return;
  const [Wl, Dl, Hl] = c.dims, n = c.p.length / 3;
  const pos = new Float32Array(n * 3), col = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) {
    pos[i * 3] = c.p[i * 3] / 100 - Wl / 2; pos[i * 3 + 1] = c.p[i * 3 + 1] / 100; pos[i * 3 + 2] = c.p[i * 3 + 2] / 100 - Dl / 2;
    col[i * 3] = c.c[i * 3] / 255; col[i * 3 + 1] = c.c[i * 3 + 1] / 255; col[i * 3 + 2] = c.c[i * 3 + 2] / 255;
  }
  disposeObj(liveCloud);
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  g.setAttribute("color", new THREE.BufferAttribute(col, 3));
  liveCloud = new THREE.Points(g, new THREE.PointsMaterial({ size: 0.04, vertexColors: true }));
  liveCloud.frustumCulled = false;
  liveGroup.add(liveCloud);

  disposeObj(liveBox);
  const bg = new THREE.BoxGeometry(Wl, Hl, Dl);
  liveBox = new THREE.LineSegments(new THREE.EdgesGeometry(bg), new THREE.LineBasicMaterial({ color: 0x2dd4bf, transparent: true, opacity: 0.6 }));
  liveBox.position.y = Hl / 2;
  bg.dispose();
  liveGroup.add(liveBox);

  const cp = [];
  for (let i = 0; i < c.cam.length; i += 3) cp.push(new THREE.Vector3(c.cam[i] / 100 - Wl / 2, c.cam[i + 1] / 100, c.cam[i + 2] / 100 - Dl / 2));
  disposeObj(livePath);
  livePath = new THREE.Line(new THREE.BufferGeometry().setFromPoints(cp), new THREE.LineBasicMaterial({ color: 0xf5b942 }));
  livePath.frustumCulled = false;
  liveGroup.add(livePath);

  if (!liveCamMark) {
    liveCamMark = new THREE.Mesh(new THREE.SphereGeometry(0.12, 12, 10), new THREE.MeshBasicMaterial({ color: 0xf5b942 }));
    liveGroup.add(liveCamMark);
  }
  liveCamMark.position.set(c.now[0] / 100 - Wl / 2, c.now[1] / 100, c.now[2] / 100 - Dl / 2);
  if (!liveFramed) { orbit.r = clamp(Math.max(Wl, Dl) * 1.7, 5, 40); liveFramed = true; }
}

function renderLive(s) {
  const L2 = s.live || {};
  const bar = L2.mode === "video" ? Math.round(100 * (L2.progress || 0)) : Math.min(100, Math.round(s.points / 40));
  $("liveBar").style.width = bar + "%";
  const stateTxt = L2.mode === "video" ? "Reading the video... " + bar + "% - " + s.message : s.message;
  if (L2.mode === "camera" || L2.mode === "video") liveNote(stateTxt, s.state === "lost");
  $("liveStats").textContent = s.frames + " frames, " + s.keyframes + " keyframes, " + s.points + " 3D points";
  $("liveFinish").disabled = !(L2.mode === "camera" && s.points >= 400);
  drawLive(s.cloud);
  if (L2.mode === "error") liveNote(L2.message || "Live reconstruction failed", true);
  if (L2.result && !liveHandled) { liveHandled = true; handleLiveResult(L2.result); }
}
function handleLiveResult(r) {
  stopCamera();
  if (r.error) { liveNote(r.error, true); liveView(false); return; }
  if (r.method === "colmap") { liveNote("COLMAP is building the accurate model from the same footage..."); pollScan(); return; }
  liveNote("Rough result: " + r.width + " x " + r.depth + " m, height " + r.height + " m. " + (r.warning || "") + " Reloading...");
  setTimeout(() => location.reload(), 3500);
}
function pollLive() {
  clearTimeout(liveTimer);
  api.get("/api/live/state").then((s) => {
    renderLive(s);
    if (liveSource && ["camera", "video", "finishing"].includes(s.live.mode)) liveTimer = setTimeout(pollLive, 900);
  }).catch(() => { liveTimer = setTimeout(pollLive, 2000); });
}
function stopCamera() {
  liveSource = liveSource === "camera" ? "done" : liveSource;
  if (liveStream) { liveStream.getTracks().forEach((t) => t.stop()); liveStream = null; }
  $("liveVid").style.display = "none";
  $("liveCam").disabled = false;
}
async function sendLoop() {
  if (liveSource !== "camera") return;
  const t0 = performance.now(), v = $("liveVid");
  if (v.videoWidth) {
    lcv.width = 640; lcv.height = Math.round((640 * v.videoHeight) / v.videoWidth);
    lcv.getContext("2d").drawImage(v, 0, 0, lcv.width, lcv.height);
    const blob = await new Promise((ok) => lcv.toBlob(ok, "image/jpeg", 0.8));
    try { await fetch("/api/live/frame", { method: "POST", headers: { "Content-Type": "image/jpeg" }, body: blob }); } catch (e) { /* retry on next loop */ }
  }
  setTimeout(sendLoop, Math.max(0, 150 - (performance.now() - t0)));
}
function liveWidth() {
  const w = parseFloat($("liveW").value);
  if (!(w > 0)) { liveNote("Type the length of the room's longest wall in metres first.", true); return 0; }
  try { localStorage.setItem("colmapPath", $("liveColmap").value); } catch (e) { /* storage blocked */ }
  return w;
}
$("liveCam").onclick = async () => {
  const w = liveWidth();
  if (!w) return;
  if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.isSecureContext)) {
    liveNote("This browser cannot open the camera here. Use the laptop at http://127.0.0.1:5000, or upload a video.", true); return;
  }
  try { liveStream = await navigator.mediaDevices.getUserMedia({ video: { width: 1280, height: 720, facingMode: "environment" }, audio: false }); }
  catch (e) { liveNote("The camera is blocked or used by another app.", true); return; }
  const r = await api.post("/api/live/start", { room_width: w });
  if (r.error) { liveStream.getTracks().forEach((t) => t.stop()); liveStream = null; liveNote(r.error, true); return; }
  const v = $("liveVid");
  v.srcObject = liveStream; v.style.display = "block"; v.play();
  liveSource = "camera"; liveHandled = false;
  $("liveCam").disabled = true;
  liveView(true);
  liveNote("Starting... move sideways slowly");
  sendLoop();
  pollLive();
};
$("liveFinish").onclick = async () => {
  $("liveFinish").disabled = true;
  stopCamera();
  liveNote("Finishing...");
  const r = await api.post("/api/live/finish", { colmap: $("liveColmap").value.trim() });
  liveHandled = true;
  handleLiveResult(r);
};
$("liveStop").onclick = async () => {
  stopCamera(); liveSource = null;
  await api.post("/api/live/stop");
  liveView(false);
  $("liveBox").classList.add("hidden");
};
$("liveFile").onchange = (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const w = liveWidth();
  if (!w) { e.target.value = ""; return; }
  const fd = new FormData();
  fd.append("files", f, f.name); fd.append("room_width", w); fd.append("colmap", $("liveColmap").value.trim());
  const x = new XMLHttpRequest();
  x.open("POST", "/api/live/video");
  x.upload.onprogress = (ev) => { if (ev.lengthComputable) { liveNote("Uploading " + Math.round((100 * ev.loaded) / ev.total) + "%"); $("liveBar").style.width = Math.round((100 * ev.loaded) / ev.total) + "%"; } };
  x.onload = () => {
    let j = {}; try { j = JSON.parse(x.responseText); } catch (er) { /* not json */ }
    if (x.status !== 200) { liveNote(j.error || "Upload failed", true); return; }
    liveSource = "video"; liveHandled = false;
    liveView(true);
    liveNote("Reading the video...");
    pollLive();
  };
  x.onerror = () => liveNote("Upload failed", true);
  x.send(fd);
};

/* ---------------- boot ---------------- */
(async function init() {
  try { L = await api.get("/api/layout"); }
  catch (e) { $("chipSensorText").textContent = "Server offline"; return; }
  W = L.width; D = L.depth; H = L.height; cols = L.cols; rows = L.rows; cell = L.cell;
  build();
  fillSetup();
  tryLoadPoints();
  pollScan();
  arm(null);
  resize();
  requestAnimationFrame(frame);
  tryLoadModel();
  poll();
  setInterval(poll, 400);
})();
})();