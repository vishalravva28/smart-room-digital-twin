"""
Simulation engine for the Smart Room Digital Twin.

Pure Python, no dependencies. Contains:
  - LAYOUT      : the room (meters) - edit this to match your real room / scan
  - Grid        : walkable grid built from the layout
  - Hazards     : fire, smoke, gas, flood, earthquake debris
  - World       : occupants, evacuation routing (multi-source Dijkstra), events
"""
import copy
import heapq
import json
import math
import os
import random
import threading
import time

INF = float("inf")

# ------------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------------
GAS_DANGER = 2500        # ESP32 gas reading that auto-starts a gas emergency
FIRE_TEMP = 50.0         # deg C that auto-starts a fire emergency
WALK_SPEED = 1.3         # m/s
SIM_STEP = 0.25          # seconds per physics step

# All sizes in meters. Origin (0,0) is the north-west corner, x -> east, z -> south.
LAYOUT = {
    "width": 12.0,
    "depth": 8.0,
    "height": 3.0,
    "cell": 0.5,
    "exits": [
        {"id": "A", "name": "Main door", "side": "south", "pos": 2.0, "width": 1.4},
        {"id": "B", "name": "Side door", "side": "east", "pos": 5.0, "width": 1.4},
    ],
    "furniture": [
        {"id": "table1", "type": "table", "x": 4.0, "z": 2.5, "w": 2.4, "d": 1.0, "h": 0.75},
        {"id": "table2", "type": "table", "x": 4.0, "z": 5.5, "w": 2.4, "d": 1.0, "h": 0.75},
        {"id": "cabinet", "type": "cabinet", "x": 11.5, "z": 3.0, "w": 0.8, "d": 1.6, "h": 1.8},
        {"id": "shelf", "type": "shelf", "x": 7.0, "z": 0.4, "w": 3.0, "d": 0.6, "h": 1.8},
        {"id": "desk", "type": "desk", "x": 8.5, "z": 6.5, "w": 1.6, "d": 0.8, "h": 0.75},
    ],
    "sensors": {
        "esp32": {"x": 10.6, "z": 0.9},
        "pir": {"x": 9.0, "z": 3.2, "radius": 1.4},
    },
    # Drop a scanned model at static/models/room.glb and tune these to align it.
    "model": {"url": "/static/models/room.glb", "scale": 1.0, "offset": [0, 0, 0], "rotation_y": 0.0},
}


LAYOUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "models", "room_layout.json")


def _r5(v):
    return math.ceil(v / 0.5 - 1e-9) * 0.5


