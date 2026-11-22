"""Mech Royale bot: map-aware duelist. Survives the zone, wins 1v1s with lead aim, retreats to cover to regen."""
import heapq
import math
import os
import random
import sys
import time
from array import array

import mechfight as mf

TAU = 2 * math.pi
NAME = "steelwit"


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def hdist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


# ---------------------------------------------------------------- map model
class World:
    G = 2.0  # fine grid (m) for buildings / LOS
    N = 4.0  # nav grid (m)

    def __init__(self, mapdir):
        import json
        with open(os.path.join(mapdir, "v2", "world.json")) as f:
            w = json.load(f)
        t = w["terrain"]
        self.tx0, self.ty0, self.tstep = t["minX"], t["minY"], t["step"]
        self.tnx, self.tny = t["nx"], t["ny"]
        self.tscale = t.get("scale", 0.01)
        h = array("h")
        with open(os.path.join(mapdir, "v2", t.get("file", "terrain.i16")), "rb") as f:
            h.frombytes(f.read())
        if sys.byteorder != "little":
            h.byteswap()
        self.th = h
        b = w["bounds"]
        self.minX, self.maxX, self.minY, self.maxY = b["minX"], b["maxX"], b["minY"], b["maxY"]
        G = self.G
        self.gx0, self.gy0 = self.minX, self.minY
        self.gnx = int((self.maxX - self.minX) / G) + 2
        self.gny = int((self.maxY - self.minY) / G) + 2
        n = self.gnx * self.gny
        self.btop = array("f", [-999.0]) * n
        self.bbase = array("f", [999.0]) * n
        self.rtop = array("f", [-999.0]) * n   # bridge top
        self.rbase = array("f", [999.0]) * n   # bridge base
        for p in w["prisms"]:
            self._raster(p)
        self._build_nav()

    # terrain height, nearest vertex (fast)
    def ground(self, x, y):
        i = int((x - self.tx0) / self.tstep + 0.5)
        j = int((y - self.ty0) / self.tstep + 0.5)
        if i < 0: i = 0
        elif i >= self.tnx: i = self.tnx - 1
        if j < 0: j = 0
        elif j >= self.tny: j = self.tny - 1
        return self.th[j * self.tnx + i] * self.tscale

    def cell(self, x, y):
        i = int((x - self.gx0) / self.G)
        j = int((y - self.gy0) / self.G)
        if i < 0 or j < 0 or i >= self.gnx or j >= self.gny:
            return -1
        return j * self.gnx + i

    def _raster(self, p):
        r = p["ring"]
        rings = [r] + list(p.get("holes") or [])
        edges = []
        ys = []
        for rg in rings:
            m = len(rg) // 2
            for k in range(m):
                x1, y1 = rg[2 * k], rg[2 * k + 1]
                x2, y2 = rg[2 * ((k + 1) % m)], rg[2 * ((k + 1) % m) + 1]
                edges.append((x1, y1, x2, y2))
            ys.extend(rg[1::2])
        if not ys:
            return
        G = self.G
        bridge = p.get("kind") == "bridge"
        top, base = p["top"], p["base"]
        j0 = max(0, int((min(ys) - self.gy0) / G) - 1)
        j1 = min(self.gny - 1, int((max(ys) - self.gy0) / G) + 1)
        tops = self.rtop if bridge else self.btop
        bases = self.rbase if bridge else self.bbase
        for j in range(j0, j1 + 1):
            yc = self.gy0 + (j + 0.5) * G
            xs = []
            for x1, y1, x2, y2 in edges:
                if (y1 <= yc < y2) or (y2 <= yc < y1):
                    xs.append(x1 + (yc - y1) * (x2 - x1) / (y2 - y1))
            if len(xs) < 2:
                continue
            xs.sort()
            for k in range(0, len(xs) - 1, 2):
                ia = max(0, math.ceil((xs[k] - self.gx0) / G - 0.5))
                ib = min(self.gnx - 1, math.floor((xs[k + 1] - self.gx0) / G - 0.5))
                row = j * self.gnx
                for i in range(ia, ib + 1):
                    c = row + i
                    if top > tops[c]: tops[c] = top
                    if base < bases[c]: bases[c] = base

    def solid_top(self, x, y):
        """Height a mech would stand on at (x,y): max of ground and building tops (ignoring overhangs)."""
        c = self.cell(x, y)
        g = self.ground(x, y)
        if c < 0:
            return g
        t = self.btop[c]
        if t > g and self.bbase[c] < g + 1.5:
            return t
        return g

    def los(self, ax, ay, az, bx, by, bz, step=2.0):
        dx, dy, dz = bx - ax, by - ay, bz - az
        d = math.sqrt(dx * dx + dy * dy)
        n = int(d / step)
        if n < 2:
            return True
        btop, bbase, rtop, rbase = self.btop, self.bbase, self.rtop, self.rbase
        gx0, gy0, G, gnx, gny = self.gx0, self.gy0, self.G, self.gnx, self.gny
        tx0, ty0, ts, tnx, tny, th, sc = self.tx0, self.ty0, self.tstep, self.tnx, self.tny, self.th, self.tscale
        inv = 1.0 / n
        for k in range(1, n):
            f = k * inv
            x = ax + dx * f
            y = ay + dy * f
            z = az + dz * f
            i = int((x - gx0) / G)
            j = int((y - gy0) / G)
            if 0 <= i < gnx and 0 <= j < gny:
                c = j * gnx + i
                if bbase[c] <= z <= btop[c] or rbase[c] <= z <= rtop[c]:
                    return False
            ti = int((x - tx0) / ts + 0.5)
            tj = int((y - ty0) / ts + 0.5)
            if 0 <= ti < tnx and 0 <= tj < tny and th[tj * tnx + ti] * sc > z + 0.2:
                return False
        return True

    # ---------- nav grid
    def _build_nav(self):
        N = self.N
        self.nnx = int((self.maxX - self.minX) / N) + 1
        self.nny = int((self.maxY - self.minY) / N) + 1
        blk = bytearray(self.nnx * self.nny)
        r = int(N / self.G)
        for j in range(self.nny):
            for i in range(self.nnx):
                bad = False
                for sj in range(r):
                    for si in range(r):
                        x = self.minX + i * N + (si + 0.5) * self.G
                        y = self.minY + j * N + (sj + 0.5) * self.G
                        c = self.cell(x, y)
                        if c < 0:
                            bad = True
                            break
                        g = self.ground(x, y)
                        if self.btop[c] > g + 1.2 and self.bbase[c] < g + 9.5:
                            bad = True
                            break
                    if bad:
                        break
                if bad:
                    blk[j * self.nnx + i] = 1
        # map edge margin
        for j in range(self.nny):
            for i in range(self.nnx):
                if i < 2 or j < 2 or i >= self.nnx - 2 or j >= self.nny - 2:
                    blk[j * self.nnx + i] = 1
        self.blk = blk
        near = bytearray(len(blk))
        nx = self.nnx
        for c in range(len(blk)):
            if blk[c]:
                continue
            i, j = c % nx, c // nx
            for dj in (-1, 0, 1):
                for di in (-1, 0, 1):
                    ii, jj = i + di, j + dj
                    if 0 <= ii < nx and 0 <= jj < self.nny and blk[jj * nx + ii]:
                        near[c] = 1
        self.near = near

    def ncell(self, x, y):
        i = int((x - self.minX) / self.N)
        j = int((y - self.minY) / self.N)
        i = min(max(i, 0), self.nnx - 1)
        j = min(max(j, 0), self.nny - 1)
        return j * self.nnx + i

    def ncenter(self, c):
        return (self.minX + (c % self.nnx + 0.5) * self.N, self.minY + (c // self.nnx + 0.5) * self.N)

    def free_near(self, c, rad=6):
        if not self.blk[c]:
            return c
        nx = self.nnx
        i0, j0 = c % nx, c // nx
        best, bd = -1, 1e9
        for dj in range(-rad, rad + 1):
            for di in range(-rad, rad + 1):
                i, j = i0 + di, j0 + dj
                if 0 <= i < nx and 0 <= j < self.nny:
                    cc = j * nx + i
                    if not self.blk[cc]:
                        d = di * di + dj * dj
                        if d < bd:
                            bd, best = d, cc
        return best

    def astar(self, sx, sy, gx, gy, cap=5000):
        s = self.ncell(sx, sy)
        g = self.free_near(self.ncell(gx, gy))
        if g < 0:
            return None
        if self.blk[s]:
            s2 = self.free_near(s, 3)
            if s2 < 0:
                return None
            s = s2
        nx, blk, near = self.nnx, self.blk, self.near
        gi, gj = g % nx, g // nx
        D2 = 1.4142
        openh = [(0.0, s)]
        gs = {s: 0.0}
        came = {s: -1}
        exp = 0
        best, bh = s, 1e18
        while openh:
            _, c = heapq.heappop(openh)
            if c == g:
                best = g
                break
            exp += 1
            if exp > cap:
                break
            i, j = c % nx, c // nx
            gc = gs[c]
            for di, dj, w in ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                              (1, 1, D2), (1, -1, D2), (-1, 1, D2), (-1, -1, D2)):
                ii, jj = i + di, j + dj
                if ii < 0 or jj < 0 or ii >= nx or jj >= self.nny:
                    continue
                cc = jj * nx + ii
                if blk[cc]:
                    continue
                if di and dj and (blk[j * nx + ii] or blk[jj * nx + i]):
                    continue
                ng = gc + w + (0.6 if near[cc] else 0.0)
                if ng < gs.get(cc, 1e18):
                    gs[cc] = ng
                    came[cc] = c
                    ax, ay = abs(ii - gi), abs(jj - gj)
                    hh = (max(ax, ay) + 0.4142 * min(ax, ay))
                    if hh < bh:
                        bh, best = hh, cc
                    heapq.heappush(openh, (ng + 1.3 * hh, cc))
        path = []
        c = best
        while c != -1:
            path.append(self.ncenter(c))
            c = came[c]
        path.reverse()
        return path


# ---------------------------------------------------------------- bot
UPGRADE_PRIORITY = ["armor", "plating", "gun_damage", "gun_velocity", "laser_damage", "gun_rate", "nanites",
                    "laser_charge", "gun_mag", "servos", "lightning_efficiency", "lightning_range", "gyros",
                    "capacitor", "reactor", "jets", "sensors", "mortar_reload", "lightning_chain",
                    "mortar_velocity", "mortar_splash", "laser_pierce"]
WEAPON_RANK = {"laser": 3, "lightning": 2, "mortar": 1, "gun": 0}


class Bot:
    name = NAME

    def __init__(self, hello):
        t0 = time.perf_counter()
        self.cfg = hello["config"]
        self.slot = hello.get("slot", 0)
        random.seed(hello.get("seed", 0))
        self.world = None
        try:
            self.world = World(hello["map"]["dir"])
        except Exception as e:  # never die: fall back to map-less play
            mf.log("map load failed", repr(e))
        mf.log(f"init {time.perf_counter() - t0:.2f}s")
        self.path = None
        self.path_goal = None
        self.path_t = -99
        self.stuck_t = 0.0
        self.unstick_until = -1
        self.unstick_dir = None
        self.jink_t = 0
        self.jink_sign = 1
        self.items = {}  # id -> (kind, pos, t)
        self.crates = {}
        self.threat_bearing = None
        self.threat_t = -99
        self.last_dmg_t = -99
        self.attackers = {}  # id -> t
        self.cover = None
        self.cover_t = -99
        self.last_enemy = None  # (x, y, z, t)
        self.mode = "roam"
        self.scan_phase = 0.0
        self.roam_goal = None
        self.roam_t = -99
        self.npc_target = None
        self.last_pos = None
        self.taken_fail = {}

    # ---------- helpers
    def eye(self, m, scale=1.0):
        return m["pos"][2] + 8.0 * scale

    def los_mech(self, me, mz_off, tgt):
        if not self.world:
            return True
        cz = tgt["pos"][2] + (4.5 if tgt.get("kind") == "mech" else 1.8)
        return self.world.los(me[0], me[1], me[2] + mz_off, tgt["pos"][0], tgt["pos"][1], cz)

    def zone_goal(self, obs):
        z = obs["zone"]
        return z["next_center"], z["next_radius"]

    def zone_ok(self, p, obs, margin=12.0):
        c, r = self.zone_goal(obs)
        return hdist(p, c) < max(r - margin, r * 0.5)

    def navigate(self, obs, goal, now):
        """Return a unit-ish move vector toward goal using the cached A* path."""
        me = obs["self"]["pos"]
        d = hdist(me, goal)
        if d < 1.5:
            return [0.0, 0.0]
        direct = [(goal[0] - me[0]) / d, (goal[1] - me[1]) / d]
        W = self.world
        if not W:
            return direct
        need = (self.path is None or self.path_goal is None or hdist(self.path_goal, goal) > 8
                or now - self.path_t > 3.0)
        if need and self.budget_left() > 0.020:
            self.path = W.astar(me[0], me[1], goal[0], goal[1])
            self.path_goal = goal
            self.path_t = now
        p = self.path
        if not p:
            return direct
        # drop reached waypoints
        while len(p) > 1 and hdist(me, p[0]) < 4.5:
            p.pop(0)
        # look ahead: farthest of the next 4 waypoints still in a clear line (cheap check: nav grid)
        tgt = p[0]
        for k in range(min(4, len(p)) - 1, 0, -1):
            if self.clear_line(me, p[k]):
                tgt = p[k]
                break
        if len(p) <= 1:
            tgt = goal if self.clear_line(me, goal) else p[0]
        dx, dy = tgt[0] - me[0], tgt[1] - me[1]
        dd = math.hypot(dx, dy) or 1
        return [dx / dd, dy / dd]

    def clear_line(self, a, b):
        W = self.world
        d = hdist(a, b)
        n = int(d / 2.0) + 1
        for k in range(1, n + 1):
            f = k / n
            c = W.ncell(a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
            if W.blk[c]:
                return False
        return True

    def budget_left(self):
        return 0.042 - (time.perf_counter() - self.t_start)

    # ---------- aiming
    def lead(self, me_pos, muzzle_h, tgt, speed, delay=0.075, k=0.8):
        p, v = tgt["pos"], tgt.get("vel", [0, 0, 0])
        v = [v[0] * k, v[1] * k, v[2] if len(v) > 2 else 0.0]
        scale = 1 + 0.05 * tgt.get("cubes", 0) if tgt.get("kind") == "mech" else 1.0
        cz = 4.5 * scale if tgt.get("kind") == "mech" else 1.75
        ox, oy, oz = me_pos[0], me_pos[1], me_pos[2] + muzzle_h
        t = 0.0
        for _ in range(3):
            px, py, pz = p[0] + v[0] * t, p[1] + v[1] * t, p[2] + cz + v[2] * t * 0.5
            dist = math.sqrt((px - ox) ** 2 + (py - oy) ** 2 + (pz - oz) ** 2)
            t = dist / speed + delay
        yaw = math.atan2(py - oy, px - ox)
        pitch = math.atan2(pz - oz, math.hypot(px - ox, py - oy))
        return yaw, pitch, dist

    def mortar_aim(self, me_pos, tgt, wst):
        v = wst.get("speed", 72)
        g = wst.get("gravity", 22)
        p, tv = tgt["pos"], tgt.get("vel", [0, 0, 0])
        oz = me_pos[2] + 9.5
        t = 1.5
        for _ in range(3):
            px, py = p[0] + tv[0] * t, p[1] + tv[1] * t
            R = math.hypot(px - me_pos[0], py - me_pos[1])
            h = p[2] + 2.0 - oz
            # low-arc solution of R tan(a) - g R^2/(2 v^2 cos^2 a) = h
            a_ = g * R * R / (2 * v * v)
            disc = R * R - 4 * a_ * (a_ + h)
            if disc < 0:
                return None
            tanp = (R - math.sqrt(disc)) / (2 * a_) if a_ > 1e-6 else h / max(R, 1)
            pitch = math.atan(tanp)
            t = R / (v * math.cos(pitch))
        return math.atan2(py - me_pos[1], px - me_pos[0]), pitch, R

    # ---------- main
    def act(self, obs):
        self.t_start = time.perf_counter()
        now = obs["t"]
        me = obs["self"]
        pos = me["pos"]
        hp, maxhp = me["hp"], me["max_hp"]
        hpf = hp / max(maxhp, 1)
        st = me.get("stats", {})
        muzzle = st.get("muzzle_height", 7.0)
        turn = st.get("turn_rate", 4.0)
        act = {"tick": obs["tick"]}
        self.recovering = False

        # ---- upgrades
        off = obs.get("offer")
        if off and off.get("choices"):
            ids = [c["id"] for c in off["choices"]]
            held = {w["kind"] for w in me.get("weapons", []) if w}
            pri = UPGRADE_PRIORITY
            best = min(range(len(ids)), key=lambda i: pri.index(ids[i]) if ids[i] in pri else 99)
            act["pick"] = best

        # ---- events
        for ev in obs.get("events", []):
            et = ev.get("type")
            if et == "damaged":
                if ev.get("by") is not None:
                    self.last_dmg_t = now
                    self.attackers[ev["by"]] = now
                    if ev.get("bearing") is not None:
                        self.threat_bearing = ev["bearing"]
                        self.threat_t = now
            elif et == "take_failed":
                self.taken_fail[ev.get("id")] = now
                self.items.pop(ev.get("id"), None)
            elif et == "took":
                self.items.pop(ev.get("id"), None)

        # ---- item memory
        for p in obs.get("pickups", []):
            self.items[p["id"]] = (p["kind"], p["pos"], now)
        vis_ids = {p["id"] for p in obs.get("pickups", [])}
        for iid in list(self.items):
            k, ip, t = self.items[iid]
            if iid not in vis_ids and (hdist(ip, pos) < 25 or now - t > 40):
                del self.items[iid]
        for c in obs.get("crates", []):
            self.crates[c["id"]] = (c["pos"], c["hp"], now)
        vis_c = {c["id"] for c in obs.get("crates", [])}
        for cid in list(self.crates):
            cp, _, t = self.crates[cid]
            if cid not in vis_c and (hdist(cp, pos) < 30 or now - t > 40):
                del self.crates[cid]

        weapons = me.get("weapons", [None, None])
        gun = weapons[0] if weapons and weapons[0] and weapons[0]["kind"] == "gun" else None
        w1 = weapons[1] if len(weapons) > 1 else None
        if w1 is None and weapons and weapons[0] and weapons[0]["kind"] != "gun":
            w1 = weapons[0]
        kinds = {w["kind"] for w in weapons if w}

        # ---- take pickups in reach
        reach = self.cfg.get("loot", {}).get("pickup_radius", 4.0) - 0.3
        slot1 = weapons[1] if len(weapons) > 1 else None
        for p in obs.get("pickups", []):
            if hdist(p["pos"], pos) < reach and abs(p["pos"][2] - pos[2]) < 3.8 and p["kind"] not in kinds:
                if now - self.taken_fail.get(p["id"], -99) < 2:
                    continue
                if slot1 is None:
                    act["take"] = p["id"]
                    break
                if WEAPON_RANK.get(p["kind"], 0) > WEAPON_RANK.get(slot1["kind"], 0) and not slot1.get("charge"):
                    act["take"] = p["id"]
                    act["drop"] = 1
                    break

        # ---- enemies
        mechs = [m for m in obs.get("mechs", []) if m.get("kind") == "mech" and m.get("hp", 1) > 0]
        npcs = [m for m in obs.get("mechs", []) if m.get("kind") == "npc"]
        eye_off = st.get("eye_height", 8.0)
        for m in mechs:
            m["_d"] = hdist(m["pos"], pos)
            m["_los"] = self.los_mech(pos, eye_off, m) if m["_d"] < 260 else False
            # is it pointing at us?
            yaw_to_me = math.atan2(pos[1] - m["pos"][1], pos[0] - m["pos"][0])
            m["_aimed"] = abs(wrap(m.get("aim", [0, 0])[0] - yaw_to_me)) < 0.25
        visible = [m for m in mechs if m["_los"]]
        if mechs:
            c0 = min(mechs, key=lambda m: m["_d"])
            self.last_enemy = (c0["pos"][0], c0["pos"][1], c0["pos"][2], now)
        threats = [m for m in mechs if m["_d"] < 160 and (m["_los"] or m["_aimed"])]
        recent_attackers = [k for k, t in self.attackers.items() if now - t < 3]
        n_threat = max(len([m for m in threats if m["_aimed"] or m["id"] in recent_attackers]), len(recent_attackers))

        # target selection: visible, prefer low hp and close
        target = None
        if visible:
            target = min(visible, key=lambda m: m["_d"] / 60.0 + m.get("hp", 1400) / 500.0 - (1.5 if m["id"] in recent_attackers else 0))
        elif mechs:
            target = min(mechs, key=lambda m: m["_d"])

        # ---- decide mode
        in_zone = me.get("in_zone", True)
        zc, zr = self.zone_goal(obs)
        z = obs["zone"]
        tgt_hpf = (target.get("hp", 1400) / max(target.get("max_hp", 1400), 1)) if target else 1
        retreat = False
        if target is not None:
            if hpf < 0.30 and tgt_hpf > hpf + 0.15:
                retreat = True
            if n_threat >= 2 and hpf < 0.65:
                retreat = True
            if n_threat >= 3:
                retreat = True
        mode = "roam"
        if target is not None and (target["_los"] or target["_d"] < 120):
            mode = "retreat" if retreat else "fight"
        self.mode = mode

        # ---- movement
        move = [0.0, 0.0]
        boost = False
        jump = False
        aim_yaw = me["aim"][0]
        aim_pitch = 0.0
        fire0 = False
        fire1 = False

        # zone pressure
        dz_center = hdist(pos, zc)
        cur_c, cur_r = z["center"], z["radius"]
        if now < z["shrink_start"]:
            urgency_r = zr
        else:
            urgency_r = zr
        outside_next = dz_center > max(zr - 10.0, zr * 0.6)
        time_to_shrink = z["shrink_end"] - now
        need_dist = dz_center - (zr - 10.0)
        walk = st.get("walk_speed", 11.7)
        zone_urgent = (not in_zone) or (outside_next and need_dist / (walk * 0.8) > time_to_shrink - 15)

        if mode in ("fight", "retreat") and not zone_urgent:
            tp = target["pos"]
            to_t = math.atan2(tp[1] - pos[1], tp[0] - pos[0])
            d = target["_d"]
            if mode == "fight":
                has_l = "lightning" in kinds and me["energy"] > 25 and n_threat <= 1 and d < 70
                want = 26.0 if has_l else (150.0 if ("laser" in kinds) else 140.0)
                radial = 0.0
                if has_l:
                    if d > want + 12:
                        radial = 2.0
                    elif d < want - 12:
                        radial = -0.7
                elif d > want + 25:
                    radial = 0.8
                elif d < want - 45:
                    radial = -2.2
                elif d < want - 12:
                    radial = -1.0
                if now > self.jink_t:
                    self.jink_t = now + random.uniform(0.4, 1.0)
                    self.jink_sign = random.choice((-1, 1))
                perp = to_t + self.jink_sign * math.pi / 2
                if not target["_los"]:
                    if has_l:
                        move = self.navigate(obs, tp, now)
                    elif d < 100:
                        # out of sight and close: open the range, don't walk into its gun
                        k_ = 60.0 / max(d, 1.0)
                        away = (pos[0] + (pos[0] - tp[0]) * k_, pos[1] + (pos[1] - tp[1]) * k_)
                        move = self.navigate(obs, away, now)
                    else:
                        move = [math.cos(perp) * 0.6, math.sin(perp) * 0.6]
                else:
                    mx = math.cos(perp) + radial * math.cos(to_t)
                    my = math.sin(perp) + radial * math.sin(to_t)
                    n = math.hypot(mx, my) or 1
                    move = [mx / n, my / n]
                if has_l:
                    boost = d > 40 and me["energy"] > 45 and target["_los"]
                else:
                    boost = d < want - 45 and me["energy"] > 25
            else:  # retreat to cover
                cov = self.find_cover(obs, pos, [m["pos"] for m in mechs if m["_d"] < 200], now)
                if cov is not None:
                    move = self.navigate(obs, cov, now)
                else:
                    away = to_t + math.pi + self.jink_sign * 0.5
                    move = [math.cos(away), math.sin(away)]
                    if now > self.jink_t:
                        self.jink_t = now + random.uniform(0.6, 1.2)
                        self.jink_sign = -self.jink_sign
                boost = me["energy"] > 20
            # keep inside zone while fighting
            if outside_next and dz_center > zr * 0.9:
                zx, zy = (zc[0] - pos[0]) / dz_center, (zc[1] - pos[1]) / dz_center
                move = [move[0] * 0.5 + zx, move[1] * 0.5 + zy]
        else:
            goal = None
            if zone_urgent or outside_next:
                # go to a point inside next circle on our side
                if dz_center > 1:
                    k = max(zr * 0.55, 0) / dz_center
                    goal = (zc[0] + (pos[0] - zc[0]) * k, zc[1] + (pos[1] - zc[1]) * k)
                else:
                    goal = tuple(zc)
                boost = (not in_zone) or (need_dist / walk > time_to_shrink - 5)
            if goal is None or (not zone_urgent and in_zone):
                loot_goal = self.loot_goal(obs, pos, kinds, slot1, hpf, now)
                if hpf < 0.75 and loot_goal is not None and hdist(loot_goal, pos) > 45:
                    loot_goal = None
                if loot_goal is not None and (goal is None or self.zone_ok(loot_goal, obs, 5)):
                    goal = loot_goal
            recovering = False
            if goal is None and hpf < 0.75 and not zone_urgent and not visible:
                recovering = True
                cube = min(obs.get("cubes", []), key=lambda c: hdist(c["pos"], pos), default=None)
                if cube is not None and hdist(cube["pos"], pos) < 40:
                    goal = (cube["pos"][0], cube["pos"][1])
                else:
                    le = self.last_enemy
                    if le is not None and now - le[3] < 60:
                        cov = self.find_cover(obs, pos, [le], now)
                        if cov is not None and hdist(cov, pos) > 3:
                            goal = cov
                    if goal is None:
                        goal = (pos[0], pos[1])
            self.recovering = recovering
            if goal is None:
                # roam: stay well inside next circle
                if self.roam_goal is None or now > self.roam_t or not self.zone_ok(self.roam_goal, obs, 20):
                    ang = random.uniform(-math.pi, math.pi)
                    rr = random.uniform(0, max(zr * 0.45, 5))
                    self.roam_goal = (zc[0] + math.cos(ang) * rr, zc[1] + math.sin(ang) * rr)
                    self.roam_t = now + 20
                goal = self.roam_goal
                if hdist(pos, goal) < 8:
                    self.roam_t = now
            move = self.navigate(obs, goal, now)
            if not boost and me["energy"] > 90 and hdist(pos, goal) > 60:
                boost = True

        # ---- stuck handling
        spd = math.hypot(me["vel"][0], me["vel"][1])
        wants = math.hypot(move[0], move[1]) > 0.3
        if wants and spd < 2.0 and me.get("grounded", True):
            self.stuck_t += 0.1
        else:
            self.stuck_t = max(0.0, self.stuck_t - 0.05)
        if self.stuck_t > 0.6 and now > self.unstick_until:
            self.unstick_until = now + 1.2
            self.unstick_dir = random.choice((-1, 1))
            self.path = None
        if now < self.unstick_until:
            jump = me.get("grounded", False)
            boost = me["energy"] > 10
            if self.stuck_t > 2.0:
                a = math.atan2(move[1], move[0]) + self.unstick_dir * math.pi / 2
                move = [math.cos(a), math.sin(a)]
        if not me.get("grounded", True) and now < self.unstick_until:
            boost = me["energy"] > 5

        # ---- aiming & firing
        aim_target = None
        if target is not None and target["_los"] and target["_d"] < 260:
            aim_target = target
        npc_t = None
        crate_t = None
        if aim_target is None:
            # shoot NPC drones / crates when calm and healthy
            if npcs and hpf > 0.75 and not mechs:
                cands = [n for n in npcs if hdist(n["pos"], pos) < 90 and self.los_mech(pos, muzzle, n)]
                if cands:
                    npc_t = min(cands, key=lambda n: hdist(n["pos"], pos))
            if npc_t is None and not mechs and hpf > 0.7:
                cands = [c for c in obs.get("crates", []) if hdist(c["pos"], pos) < 80]
                for c in sorted(cands, key=lambda c: hdist(c["pos"], pos)):
                    if self.world is None or self.world.los(pos[0], pos[1], pos[2] + muzzle, c["pos"][0], c["pos"][1], c["pos"][2] + 1.2):
                        crate_t = c
                        break
        shoot = aim_target or npc_t
        if shoot is None and crate_t is not None:
            shoot = {"pos": crate_t["pos"], "vel": [0, 0, 0], "kind": "crate"}

        if shoot is not None:
            gspeed = gun["stats"].get("speed", 252) if gun else 252
            yaw, pitch, dist = self.lead(pos, muzzle, shoot, gspeed)
            if shoot.get("kind") == "crate":
                pitch = math.atan2(shoot["pos"][2] + 1.2 - (pos[2] + muzzle), max(dist, 1))
            aim_yaw, aim_pitch = yaw, pitch
            cy, cp = me["aim"]
            rad = 2.5 if shoot.get("kind") == "mech" else 1.6
            tol = math.atan2(rad * 0.9, max(dist, 1)) + 0.01
            on = abs(wrap(cy - yaw)) < tol + 0.03 and abs(cp - pitch) < tol + 0.05
            if gun and dist < gun["stats"].get("range", 230) and on and gun.get("reload", 0) <= 0:
                fire0 = True
            # secondary
            if w1 is not None:
                k = w1["kind"]
                ws = w1.get("stats", {})
                slot_i = weapons.index(w1)
                f = False
                if k == "laser" and shoot.get("kind") == "mech":
                    ch = w1.get("charge", 0) or 0
                    full = ws.get("charge", 0.75)
                    if dist < ws.get("range", 280):
                        if w1.get("cooldown", 0) <= 0.1 or ch > 0:
                            lyaw, lpitch, _ = self.lead(pos, muzzle, shoot, 1e7, 0.1, 1.0)
                            if ch >= full - 0.25:
                                aim_yaw, aim_pitch = lyaw, lpitch
                            lon = abs(wrap(cy - lyaw)) < tol * 0.8 and abs(cp - lpitch) < tol * 0.8 + 0.01
                            if ch < full - 0.05:
                                f = True  # keep charging
                            elif not lon:
                                f = ch < full + ws.get("hold_s", 2) - 0.3  # hold until on target
                            else:
                                f = False  # release
                    elif ch > 0:
                        f = False
                elif k == "lightning":
                    cone = math.radians(ws.get("cone_deg", 30)) / 2
                    if dist < ws.get("range", 45) - 2 and abs(wrap(cy - yaw)) < cone * 0.8 and me["energy"] > ws.get("energy", 3) + 2:
                        f = True
                elif k == "mortar" and shoot.get("kind") in ("mech", "npc", "crate") and 25 < dist < 220:
                    sol = self.mortar_aim(pos, shoot, ws)
                    if sol and w1.get("cooldown", 0) <= 0:
                        # take over aim only when gun is not mid-burst
                        if not fire0 or (gun and gun.get("ammo", 1) == 0):
                            aim_yaw, aim_pitch = sol[0], sol[1]
                            if abs(wrap(cy - sol[0])) < 0.04 and abs(cp - sol[1]) < 0.04:
                                f = True
                if slot_i == 0:
                    fire0 = fire0 or f
                else:
                    fire1 = f
        else:
            # no target: release laser charge if any (won't fire, just stop)
            # look toward recent threat / sounds / movement
            if self.threat_bearing is not None and now - self.threat_t < 4:
                aim_yaw = self.threat_bearing
            elif target is not None:
                aim_yaw = math.atan2(target["pos"][1] - pos[1], target["pos"][0] - pos[0])
            elif self.recovering and self.last_enemy is not None:
                le = self.last_enemy
                aim_yaw = math.atan2(le[1] - pos[1], le[0] - pos[0]) + math.sin(now * 0.8) * 0.5
            else:
                snd = [s for s in obs.get("sounds", []) if s["kind"] in ("gun", "laser", "laser_charge", "lightning", "step", "boost", "jet", "mortar")]
                if snd:
                    aim_yaw = snd[0]["bearing"]
                    self.threat_bearing, self.threat_t = aim_yaw, now - 2.5
                else:
                    mv = math.hypot(move[0], move[1])
                    base = math.atan2(move[1], move[0]) if mv > 0.3 else me["aim"][0]
                    self.scan_phase += 0.12
                    if now % 9.0 < 1.9:
                        aim_yaw = me["aim"][0] + 1.3   # full turn: spot loot, crates and hunters all round
                    else:
                        aim_yaw = base + math.sin(self.scan_phase) * 1.0
            aim_pitch = 0.0
            # reload when calm
            if gun and gun.get("ammo", 99) < gun["stats"].get("mag", 12) * 0.6 and gun.get("reload", 0) <= 0:
                act["reload"] = True

        # don't move-dash while laser charging? (half speed anyway)
        act["move"] = [round(move[0], 3), round(move[1], 3)]
        act["boost"] = bool(boost)
        act["jump"] = bool(jump)
        act["aim"] = [round(aim_yaw, 4), round(max(-0.69, min(1.29, aim_pitch)), 4)]
        act["fire"] = [bool(fire0), bool(fire1)]
        try:
            mf.draw.label(f"{mode} hp{hpf:.2f} th{n_threat}")
        except Exception:
            pass
        return act

    def loot_goal(self, obs, pos, kinds, slot1, hpf, now):
        best, bs = None, 1e9
        cur_rank = WEAPON_RANK.get(slot1["kind"], 0) if slot1 else 0
        for iid, (k, ip, t) in self.items.items():
            if k in kinds or WEAPON_RANK.get(k, 0) <= cur_rank:
                continue
            if now - self.taken_fail.get(iid, -99) < 3:
                continue
            d = hdist(ip, pos)
            if abs(ip[2] - pos[2]) > 6 and d < 6:
                pass
            if d > 180:
                continue
            s = d - 40 * WEAPON_RANK.get(k, 0)
            if s < bs:
                bs, best = s, (ip[0], ip[1])
        for c in obs.get("cubes", []):
            d = hdist(c["pos"], pos)
            if d < 120 and d - 30 < bs:
                bs, best = d - 30, (c["pos"][0], c["pos"][1])
        # supply drop
        for dr in obs.get("drops", []):
            d = hdist(dr["pos"], pos)
            if d < 200 and dr.get("lands_in", 99) < d / 12 + 2 and hpf > 0.4:
                if d - 50 < bs:
                    bs, best = d - 50, (dr["pos"][0], dr["pos"][1])
        # crates: approach to shooting range
        if best is None or bs > 40:
            for cid, (cp, chp, t) in self.crates.items():
                d = hdist(cp, pos)
                if 50 < d < 150 and d < bs + 50:
                    bs, best = d - 50, (cp[0], cp[1])
        return best

    def find_cover(self, obs, pos, threats, now):
        if self.cover is not None and now - self.cover_t < 1.0:
            return self.cover
        W = self.world
        if not W or not threats:
            return None
        best, bd = None, 1e9
        for r in (12, 24, 40):
            for k in range(12):
                if self.budget_left() < 0.012:
                    break
                a = k * TAU / 12
                x, y = pos[0] + math.cos(a) * r, pos[1] + math.sin(a) * r
                c = W.ncell(x, y)
                if W.blk[c]:
                    continue
                if not self.zone_ok((x, y), obs, 5):
                    continue
                g = W.ground(x, y)
                hidden = True
                for mp in threats[:3]:
                    if W.los(mp[0], mp[1], mp[2] + 8, x, y, g + 4.5):
                        hidden = False
                        break
                if hidden:
                    # prefer away from threats
                    tm = threats[0]
                    s = r - 0.3 * hdist((x, y), tm)
                    if s < bd:
                        bd, best = s, (x, y)
            if best is not None:
                break
        self.cover = best
        self.cover_t = now
        return best


if __name__ == "__main__":
    mf.run(Bot)
