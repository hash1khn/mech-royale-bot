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
# strategy knobs (variants are A/B tested with real submissions)
RANGE_GUN = 100.0     # preferred fighting distance with the gun only
RANGE_LASER = 130.0   # ... holding a laser
ENGAGE_MAX = 120.0    # ignore mechs farther than this unless they are shooting us
SEEK = False          # walk toward the last seen enemy when idle
DUCK = True           # step out of sight of a laser that is charging on us
SALT = 4              # RNG offset: the scorer is deterministic per commit, this re-rolls the matches
USE_DROPS = True      # walk to supply drops
CENTER_W = 0.0        # farming bias toward the next zone centre (cost per metre from it)
CUBE_R = 150.0         # leave a fight this far to pick up cubes (when healthy)


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
UPGRADE_PRIORITY = ["plating", "armor", "laser_damage", "laser_charge", "nanites", "gun_damage", "gun_rate",
                    "lightning_efficiency", "lightning_range", "servos", "gun_mag", "gun_velocity", "gyros",
                    "lightning_chain", "reactor", "capacitor", "mortar_reload", "mortar_splash", "mortar_velocity",
                    "sensors", "jets", "laser_pierce"]
LIGHTNING_FIRST = False  # rank lightning above laser
CUBE_CAP = 10            # stop farming cubes past this (stat cap is 10; upgrades continue at 15, 21)
WEAPON_RANK = {"lightning": 3, "laser": 2, "mortar": 1, "gun": 0} if LIGHTNING_FIRST else {"laser": 3, "lightning": 2, "mortar": 1, "gun": 0}