def _num(v, lo, hi, default):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def sanitize_layout(raw):
    """Merge a user/scan layout over the default one and keep it physically valid."""
    raw = raw if isinstance(raw, dict) else {}
    L = copy.deepcopy(LAYOUT)
    L["width"] = _r5(_num(raw.get("width"), 2, 40, L["width"]))
    L["depth"] = _r5(_num(raw.get("depth"), 2, 40, L["depth"]))
    L["height"] = _num(raw.get("height"), 2, 6, L["height"])
    W, D = L["width"], L["depth"]

    src = raw["exits"] if isinstance(raw.get("exits"), list) and raw["exits"] else L["exits"]
    exits, used = [], set()
    for i, e in enumerate(src[:6]):
        side = e.get("side")
        if side not in ("north", "south", "east", "west"):
            continue
        length = W if side in ("north", "south") else D
        wd = _num(e.get("width"), 0.6, min(3.0, length - 0.6), 1.4)
        pos = _num(e.get("pos"), wd / 2 + 0.1, length - wd / 2 - 0.1, length / 2)
        eid = str(e.get("id") or "").strip()[:3]
        if not eid or eid in used:
            eid = next(chr(65 + k) for k in range(26) if chr(65 + k) not in used)
        used.add(eid)
        exits.append({"id": eid, "name": str(e.get("name") or "Exit %s" % eid)[:30],
                      "side": side, "pos": pos, "width": wd})
    L["exits"] = exits or [{"id": "A", "name": "Main door", "side": "south", "pos": W / 2, "width": 1.4}]

    furn = raw["furniture"] if isinstance(raw.get("furniture"), list) else L["furniture"]
    L["furniture"] = [f for f in furn
                      if isinstance(f, dict) and all(k in f for k in ("x", "z", "w", "d", "h"))
                      and 0 < f["x"] - f["w"] / 2 and f["x"] + f["w"] / 2 < W
                      and 0 < f["z"] - f["d"] / 2 and f["z"] + f["d"] / 2 < D]

    sens = raw.get("sensors") if isinstance(raw.get("sensors"), dict) else {}
    e32, pir = sens.get("esp32", {}), sens.get("pir", {})
    d32, dpir = L["sensors"]["esp32"], L["sensors"]["pir"]
    L["sensors"] = {
        "esp32": {"x": _num(e32.get("x", min(d32["x"], W - 0.6)), 0.3, W - 0.3, W - 1.4),
                  "z": _num(e32.get("z", min(d32["z"], D - 0.6)), 0.3, D - 0.3, 0.9)},
        "pir": {"x": _num(pir.get("x", min(dpir["x"], W - 1.6)), 0.3, W - 0.3, W * 0.75),
                "z": _num(pir.get("z", min(dpir["z"], D - 1.6)), 0.3, D - 0.3, D * 0.4),
                "radius": _num(pir.get("radius", dpir["radius"]), 0.5, 4.0, 1.4)},
    }
    if isinstance(raw.get("model"), dict):
        L["model"].update(raw["model"])
    return L


def load_layout():
    try:
        with open(LAYOUT_PATH, "r", encoding="utf-8") as f:
            return sanitize_layout(json.load(f))
    except (OSError, ValueError):
        return sanitize_layout({})


def save_layout(L):
    os.makedirs(os.path.dirname(LAYOUT_PATH), exist_ok=True)
    keep = {k: L[k] for k in ("width", "depth", "height", "exits", "furniture", "sensors", "model")}
    with open(LAYOUT_PATH, "w", encoding="utf-8") as f:
        json.dump(keep, f, indent=2)


# ------------------------------------------------------------------
# GRID
# ------------------------------------------------------------------
class Grid:
    def __init__(self, layout):
        self.layout = layout
        self.cell = layout["cell"]
        self.cols = int(round(layout["width"] / self.cell))
        self.rows = int(round(layout["depth"] / self.cell))
        self.n = self.cols * self.rows

        self.furniture = [False] * self.n
        for f in layout["furniture"]:
            hw, hd = f["w"] / 2 + 0.15, f["d"] / 2 + 0.15
            for i in range(self.n):
                x, z = self.center(i)
                if abs(x - f["x"]) <= hw and abs(z - f["z"]) <= hd:
                    self.furniture[i] = True

        # exits -> gate cells (cells just inside the opening)
        self.exits = {}
        self.gates = set()
        for ex in layout["exits"]:
            half = ex["width"] / 2
            cells = []
            for i in range(self.n):
                r, c = divmod(i, self.cols)
                x, z = self.center(i)
                s = ex["side"]
                if s == "south" and r == self.rows - 1 and abs(x - ex["pos"]) <= half:
                    cells.append(i)
                elif s == "north" and r == 0 and abs(x - ex["pos"]) <= half:
                    cells.append(i)
                elif s == "east" and c == self.cols - 1 and abs(z - ex["pos"]) <= half:
                    cells.append(i)
                elif s == "west" and c == 0 and abs(z - ex["pos"]) <= half:
                    cells.append(i)
            for i in cells:
                self.furniture[i] = False
                self.gates.add(i)
            self.exits[ex["id"]] = {"id": ex["id"], "name": ex["name"], "gates": cells}

        self.base_furniture = list(self.furniture)

        # adjacency
        self.n4 = [[] for _ in range(self.n)]
        self.n8 = [[] for _ in range(self.n)]
        self.adj = [[] for _ in range(self.n)]
        for i in range(self.n):
            r, c = divmod(i, self.cols)
            for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                rr, cc = r + dr, c + dc
                if 0 <= rr < self.rows and 0 <= cc < self.cols:
                    j = rr * self.cols + cc
                    self.n4[i].append(j)
                    self.n8[i].append(j)
                    self.adj[i].append((j, 1.0, None, None))
            for dr, dc in ((-1, -1), (-1, 1), (1, -1), (1, 1)):
                rr, cc = r + dr, c + dc
                if 0 <= rr < self.rows and 0 <= cc < self.cols:
                    j = rr * self.cols + cc
                    self.n8[i].append(j)
                    self.adj[i].append((j, 1.4142, r * self.cols + cc, rr * self.cols + c))

    def mask_with(self, rects):
        """Blocked mask = fixed furniture + user-placed objects (rects: x,z,w,d in m)."""
        f = list(self.base_furniture)
        for r in rects:
            hw, hd = r["w"] / 2 + 0.15, r["d"] / 2 + 0.15
            for i in range(self.n):
                x, z = self.center(i)
                if abs(x - r["x"]) <= hw and abs(z - r["z"]) <= hd:
                    f[i] = True
        return f

    def apply_extras(self, rects):
        self.furniture = self.mask_with(rects)

    def center(self, i):
        r, c = divmod(i, self.cols)
        return ((c + 0.5) * self.cell, (r + 0.5) * self.cell)

    def cell_of(self, x, z):
        c = min(self.cols - 1, max(0, int(x / self.cell)))
        r = min(self.rows - 1, max(0, int(z / self.cell)))
        return r * self.cols + c

    def compute_field(self, blocked, mult, open_exits):
        """Multi-source Dijkstra from every open exit gate.
        dist[i] = weighted distance to the nearest exit, nxt[i] = next cell on the way."""
        dist = [INF] * self.n
        nxt = [-1] * self.n
        exit_of = [None] * self.n
        pq = []
        for eid in open_exits:
            for g in self.exits[eid]["gates"]:
                if not blocked[g]:
                    dist[g] = 0.0
                    exit_of[g] = eid
                    heapq.heappush(pq, (0.0, g))
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u]:
                continue
            for v, step, a, b in self.adj[u]:
                if blocked[v]:
                    continue
                if a is not None and (blocked[a] or blocked[b]):
                    continue  # no cutting corners
                nd = d + step * (mult[u] + mult[v]) * 0.5 * self.cell
                if nd < dist[v]:
                    dist[v] = nd
                    nxt[v] = u
                    exit_of[v] = exit_of[u]
                    heapq.heappush(pq, (nd, v))
        return dist, nxt, exit_of