class Bot:
    name = NAME

    def __init__(self, hello):
        t0 = time.perf_counter()
        self.cfg = hello["config"]
        self.slot = hello.get("slot", 0)
        random.seed(hello.get("seed", 0) + SALT)
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
        self.stick_id = -1; self.stick_t = -99.0; self.lose_t = -99.0
        self.laser_hold = None; self.npc_hurt_t = -99.0; self.recovering = False
        self.npc_mem = {}     # id -> (pos, t)
        self.farm_bl = []; self.farm_since = 0.0; self.farm_cubes = 0; self.farm_goal = None
        self.boosting = False
        self.cube_spots = []  # (x, y, t) where a mech we killed fell
        self.last_seen = {}   # id -> pos

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

    def steer(self, pos, mv):
        """Turn a raw move vector away from walls: first free heading near the wanted one."""
        W = self.world
        l = math.hypot(mv[0], mv[1])
        if not W or l < 0.2:
            return mv
        a0 = math.atan2(mv[1], mv[0])
        for da in (0.0, 0.5, -0.5, 1.0, -1.0, 1.5, -1.5, 2.1, -2.1):
            a = a0 + da
            cx, cy = math.cos(a), math.sin(a)
            if not W.blk[W.ncell(pos[0] + cx * 5, pos[1] + cy * 5)] and not W.blk[W.ncell(pos[0] + cx * 10, pos[1] + cy * 10)]:
                return [cx, cy]
        return mv

    def pref_range(self, me, kinds, t):
        r = 85.0
        if "lightning" in kinds and me["energy"] > 12:
            r = 28.0
        elif "laser" in kinds:
            r = 120.0
        if "lightning" not in kinds:
            tw = t.get("weapons") or []
            if "lightning" in tw:
                r = max(r, 64.0)
            if "mortar" in tw:
                r = max(r, 80.0)
        return r

    def fight_move(self, obs, pos, tg, mechs, want_los, rp, muzzle):
        """Score 16 spots 6 m around us: range, line of fire, who else sees it, walls, zone. Returns a unit move."""
        W = self.world
        now = obs["t"]
        x, y = pos[0], pos[1]
        bx, by = tg["pos"][0] - x, tg["pos"][1] - y
        bd = math.hypot(bx, by) + 1e-9
        ux, uy = bx / bd, by / bd
        if now >= self.jink_t:
            if random.random() < 0.6:
                self.jink_sign = -self.jink_sign
            self.jink_t = now + 0.3 + random.random() * 0.7
        sx, sy = -uy * self.jink_sign, ux * self.jink_sign
        if not W or self.budget_left() < 0.016:
            return self.steer(pos, [sx, sy])
        tsc = 1 + 0.05 * tg.get("cubes", 0)
        tcx, tcy, tcz = tg["pos"][0], tg["pos"][1], tg["pos"][2] + 4.5 * tsc
        others = [m for m in mechs if m is not tg][:3]
        zc, zr = self.zone_goal(obs)
        zlim = max(zr - 10.0, zr * 0.6)
        best, mx, my = -1e18, 0.0, 0.0
        blocked_side = False
        rings = 2 if want_los < -1 else 1
        for k in range(-1, 16 * rings):
            cx, cy, dx, dy, r = x, y, 0.0, 0.0, 0.0
            if k >= 0:
                a = (k % 16) * math.pi / 8
                r = 6.0 * (1 + k // 16)
                dx, dy = math.cos(a), math.sin(a)
                cx, cy = x + dx * r, y + dy * r
                if W.blk[W.ncell(cx, cy)] or W.blk[W.ncell(x + dx * 3, y + dy * 3)] or (r > 6 and (W.blk[W.ncell(x + dx * 6, y + dy * 6)] or W.blk[W.ncell(x + dx * 9, y + dy * 9)])):
                    if k < 16 and dx * sx + dy * sy > 0.9:
                        blocked_side = True
                    continue
            cz = W.ground(cx, cy)
            d = math.hypot(tcx - cx, tcy - cy)
            s = -1.1 * abs(d - rp) - r * 0.4
            los = W.los(cx, cy, cz + muzzle, tcx, tcy, tcz)
            if los:
                s += 45 * want_los
            for e in others:
                ep = e["pos"]
                if math.hypot(ep[0] - cx, ep[1] - cy) <= 170 and W.los(ep[0], ep[1], ep[2] + 8.0, cx, cy, cz + 5.4):
                    s -= 60 if (want_los < -1 and e.get("charging")) else 30
            ex = math.hypot(cx - zc[0], cy - zc[1]) - zlim
            if ex > 0:
                s -= 2.0 * ex
            if W.near[W.ncell(cx, cy)]:
                s -= 5
            if want_los > 0:
                s += 9 * (dx * sx + dy * sy)
            if k < 0:
                s -= 8
                if want_los < 0 and not los:
                    s += 14
            if s > best:
                best, mx, my = s, dx, dy
        if blocked_side:
            self.jink_sign = -self.jink_sign
            self.jink_t = now + 0.5
        return [mx, my]

    def dodge(self, obs, me, mechs):
        pos = me["pos"]
        sc = 1 + 0.05 * me.get("cubes", 0)
        cx, cy, cz = pos[0], pos[1], pos[2] + 9.0 * sc * 0.56
        reach = 2.5 * sc + 1.1
        dx = dy = 0.0
        laser = False
        g = self.cfg.get("weapons", {}).get("mortar", {}).get("gravity", 22)
        for p in obs.get("projectiles", []):
            v, pp = p.get("vel"), p.get("pos")
            if not v or not pp:
                continue
            if p.get("kind") == "mortar":
                # where does the shell land? step its arc to our ground level, then walk out of the blast
                a_, b_, c_ = -0.5 * g, v[2], pp[2] - pos[2]
                disc = b_ * b_ - 4 * a_ * c_
                if disc < 0:
                    continue
                tl = (-b_ - math.sqrt(disc)) / (2 * a_)
                if not 0 < tl < 2.5:
                    continue
                lx, ly = pp[0] + v[0] * tl, pp[1] + v[1] * tl
                dl = math.hypot(cx - lx, cy - ly)
                if dl < 11:
                    if dl > 0.5:
                        dx += (cx - lx) / dl * 1.5; dy += (cy - ly) / dl * 1.5
                    else:
                        dx += 1.5
                continue
            v2 = v[0] * v[0] + v[1] * v[1] + v[2] * v[2]
            if v2 < 1:
                continue
            tc = ((cx - pp[0]) * v[0] + (cy - pp[1]) * v[1] + (cz - pp[2]) * v[2]) / v2
            if tc <= 0 or tc > 0.7:
                continue
            mxx, myy = pp[0] + v[0] * tc - cx, pp[1] + v[1] * tc - cy
            if math.hypot(mxx, myy) < reach:
                vh = math.hypot(v[0], v[1])
                if vh < 1:
                    continue
                px, py = -v[1] / vh, v[0] / vh
                if px * mxx + py * myy > 0:
                    px, py = -px, -py
                dx += px; dy += py
        for m in mechs:
            if not m.get("charging"):
                continue
            to_me = math.atan2(cy - m["pos"][1], cx - m["pos"][0])
            if abs(wrap(m.get("aim", [0, 0])[0] - to_me)) > 0.15:
                continue
            dx += math.cos(to_me + math.pi / 2 * self.jink_sign) * 1.5
            dy += math.sin(to_me + math.pi / 2 * self.jink_sign) * 1.5
            laser = True
        l = math.hypot(dx, dy)
        if l > 0.01:
            return dx / l, dy / l, laser
        return 0.0, 0.0, False

    def budget_left(self):
        return 0.042 - (time.perf_counter() - self.t_start)

    # ---------- aiming
    def lead(self, me_pos, muzzle_h, tgt, speed, delay=0.075, k=0.8):
        p, v = tgt["pos"], tgt.get("vel", [0, 0, 0])
        vz = v[2] if len(v) > 2 else 0.0
        cz = tgt.get("_cz")
        if cz is None:
            cz = 4.5 * (1 + 0.05 * tgt.get("cubes", 0)) if tgt.get("kind") == "mech" else 1.75
        ox, oy, oz = me_pos[0], me_pos[1], me_pos[2] + muzzle_h
        t = 0.0
        px, py, pz, dist = p[0], p[1], p[2] + cz, 1.0
        for _ in range(4):
            tt = t + delay
            px, py = p[0] + v[0] * k * tt, p[1] + v[1] * k * tt
            pz = p[2] + cz + (vz * tt * 0.5 if abs(vz) > 0.5 else 0.0)
            dist = math.sqrt((px - ox) ** 2 + (py - oy) ** 2 + (pz - oz) ** 2)
            t = max(0.0, dist - 3.0) / speed
        yaw = math.atan2(py - oy, px - ox)
        pitch = math.atan2(pz - oz, math.hypot(px - ox, py - oy))
        return yaw, pitch, math.hypot(px - ox, py - oy)

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
                if ev.get("weapon") == "npc":
                    self.npc_hurt_t = now
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
            elif et == "kill":
                lp = self.last_seen.get(ev.get("target"))
                if lp is not None:
                    self.cube_spots.append((lp[0], lp[1], now))
        for m_ in obs.get("mechs", []):
            if m_.get("kind") == "mech":
                self.last_seen[m_["id"]] = m_["pos"]
        self.cube_spots = [c for c in self.cube_spots if now - c[2] < 70 and hdist(c, pos) > 6]

        # ---- item memory
        for p in obs.get("pickups", []):
            self.items[p["id"]] = (p["kind"], p["pos"], now)
        vis_ids = {p["id"] for p in obs.get("pickups", [])}
        for iid in list(self.items):
            k, ip, t = self.items[iid]
            if iid not in vis_ids and (hdist(ip, pos) < 25 or now - t > 150):
                del self.items[iid]
        for c in obs.get("crates", []):
            self.crates[c["id"]] = (c["pos"], c["hp"], now)
        vis_c = {c["id"] for c in obs.get("crates", [])}
        for cid in list(self.crates):
            cp, _, t = self.crates[cid]
            if cid not in vis_c and (hdist(cp, pos) < 35 or now - t > 150):
                del self.crates[cid]
        vis_n = set()
        for n_ in obs.get("mechs", []):
            if n_.get("kind") == "npc":
                self.npc_mem[n_["id"]] = (n_["pos"], now); vis_n.add(n_["id"])
        for nid in list(self.npc_mem):
            np_, t = self.npc_mem[nid]
            if nid not in vis_n and (hdist(np_, pos) < 40 or now - t > 120):
                del self.npc_mem[nid]

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
            if hdist(p["pos"], pos) < reach and abs(p["pos"][2] - pos[2]) < 3.8 and p["kind"] not in kinds and p["kind"] != "mortar":
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
        z = obs["zone"]
        late = z["next_radius"] <= 70 or obs.get("alive", 16) <= 3
        sv = me.get("vel", [0, 0, 0])
        for m in mechs:
            d_ = hdist(m["pos"], pos)
            m["_d"] = d_
            m["_los"] = self.los_mech(pos, eye_off, m) if d_ < 260 else False
            yaw_to_me = math.atan2(pos[1] - m["pos"][1], pos[0] - m["pos"][0])
            m["_aimed"] = abs(wrap(m.get("aim", [0, 0])[0] - yaw_to_me)) < 0.22
            mv_ = m.get("vel", [0, 0, 0])
            m["_closing"] = ((mv_[0] - sv[0]) * (pos[0] - m["pos"][0]) + (mv_[1] - sv[1]) * (pos[1] - m["pos"][1])) / max(d_, 1.0)
            m["_hit"] = now - self.attackers.get(m["id"], -99)
            hp_, mhp_ = m.get("hp", 1400), max(m.get("max_hp", 1400), 1)
            # who is worth fighting: close, shooting us, coming at us, weak, or pointing at us
            m["_engage"] = (d_ < 60 or m["_hit"] < 6 or late or (m["_closing"] > 3 and d_ < 180)
                            or (hp_ < 0.4 * mhp_ and d_ < 160) or (m["_aimed"] and d_ < 190))
        recent_attackers = [k for k, t in self.attackers.items() if now - t < 3]
        visible = [m for m in mechs if m["_los"]]
        if mechs:
            c0 = min(mechs, key=lambda m: m["_d"])
            self.last_enemy = (c0["pos"][0], c0["pos"][1], c0["pos"][2], now)
        near = sum(1 for m in mechs if m["_d"] < 160)
        n_threat = near

        # target: engaged and in sight; stick with the current one, prefer nearly dead ones
        target, hidden, best_s = None, None, 1e9
        for m in mechs:
            if not m["_engage"] or m["_d"] > 240:
                continue
            if not m["_los"]:
                if hidden is None or m["_d"] < hidden["_d"]:
                    hidden = m
                continue
            hp_, mhp_ = m.get("hp", 1400), max(m.get("max_hp", 1400), 1)
            sc_ = m["_d"] + 0.2 * hp_
            if hp_ <= 0.3 * mhp_:
                sc_ -= 100
            if m["id"] == self.stick_id and now - self.stick_t < 2:
                sc_ -= 120
            if m["_hit"] < 4:
                sc_ -= 40
            if m["_closing"] > 3:
                sc_ -= 15
            if sc_ < best_s:
                best_s, target = sc_, m
        if target is not None:
            self.stick_id, self.stick_t = target["id"], now

        # ---- decide mode: fight by default, retreat only in short pulses while clearly losing
        in_zone = me.get("in_zone", True)
        zc, zr = self.zone_goal(obs)
        tgt_hpf = (target.get("hp", 1400) / max(target.get("max_hp", 1400), 1)) if target else 1
        losing = (near >= 2 and hpf < 0.55) or (near >= 3 and hpf < 0.8)
        if target is not None:
            if hpf < 0.35 and tgt_hpf > hpf + 0.1:
                losing = True
            if tgt_hpf < 0.25 and hpf > 0.2 and near <= 2:
                losing = False
        if losing:
            self.lose_t = now
        retreat = now - self.lose_t < 1.5
        finishing = target is not None and target["_d"] < 120 and (target.get("hp", 1400) < 200 or target.get("hp", 1400) < hp * 0.5)
        crowd = sum(1 for m in mechs if m["_engage"] or (m["_los"] and m["_d"] < 150))
        mode = "roam"
        if retreat and not finishing and not late and mechs:
            mode = "retreat"
        elif target is not None:
            mode = "fight"
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
        outside_next = dz_center > max(zr - 10.0, zr * 0.6)
        time_to_shrink = z["shrink_end"] - now
        need_dist = dz_center - (zr - 10.0)
        walk = st.get("walk_speed", 11.7)
        zone_urgent = (not in_zone) or (outside_next and need_dist / (walk * 0.8) > time_to_shrink - 15)

        if mode in ("fight", "retreat") and not zone_urgent:
            if mode == "fight":
                d = target["_d"]
                gun_ready = bool(gun) and gun.get("ammo", 0) > 0 and gun.get("reload", 0) <= 0.25 and d <= gun["stats"].get("range", 230) - 10
                ready = gun_ready or "laser" in kinds or "lightning" in kinds
                rp = self.pref_range(me, kinds, target)
                if tgt_hpf < 0.38 and hpf > 0.42 and crowd <= 2:
                    rp = 20.0   # close in to finish it (and to be standing on its cubes)
                want_los = 1.0 if ready else -1.0
                # an enemy laser is charging on us: lasers can't be sidestepped, so step out of its sight if cover is close
                lw_ = next((w for w in weapons if w and w["kind"] == "laser"), None)
                hot = lw_ is not None and (lw_.get("charge", 0) or 0) >= lw_["stats"].get("charge", 0.75) - 0.3
                if DUCK and not hot and not finishing and any(
                        m.get("charging") and m["_los"] and abs(wrap(m.get("aim", [0, 0])[0] - math.atan2(pos[1] - m["pos"][1], pos[0] - m["pos"][0]))) < 0.3
                        for m in mechs):
                    want_los = -1.6
                move = self.fight_move(obs, pos, target, mechs, want_los, rp, muzzle)
                boost = "lightning" in kinds and d > 50 and me["energy"] > 55
                if me.get("cubes", 0) < CUBE_CAP:
                    cube_r = 40.0 * (0.57 if hpf <= 0.5 else 1.0)
                    cube = min((c for c in obs.get("cubes", []) if abs(c["pos"][2] - pos[2]) < 4), key=lambda c: hdist(c["pos"], pos), default=None)
                    if cube is not None and hdist(cube["pos"], pos) < cube_r:
                        move = self.navigate(obs, (cube["pos"][0], cube["pos"][1]), now)
            else:  # short retreat pulse to cover
                cov = self.find_cover(obs, pos, [m["pos"] for m in sorted(mechs, key=lambda m: m["_d"]) if m["_d"] < 200], now)
                if cov is not None:
                    move = self.navigate(obs, cov, now)
                elif target is not None:
                    move = self.fight_move(obs, pos, target, mechs, 1.0, self.pref_range(me, kinds, target), muzzle)
                else:
                    c0 = min(mechs, key=lambda m: m["_d"])
                    away = math.atan2(pos[1] - c0["pos"][1], pos[0] - c0["pos"][0])
                    move = self.steer(pos, [math.cos(away), math.sin(away)])
                boost = me["energy"] > 10
            for dr in (obs.get("drops", []) if USE_DROPS else []):
                dd_ = hdist(dr["pos"], pos)
                if dd_ < 140 and dr.get("lands_in", 99) < dd_ / 11 + 3 and (hpf < 0.7 or me.get("cubes", 0) < CUBE_CAP):
                    move = self.navigate(obs, (dr["pos"][0], dr["pos"][1]), now)
                    boost = boost or (dd_ > 20 and me["energy"] > 30)
                    break
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
                if hpf < 0.5 and loot_goal is not None and hdist(loot_goal, pos) > 45 and not any(
                        hdist(dr["pos"], loot_goal) < 1 for dr in obs.get("drops", [])):
                    loot_goal = None
                if loot_goal is not None and (goal is None or self.zone_ok(loot_goal, obs, 5)):
                    goal = loot_goal
                    shootable = [c_[0] for c_ in self.crates.values()] + [n_[0] for n_ in self.npc_mem.values()]
                    if any(hdist(q, goal) < 3 for q in shootable):
                        dg = hdist(goal, pos)
                        if dg < 30 and (self.world is None or self.world.los(pos[0], pos[1], pos[2] + muzzle, goal[0], goal[1], self.world.ground(goal[0], goal[1]) + 1.5)):
                            goal = (pos[0], pos[1]) if dg > 14 else (pos[0] + (pos[0] - goal[0]), pos[1] + (pos[1] - goal[1]))
            recovering = False
            if goal is None and hpf < 0.5 and not zone_urgent and not visible:
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
            if goal is None and SEEK and hpf > 0.7 and self.last_enemy is not None and now - self.last_enemy[3] < 40 \
                    and hdist(self.last_enemy, pos) > 60 and self.zone_ok(self.last_enemy, obs, 5):
                goal = (self.last_enemy[0], self.last_enemy[1])
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
            if not boost and not recovering:
                if me["energy"] > 85:
                    self.boosting = True
                elif me["energy"] < 35:
                    self.boosting = False
                if self.boosting and hdist(pos, goal) > 25 and "lightning" not in kinds:
                    boost = True

        # ---- dodge bullets, shells and charging lasers that are lined up on us
        if now >= self.unstick_until:
            ddx, ddy, lz = self.dodge(obs, me, mechs)
            if ddx or ddy:
                move = [move[0] * 0.4 + ddx * 0.9, move[1] * 0.4 + ddy * 0.9]
                l_ = math.hypot(move[0], move[1])
                if l_ > 1:
                    move = [move[0] / l_, move[1] / l_]
                if lz and me["energy"] > 25:
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
        LAT = 0.075
        mo = (pos[0] + sv[0] * LAT, pos[1] + sv[1] * LAT, pos[2])
        aim_target = target
        npc_t = None
        crate_t = None
        npc_hurt = now - self.npc_hurt_t < 3
        if aim_target is None and mode != "retreat":
            if npcs and (hpf > 0.5 or npc_hurt):
                cands = [n for n in npcs if hdist(n["pos"], pos) < 150 and self.los_mech(pos, muzzle, n)]
                if cands:
                    npc_t = min(cands, key=lambda n: hdist(n["pos"], pos))
            if npc_t is None and hpf > 0.5:
                cands = [c for c in obs.get("crates", []) if 6 < hdist(c["pos"], pos) < 110]
                for c in sorted(cands, key=lambda c: hdist(c["pos"], pos)):
                    if self.world is None or self.world.los(pos[0], pos[1], pos[2] + muzzle, c["pos"][0], c["pos"][1], c["pos"][2] + 1.2):
                        crate_t = c
                        break
        shoot = aim_target or npc_t
        if shoot is None and crate_t is not None:
            shoot = {"pos": crate_t["pos"], "vel": [0, 0, 0], "kind": "crate"}
        laser_w = next((w for w in weapons if w and w["kind"] == "laser"), None)
        fires = [False, False]

        if shoot is not None:
            kind_ = shoot.get("kind")
            is_mech = kind_ == "mech"
            tsc = 1 + 0.05 * shoot.get("cubes", 0) if is_mech else 1.0
            tr, th = (2.5 * tsc, 9.0 * tsc) if is_mech else ((1.8, 3.5) if kind_ == "npc" else (1.4, 2.4))
            shoot["_cz"] = th * 0.5
            gspeed = gun["stats"].get("speed", 252) if gun else 252
            gy, gp, dist = self.lead(mo, muzzle, shoot, gspeed, LAT, 0.8)
            hy, hpit, _ = self.lead(mo, muzzle, shoot, 1e7, LAT + 0.05, 1.0)
            aim_yaw, aim_pitch = (gy, gp) if gun else (hy, hpit)
            cy, cp = me["aim"]
            tol_y = math.atan2(tr * 0.75, max(dist, 1))
            tol_p = math.atan2(th * 0.4, max(dist, 1))
            prate = st.get("pitch_rate", 3.0)

            def slew_ok(wy, wp):
                ey = max(0.0, abs(wrap(wy - cy)) - turn * 0.05)
                ep = max(0.0, abs(wp - cp) - prate * 0.05)
                return ey < tol_y and ep < tol_p

            for i_, w in enumerate(weapons[:2]):
                if not w:
                    continue
                k = w["kind"]
                ws = w.get("stats", {})
                if k == "gun":
                    if dist <= ws.get("range", 230) * 0.96 and w.get("ammo", 0) > 0 and w.get("reload", 0) <= 0 and slew_ok(gy, gp):
                        fires[i_] = True
                elif k == "lightning":
                    cone = math.radians(ws.get("cone_deg", 30))
                    if dist < ws.get("range", 45) - 1.5 and abs(wrap(hy - cy)) < cone * 0.5 - 0.05 and me["energy"] >= ws.get("energy", 3):
                        fires[i_] = True
                elif k == "laser":
                    if not is_mech or dist > ws.get("range", 280) * 0.97:
                        continue
                    ch = w.get("charge", 0) or 0
                    full = ws.get("charge", 0.75)
                    on = abs(wrap(hy - cy)) < tol_y * 0.9 and abs(hpit - cp) < tol_p
                    if ws.get("min_charge", 0.15) <= ch < full - 0.051:
                        # a partial charge that already kills: fire now
                        dmg = ws.get("damage", 315) * (ws.get("min_frac", 0.35) + (1 - ws.get("min_frac", 0.35)) * ch / full) * (1 + 0.04 * min(me.get("cubes", 0), 10))
                        if "mortar" in (shoot.get("weapons") or []):
                            dmg *= 1 + ws.get("cookoff", 0.6)
                        if dmg * 0.8 >= shoot.get("hp", 1e9) and on:
                            aim_yaw, aim_pitch = hy, hpit
                            continue
                    if ch < full - 0.051:
                        fires[i_] = True
                        if ch > full * 0.45:
                            aim_yaw, aim_pitch = hy, hpit
                    else:
                        aim_yaw, aim_pitch = hy, hpit
                        fires[i_] = not on   # release exactly when lined up
            self.laser_hold = (shoot["pos"], now) if is_mech else self.laser_hold
        else:
            # target dropped out of sight mid-charge: keep the charge for up to 1 s, aimed where it was
            held = False
            if laser_w is not None and (laser_w.get("charge", 0) or 0) > 0 and self.laser_hold is not None and now - self.laser_hold[1] < 1.0:
                ws = laser_w.get("stats", {})
                if (laser_w.get("charge", 0) or 0) < ws.get("charge", 0.75) + ws.get("hold_s", 2) - 0.35:
                    fires[weapons.index(laser_w)] = True
                    lp = self.laser_hold[0]
                    aim_yaw = math.atan2(lp[1] - pos[1], lp[0] - pos[0])
                    aim_pitch = math.atan2(lp[2] + 4.5 - (pos[2] + muzzle), max(hdist(lp, pos), 1))
                    held = True
            if not held:
                watch = min((m for m in mechs if m["_engage"] or m["_hit"] < 6), key=lambda m: m["_d"], default=None)
                if watch is not None:
                    aim_yaw = math.atan2(watch["pos"][1] - pos[1], watch["pos"][0] - pos[0])
                elif self.threat_bearing is not None and now - self.threat_t < 2.5:
                    aim_yaw = self.threat_bearing
                else:
                    mv = math.hypot(move[0], move[1])
                    if now % 9.0 < 1.9 and not self.recovering:
                        aim_yaw = me["aim"][0] + 1.3   # full turn: spot loot, crates and hunters all round
                    elif mv > 0.3:
                        aim_yaw = math.atan2(move[1], move[0]) + 1.15 * math.sin(now * 1.3)
                    else:
                        aim_yaw = me["aim"][0] + 0.35
                aim_pitch = 0.0
            if gun and gun.get("reload", 0) <= 0 and gun.get("ammo", 99) < gun["stats"].get("mag", 12) and (
                    gun.get("ammo", 99) <= gun["stats"].get("mag", 12) * 0.6 or now - self.last_dmg_t > 3):
                act["reload"] = True
        fire0, fire1 = fires

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
        """Cheapest worthwhile thing to walk to: cost is metres of travel minus a value bonus."""
        best, bs = None, 1e9
        z = obs["zone"]
        late = now > z["shrink_start"] - 25
        W = self.world

        prev = getattr(self, "farm_goal", None)
        bl = self.farm_bl

        def consider(p, cost, keep=False):
            nonlocal best, bs
            if CENTER_W:
                cost += CENTER_W * hdist(p, z["next_center"])
            if not keep and bl and any(hdist(p, q) < 6 and now - tq < 60 for q, tq in bl):
                return
            if prev is not None and hdist(p, prev) < 6:
                cost -= 30  # commitment: don't dither between two targets
            if cost < bs and (not late or self.zone_ok(p, obs, 5)):
                bs, best = cost, (p[0], p[1])

        cur_rank = WEAPON_RANK.get(slot1["kind"], 0) if slot1 else 0
        for iid, (k, ip, t) in self.items.items():
            if k in kinds or k == "mortar" or WEAPON_RANK.get(k, 0) <= cur_rank:
                continue
            if now - self.taken_fail.get(iid, -99) < 3:
                continue
            if W and ip[2] > W.ground(ip[0], ip[1]) + 3:
                continue  # on a roof
            d = hdist(ip, pos)
            if k == ("lightning" if LIGHTNING_FIRST else "laser"):
                consider(ip, d - 230)   # hitscan at any range: worth a long walk
            elif d < 220:
                consider(ip, d - 45 * (WEAPON_RANK.get(k, 0) - cur_rank))
        ncubes = obs["self"].get("cubes", 0)
        want_cubes = ncubes < CUBE_CAP
        for c in (obs.get("cubes", []) if want_cubes else []):
            cp = c["pos"]
            if W and cp[2] > W.ground(cp[0], cp[1]) + 3 and abs(cp[2] - pos[2]) > 3:
                continue
            d = hdist(cp, pos)
            if d < 200:
                consider(cp, d - 70)
        if want_cubes and not obs.get("cubes"):
            for c in self.cube_spots:
                consider(c, hdist(c, pos) - 90)
        for dr in (obs.get("drops", []) if USE_DROPS else []):
            d = hdist(dr["pos"], pos)
            if d < 340 and dr.get("lands_in", 99) < d / 11 + 10 and (want_cubes or hpf < 0.8):
                consider(dr["pos"], d - 170, True)
        has_laser = "laser" in kinds
        for cid, (cp, chp, t) in self.crates.items():
            if W and cp[2] > W.ground(cp[0], cp[1]) + 3:
                continue
            if want_cubes or not has_laser:   # crates also drop weapons half the time
                consider(cp, hdist(cp, pos) - 35)
        if hpf > 0.6 and want_cubes:
            for nid, (np_, t) in self.npc_mem.items():
                consider(np_, hdist(np_, pos) - 55)
        # give up on a target we have chased for 14 s without getting anything (roof, walled in, bugged)
        cubes_now = obs["self"].get("cubes", 0)
        if best is None or prev is None or hdist(best, prev) > 6 or cubes_now != self.farm_cubes:
            self.farm_since = now
            self.farm_cubes = cubes_now
        elif any(hdist(dr["pos"], best) < 2 for dr in obs.get("drops", [])):
            self.farm_since = now
        elif now - self.farm_since > 14:
            bl.append((best, now))
            del bl[:-12]
            self.farm_since = now
        self.farm_goal = best
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