# ------------------------------------------------------------------
# HAZARDS
# ------------------------------------------------------------------
class Hazards:
    def __init__(self, grid):
        n = grid.n
        self.fire = [0.0] * n
        self.smoke = [0.0] * n
        self.gas = [0.0] * n
        self.water = [0.0] * n
        self.debris = [False] * n
        self.gas_src = []
        self.flood_on = False
        self.flood_level = 0.0
        self.flood_x = 0.0
        self.shake_until = -1.0
        self.quake_mag = 0.0

    @staticmethod
    def _diffuse(v, g, k, leak, decay):
        out = [0.0] * g.n
        for i in range(g.n):
            nb = g.n4[i]
            avg = sum(v[j] for j in nb) / len(nb)
            val = v[i] + k * (avg - v[i])
            if i in g.gates:
                val *= 1.0 - leak
            val *= 1.0 - decay
            out[i] = 0.0 if val < 0.002 else min(1.0, val)
        return out

    def step(self, dt, g):
        # fire grows and spreads; smoke comes from fire
        for i in [i for i in range(g.n) if self.fire[i] > 0]:
            if self.water[i] > 0.5:
                self.fire[i] = 0.0
                continue
            f = min(1.0, self.fire[i] + 0.12 * dt)
            self.fire[i] = f
            self.smoke[i] = min(1.0, self.smoke[i] + 0.6 * dt * f)
            for j in g.n4[i]:
                if self.fire[j] == 0 and not self.debris[j] and self.water[j] < 0.5:
                    p = 0.05 * dt * f * (2.5 if g.furniture[j] else 1.0)
                    if random.random() < p:
                        self.fire[j] = 0.3
        # gas leaks
        for i in self.gas_src:
            self.gas[i] = min(1.0, self.gas[i] + 0.5 * dt)
        # flood rises from one side
        if self.flood_on:
            self.flood_level = min(2.0, self.flood_level + 0.03 * dt)
            for i in range(g.n):
                x, _ = g.center(i)
                self.water[i] = max(0.0, min(1.0, self.flood_level - 0.12 * abs(x - self.flood_x)))
        self.smoke = self._diffuse(self.smoke, g, 0.25 * dt, 0.20 * dt, 0.004 * dt)
        self.gas = self._diffuse(self.gas, g, 0.35 * dt, 0.12 * dt, 0.002 * dt)

    def blocked(self, g):
        return [
            g.furniture[i] or self.debris[i] or self.fire[i] > 0.2 or self.water[i] >= 0.8
            for i in range(g.n)
        ]

    def cost_mult(self, g):
        m = [1.0 + 3.0 * self.smoke[i] + 3.0 * self.gas[i] + 2.0 * self.water[i] for i in range(g.n)]
        for i in range(g.n):
            if self.fire[i] > 0:
                for j in g.n8[i]:
                    m[j] += 2.5
        return m


# ------------------------------------------------------------------
# AGENTS + WORLD
# ------------------------------------------------------------------
class Agent:
    __slots__ = ("id", "x", "z", "state", "health", "delay", "speed_mul", "live", "exit")

    def __init__(self, aid, x, z, live=False):
        self.id = str(aid)
        self.x, self.z = x, z
        self.state = "idle"       # idle | reacting | evacuating | stuck | evacuated | incapacitated
        self.health = 100.0
        self.delay = 0.0
        self.speed_mul = random.uniform(0.85, 1.15)
        self.live = live          # True = person detected by the real PIR sensor
        self.exit = None


class World:
    def __init__(self, layout=None):
        self.layout = layout or load_layout()
        self.grid = Grid(self.layout)
        self.lock = threading.RLock()
        self.speed = 1.0
        self.auto = True
        self.n_occ = 6
        self.sensors = {}
        self.last_pir = 0.0
        self.objects = []        # user-placed objects (fit check)
        self.objects_rev = 0
        self._oid = 0
        self.reset()

    # ---------- lifecycle ----------
    def reset(self):
        with self.lock:
            self.h = Hazards(self.grid)
            self.agents = []
            self.t = 0.0
            self.alarm = False
            self.alarm_t = None
            self.clear_t = None
            self.events = []
            self.active = []
            self.manual_block = {eid: False for eid in self.grid.exits}
            self.exit_status = {eid: ("open", "") for eid in self.grid.exits}
            self.exit_usage = {eid: 0 for eid in self.grid.exits}
            self.dist = self.nxt = self.exit_of = None
            self.grid.apply_extras(self.objects)
            self._spawn(self.n_occ)
            self.log("Twin reset - %d occupants" % self.n_occ)

    def log(self, msg, level="info"):
        self.events.append({"t": round(self.t, 1), "msg": msg, "level": level})
        del self.events[:-60]

    def _spawn(self, n):
        g = self.grid
        free = [i for i in range(g.n) if not g.furniture[i] and i not in g.gates
                and (g.center(i)[0] > 1 and g.center(i)[0] < self.layout["width"] - 1)
                and (g.center(i)[1] > 1 and g.center(i)[1] < self.layout["depth"] - 1)]
        random.shuffle(free)
        placed = []
        for i in free:
            if len(placed) >= n:
                break
            x, z = g.center(i)
            if all(math.hypot(x - px, z - pz) > 1.2 for px, pz in placed):
                placed.append((x, z))
        for k, (x, z) in enumerate(placed, 1):
            self.agents.append(Agent(k, x, z))

    def set_occupants(self, n):
        with self.lock:
            self.n_occ = max(0, min(20, int(n)))
            self.reset()

    # ---------- disasters ----------
    def start_disaster(self, kind, x=None, z=None, source="user"):
        with self.lock:
            g, h = self.grid, self.h
            W, D = self.layout["width"], self.layout["depth"]
            x = W / 2 if x is None else max(0.1, min(W - 0.1, float(x)))
            z = D / 2 if z is None else max(0.1, min(D - 0.1, float(z)))
            who = " (sensor-triggered)" if source == "sensor" else ""
            if kind == "fire":
                h.fire[g.cell_of(x, z)] = 0.35
                self.log("Fire started at (%.1f, %.1f)%s" % (x, z, who), "danger")
            elif kind == "gas":
                h.gas_src.append(g.cell_of(x, z))
                self.log("Gas leak started at (%.1f, %.1f)%s" % (x, z, who), "danger")
            elif kind == "flood":
                h.flood_on = True
                h.flood_x = x
                self.log("Flood started near x=%.1f m" % x, "danger")
            elif kind == "earthquake":
                self._earthquake()
            else:
                return False
            if kind not in self.active:
                self.active.append(kind)
            self._raise_alarm()
            return True

    def _earthquake(self):
        g, h = self.grid, self.h
        mag = round(random.uniform(5.0, 6.8), 1)
        h.quake_mag = mag
        h.shake_until = self.t + 6.0
        near_gate = set()
        for gc in g.gates:
            near_gate.add(gc)
            near_gate.update(g.n8[gc])
        free = [i for i in range(g.n) if not g.furniture[i] and i not in near_gate]
        k = int(8 + (mag - 5.0) * 6)
        for i in random.sample(free, min(k, len(free))):
            h.debris[i] = True
        eid = random.choice(list(g.exits))
        gates = list(g.exits[eid]["gates"])
        random.shuffle(gates)
        cnt = max(1, int(len(gates) * 0.6))
        for i in gates[:cnt]:
            h.debris[i] = True
        for a in self.agents:
            if h.debris[g.cell_of(a.x, a.z)]:
                a.health -= 60
        self.log("Earthquake M%.1f - %d debris zones, exit %s partly blocked" % (mag, k, eid), "danger")

    def _raise_alarm(self):
        if not self.alarm:
            self.alarm = True
            self.alarm_t = self.t
            self.clear_t = None
            self.log("Alarm raised - occupants evacuating", "warn")
        for a in self.agents:
            if a.state == "idle":
                a.state = "reacting"
                a.delay = random.uniform(1.0, 4.0)

    def block_exit(self, eid, blocked):
        with self.lock:
            if eid in self.manual_block:
                self.manual_block[eid] = bool(blocked)

    # ---------- sensors ----------
    def update_sensors(self, d):
        with self.lock:
            self.sensors = dict(d)
            now = time.time()
            live = next((a for a in self.agents if a.live), None)
            if int(d.get("pir", 0)) == 1:
                self.last_pir = now
                if live is None:
                    p = self.layout["sensors"]["pir"]
                    live = Agent("P", p["x"] + random.uniform(-0.4, 0.4), p["z"] + random.uniform(-0.4, 0.4), live=True)
                    if self.alarm:
                        live.state, live.delay = "reacting", 1.0
                    self.agents.append(live)
                    self.log("Person detected by PIR sensor")
            elif live is not None and not self.alarm and now - self.last_pir > 6:
                self.agents.remove(live)
                self.log("PIR area clear")
            if self.auto and d.get("connected"):
                e = self.layout["sensors"]["esp32"]
                if float(d.get("gas", 0)) >= GAS_DANGER and "gas" not in self.active:
                    self.start_disaster("gas", e["x"], e["z"], source="sensor")
                elif float(d.get("temperature", 0)) >= FIRE_TEMP and "fire" not in self.active:
                    self.start_disaster("fire", e["x"], e["z"], source="sensor")

    # ---------- simulation ----------
    def advance(self, real_dt):
        with self.lock:
            remaining = min(2.0, max(0.0, real_dt * self.speed))
            while remaining > 1e-6:
                h = min(SIM_STEP, remaining)
                self._step(h)
                remaining -= h

    def _route(self, cell):
        out, seen = [cell], 0
        while self.dist[cell] > 0 and self.nxt[cell] >= 0 and seen < 200:
            cell = self.nxt[cell]
            out.append(cell)
            seen += 1
        return out

    def _step(self, dt):
        g, h = self.grid, self.h
        self.t += dt
        h.step(dt, g)
        blocked = h.blocked(g)
        for eid, ex in g.exits.items():
            if self.manual_block[eid]:
                for c in ex["gates"]:
                    blocked[c] = True

        # exit status (+ events when it changes)
        open_exits = []
        for eid, ex in g.exits.items():
            if self.manual_block[eid]:
                st = ("blocked", "closed by operator")
            else:
                bad = [c for c in ex["gates"] if blocked[c]]
                if len(bad) == len(ex["gates"]):
                    c = bad[0]
                    reason = ("obstructed" if g.furniture[c] else "fire" if h.fire[c] > 0.2
                              else "debris" if h.debris[c] else "flood")
                    st = ("blocked", reason)
                else:
                    st = ("open", "")
            if st != self.exit_status[eid]:
                self.exit_status[eid] = st
                if st[0] == "blocked":
                    self.log("Exit %s blocked (%s)" % (eid, st[1]), "warn")
                elif self.alarm:
                    self.log("Exit %s is open again" % eid)
            if st[0] == "open":
                open_exits.append(eid)

        if not self.alarm:
            return
        mult = h.cost_mult(g)
        self.dist, self.nxt, self.exit_of = g.compute_field(blocked, mult, open_exits)
        for a in self.agents:
            self._step_agent(a, dt, blocked)

        if self.clear_t is None and self.agents and not any(
            a.state in ("idle", "reacting", "evacuating", "stuck") for a in self.agents
        ):
            self.clear_t = self.t - self.alarm_t
            self.log("Evacuation finished in %.0f s" % self.clear_t, "ok")

    def _step_agent(self, a, dt, blocked):
        if a.state in ("evacuated", "incapacitated"):
            return
        g, h = self.grid, self.h
        cur = g.cell_of(a.x, a.z)

        dmg = 0.0
        if h.fire[cur] > 0.2:
            dmg += 35
        if h.smoke[cur] > 0.45:
            dmg += 4 * h.smoke[cur]
        if h.gas[cur] > 0.5:
            dmg += 6 * h.gas[cur]
        if h.water[cur] > 0.8:
            dmg += 2
        a.health -= dmg * dt
        if a.health <= 0:
            a.state = "incapacitated"
            self.log("Occupant %s incapacitated" % a.id, "danger")
            return

        if a.state == "idle":
            return
        if a.state == "reacting":
            a.delay -= dt
            if a.delay > 0:
                return
            a.state = "evacuating"

        factor = max(0.25, 1.0 - 0.5 * h.smoke[cur] - 0.4 * min(1.0, h.gas[cur]) - 0.5 * min(1.0, h.water[cur]))
        budget = WALK_SPEED * a.speed_mul * factor * dt

        for _ in range(8):
            cur = g.cell_of(a.x, a.z)
            if self.dist[cur] == 0.0:
                a.state = "evacuated"
                a.exit = self.exit_of[cur]
                self.exit_usage[a.exit] += 1
                self.log("Occupant %s left via exit %s" % (a.id, a.exit), "ok")
                return
            nx = self.nxt[cur]
            if self.dist[cur] == INF:
                # standing in a cell that just became blocked: step out to the best neighbour
                best = None
                for j in g.n8[cur]:
                    if not blocked[j] and self.dist[j] < INF and (best is None or self.dist[j] < self.dist[best]):
                        best = j
                if best is None:
                    a.state = "stuck"
                    return
                nx = best
            a.state = "evacuating"
            a.exit = self.exit_of[nx] if nx >= 0 else None
            tx, tz = g.center(nx)
            dx, dz = tx - a.x, tz - a.z
            d = math.hypot(dx, dz)
            if d <= budget:
                a.x, a.z = tx, tz
                budget -= d
            else:
                a.x += dx / d * budget
                a.z += dz / d * budget
                return

    # ---------- output ----------
    def state(self):
        with self.lock:
            g, h = self.grid, self.h
            agents, etas = [], []
            for a in self.agents:
                path = []
                if self.alarm and a.state in ("reacting", "evacuating") and self.dist is not None:
                    cur = g.cell_of(a.x, a.z)
                    if self.dist[cur] < INF:
                        cells = self._route(cur)
                        path = [[round(a.x, 2), round(a.z, 2)]] + [
                            [round(g.center(c)[0], 2), round(g.center(c)[1], 2)] for c in cells[1:60]
                        ]
                        length = sum(math.hypot(path[k + 1][0] - path[k][0], path[k + 1][1] - path[k][1])
                                     for k in range(len(path) - 1))
                        etas.append(max(0.0, a.delay) + length / WALK_SPEED)
                agents.append({
                    "id": a.id, "x": round(a.x, 2), "z": round(a.z, 2), "state": a.state,
                    "health": int(max(0, a.health)), "live": a.live, "exit": a.exit, "path": path,
                })
            count = lambda *s: sum(1 for a in self.agents if a.state in s)
            q = lambda arr, k=100: [int(v * k) for v in arr]
            return {
                "t": round(self.t, 1),
                "alarm": self.alarm,
                "speed": self.speed,
                "auto": self.auto,
                "active": list(self.active),
                "objects_rev": self.objects_rev,
                "cols": g.cols, "rows": g.rows,
                "hazards": {
                    "fire": q(h.fire), "smoke": q(h.smoke), "gas": q(h.gas), "water": q(h.water),
                    "debris": [i for i, v in enumerate(h.debris) if v],
                },
                "quake": {"shake": self.t < h.shake_until, "mag": h.quake_mag},
                "agents": agents,
                "exits": [
                    {"id": eid, "name": ex["name"], "status": self.exit_status[eid][0],
                     "reason": self.exit_status[eid][1], "manual": self.manual_block[eid],
                     "used": self.exit_usage[eid]}
                    for eid, ex in g.exits.items()
                ],
                "stats": {
                    "total": len(self.agents),
                    "evacuated": count("evacuated"),
                    "moving": count("reacting", "evacuating"),
                    "stuck": count("stuck"),
                    "incapacitated": count("incapacitated"),
                    "eta": round(max(etas), 1) if etas else 0,
                    "elapsed": round(self.t - self.alarm_t, 1) if self.alarm and self.clear_t is None else None,
                    "clear_time": round(self.clear_t, 1) if self.clear_t is not None else None,
                },
                "events": self.events[-12:],
            }


# ------------------------------------------------------------------
# OBJECT FIT CHECK (added methods)
# ------------------------------------------------------------------
def _ov(ax, az, aw, ad, bx, bz, bw, bd):
    return abs(ax - bx) < (aw + bw) / 2 and abs(az - bz) < (ad + bd) / 2


def _reach(self, rects):
    g = self.grid
    blocked = g.mask_with(rects)
    dist, _, _ = g.compute_field(blocked, [1.0] * g.n, list(g.exits))
    return blocked, dist


def check_fit(self, x, z, w, d, h, exclude=None):
    """Can a w x d x h (m) object stand at (x, z)? Also: does it hurt evacuation?"""
    with self.lock:
        L, g = self.layout, self.grid
        W, D = L["width"], L["depth"]
        issues, warnings = [], []
        if x - w / 2 < 0.05 or x + w / 2 > W - 0.05 or z - d / 2 < 0.05 or z + d / 2 > D - 0.05:
            issues.append("Sticks out through a wall")
        if h > L["height"]:
            issues.append("Taller than the ceiling (%.2f m)" % L["height"])
        for f in L["furniture"]:
            if _ov(x, z, w, d, f["x"], f["z"], f["w"], f["d"]):
                issues.append("Overlaps the %s" % f["id"])
        others = [o for o in self.objects if o["id"] != exclude]
        for o in others:
            if _ov(x, z, w, d, o["x"], o["z"], o["w"], o["d"]):
                issues.append("Overlaps %s" % o["name"])
        for ex in L["exits"]:
            p, wd = ex["pos"], ex["width"] + 0.4
            zone = {"south": (p, D - 0.6, wd, 1.2), "north": (p, 0.6, wd, 1.2),
                    "east": (W - 0.6, p, 1.2, wd), "west": (0.6, p, 1.2, wd)}[ex["side"]]
            if _ov(x, z, w, d, *zone):
                issues.append("Blocks exit %s" % ex["id"])

        new = {"x": x, "z": z, "w": w, "d": d}
        b_blk, b_d = _reach(self, others)
        a_blk, a_d = _reach(self, others + [new])
        cut = sum(1 for i in range(g.n)
                  if not b_blk[i] and b_d[i] < INF and not a_blk[i] and a_d[i] == INF)
        lb = max([v for v in b_d if v < INF] or [0.0])
        la = max([v for v in a_d if v < INF] or [0.0])
        if cut:
            issues.append("Cuts off %d floor cells (%.1f m2) from every exit" % (cut, cut * g.cell * g.cell))
        elif la - lb > 1.0:
            warnings.append("Longest escape route grows by %.1f m" % (la - lb))
        return {
            "fits": not issues, "issues": issues, "warnings": warnings,
            "evac": {"longest_before": round(lb, 1), "longest_after": round(la, 1), "cut_off_cells": cut},
        }


def save_object(self, data):
    with self.lock:
        L = self.layout

        def num(k, lo, hi):
            return max(lo, min(hi, float(data[k])))

        x, z = num("x", 0, L["width"]), num("z", 0, L["depth"])
        w, d = num("w", 0.05, 6.0), num("d", 0.05, 6.0)
        o = next((o for o in self.objects if o["id"] == data.get("id")), None)
        if o:
            o.update(x=x, z=z, w=w, d=d)
            verb = "moved"
        else:
            self._oid += 1
            img = data.get("image")
            img = img if isinstance(img, str) and img.startswith("data:image/") and len(img) < 250000 else None
            o = {"id": "o%d" % self._oid, "name": str(data.get("name") or "Object")[:40],
                 "x": x, "z": z, "w": w, "d": d, "h": num("h", 0.05, 6.0), "image": img}
            self.objects.append(o)
            verb = "placed"
        self.grid.apply_extras(self.objects)
        self.objects_rev += 1
        self.log("Object '%s' %s" % (o["name"], verb))
        return o["id"]


def remove_object(self, oid):
    with self.lock:
        before = len(self.objects)
        self.objects = [o for o in self.objects if o["id"] != oid]
        if len(self.objects) != before:
            self.grid.apply_extras(self.objects)
            self.objects_rev += 1
            self.log("Object removed")


World.check_fit = check_fit
World.save_object = save_object
World.remove_object = remove_object


def set_layout(self, raw, persist=True):
    with self.lock:
        L = sanitize_layout(raw)
        self.layout = L
        self.grid = Grid(L)
        self.objects = []
        self.objects_rev += 1
        if persist:
            save_layout(L)
        self.reset()
        self.log("Room set to %.1f x %.1f m with %d exit(s)" % (L["width"], L["depth"], len(L["exits"])))
        return L


def restore_default_layout(self):
    try:
        os.remove(LAYOUT_PATH)
    except OSError:
        pass
    return self.set_layout({}, persist=False)


World.set_layout = set_layout
World.restore_default_layout = restore_default_layout
