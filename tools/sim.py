"""Rough local match simulator for bot.py: real map + zone + weapons, scripted squad of 15 hunters.
Not the real server. Use it to find bugs (stuck, zone deaths) and to A/B changes.
  python3 tools/sim.py [--bot DIR] [--n 16] [--seed 1] [--v]
"""
import argparse, copy, gzip, json, math, os, random, sys, time
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TAU = 2 * math.pi
DT = 0.05


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def hd(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


DIFF = {  # aim error sigma (rad), fire duty, preferred range
    "easy": dict(sig=0.11, pref=(45, 75), lead=0.3, duty=0.5),
    "medium": dict(sig=0.06, pref=(40, 70), lead=0.7, duty=0.7),
    "hard": dict(sig=0.035, pref=(35, 70), lead=1.0, duty=0.85),
    "expert": dict(sig=0.02, pref=(35, 80), lead=1.0, duty=1.0),
}
if os.environ.get("SIM_DIFF"):
    for k_, v_ in json.loads(os.environ["SIM_DIFF"]).items(): DIFF[k_].update(v_)


class M:
    def __init__(s, i, pos, cfg, diff=None):
        s.id = i; s.pos = list(pos); s.vel = [0.0, 0.0, 0.0]; s.aim = [0.0, 0.0]; s.cmd_aim = [0.0, 0.0]
        s.base = copy.deepcopy(cfg["mech"]); s.st = dict(s.base)
        s.wbase = copy.deepcopy(cfg["weapons"]); s.ups = []
        s.hp = s.max_hp = s.st["hp"]; s.energy = s.st["energy"]; s.cubes = 0
        s.weapons = [s.mk("gun"), None]
        s.alive = True; s.grounded = True; s.last_combat = -99; s.last_spend = -99; s.diff = diff
        s.move = [0.0, 0.0]; s.boost = False; s.jump = False; s.fire = [False, False]
        s.events = []; s.thresh_i = 0; s.offers = []; s.offer = None
        s.zone_dmg = 0.0; s.dealt = 0.0; s.taken = {}
        # AI state
        s.path = None; s.path_t = -99; s.goal = None; s.strafe = 1; s.strafe_t = 0; s.err = [0.0, 0.0]; s.err_t = 0
        s.short_until = -1; s.burst_t = 0; s.burst_on = True

    def mk(s, kind):
        w = {"kind": kind, "cooldown": 0.0, "stats": dict(s.wbase[kind])}
        if kind == "gun":
            w["ammo"] = w["stats"]["mag"]; w["reload"] = 0.0
        if kind == "laser":
            w["charge"] = 0.0; w["charging"] = False
        return w

    def scale(s):
        return 1 + 0.05 * s.cubes

    def recompute(s, cfg):
        st = dict(s.base); wb = copy.deepcopy(cfg["weapons"])
        ups = {u["id"]: u for u in cfg["upgrades"]}
        for uid in s.ups:
            for e in ups[uid]["effects"]:
                p = e["path"].split(".")
                d = st if p[0] == "mech" else wb[p[1]]
                k = p[-1]
                if "mul" in e: d[k] = d[k] * e["mul"]
                if "add" in e: d[k] = d[k] + e["add"]
        s.st = st; s.wbase = wb
        for w in s.weapons:
            if w: w["stats"] = dict(wb[w["kind"]])
        old = s.max_hp
        s.max_hp = st["hp"] * (1 + 0.04 * min(s.cubes, 10))
        if s.max_hp > old: s.hp += s.max_hp - old
        s.hp = min(s.hp, s.max_hp)


class Sim:
    def __init__(s, botdir, seed, verbose=False):
        s.rng = random.Random(seed); s.v = verbose; s.seed = seed
        import importlib.util
        def load(path, name):
            sp = importlib.util.spec_from_file_location(name, path); mod = importlib.util.module_from_spec(sp)
            sys.modules[name] = mod; sp.loader.exec_module(mod); return mod
        sys.path.insert(0, ROOT)
        import mechfight as _mf
        _mf.run = lambda *a, **k: None
        ref = load(os.path.join(ROOT, "bot.py"), "refbot")
        B = load(os.path.join(botdir, "bot.py"), "testbot")
        hello = json.loads(gzip.open(os.path.join(ROOT, "check", "stream.jsonl.gz"), "rt").readline())
        hello["map"]["dir"] = os.path.join(ROOT, "arena"); hello["slot"] = 0; hello["seed"] = seed
        s.cfg = hello["config"]
        s.bot = B.Bot(hello)
        s.W = getattr(s.bot, "world", None) if botdir == ROOT else None
        if s.W is None: s.W = ref.World(hello["map"]["dir"])
        s.t = 0.0; s.tick = 0
        z = s.cfg["zone"]; s.zc = list(z["start_center"]); s.zr = z["start_radius"]
        s.phases = []
        t = 0; c = list(z["start_center"]); r = z["start_radius"]
        for ph in z["phases"]:
            a = s.rng.uniform(0, TAU); dmax = max(r - ph["radius"], 0) * 0.8; dd = s.rng.uniform(0, dmax)
            nc = [c[0] + math.cos(a) * dd, c[1] + math.sin(a) * dd]
            s.phases.append(dict(t0=t, ss=t + ph["hold_s"], se=t + ph["hold_s"] + ph["shrink_s"], c0=c, r0=r, c1=nc, r1=ph["radius"], dps=ph["dps"]))
            t += ph["hold_s"] + ph["shrink_s"]; c = nc; r = ph["radius"]
        # spawns
        diffs = ["easy"] * 4 + ["medium"] * 4 + ["hard"] * 4 + ["expert"] * 3
        s.rng.shuffle(diffs)
        s.mechs = []
        pts = []
        while len(pts) < 16:
            p = s.free_point(s.zc, s.zr * 0.8)
            if all(hd(p, q) > 130 for q in pts): pts.append(p)
        for i in range(16):
            p = pts[i]
            s.mechs.append(M(i, [p[0], p[1], s.W.ground(*p)], s.cfg, None if i == 0 else diffs[i - 1]))
        s.me = s.mechs[0]
        for m in s.mechs[1:]:
            m.pref = s.rng.uniform(*DIFF[m.diff]["pref"])
            m.sec_t = s.rng.uniform(15, 150); m.sec_kind = s.rng.choice(["laser", "lightning", "mortar", None])
            m.cube_t = s.rng.uniform(30, 70)
        s.order = sorted(s.mechs[1:], key=lambda m: (["easy", "medium", "hard", "expert"].index(m.diff), m.id))
        s.kills = 0
        s.proj = []; s.pid = 0
        s.pickups = {}; s.cubes = {}; s.crates = {}
        nid = 8000
        for _ in range(s.cfg["loot"]["floor_count"]):
            p = s.free_point(s.zc, s.zr * 0.9)
            s.pickups[nid] = (s.rng.choice(["mortar", "laser", "lightning"]), [p[0], p[1], s.W.ground(*p)]); nid += 1
        s.next_pick = nid; s.next_cube = 5000
        cid = 1000
        for _ in range(27):
            p = s.free_point(s.zc, s.zr * 0.9)
            s.crates[cid] = dict(pos=[p[0], p[1], s.W.ground(*p)], hp=300.0); cid += 1
        s.drops = []; s.drop_i = 0; s.drops_taken = 0
        s.seen = {}  # id -> last seen t
        s.enemy_knows = -99; s.last_known = None
        s.log = dict(stuck=0.0, death=None, first_w=None, modes={})

    def free_point(s, c, r):
        W = s.W
        for _ in range(500):
            a = s.rng.uniform(0, TAU); d = r * math.sqrt(s.rng.random())
            x, y = c[0] + math.cos(a) * d, c[1] + math.sin(a) * d
            if W.minX + 20 < x < W.maxX - 20 and W.minY + 20 < y < W.maxY - 20 and not W.blk[W.ncell(x, y)] and not W.near[W.ncell(x, y)]:
                return (x, y)
        return (c[0], c[1])

    # ---- zone
    def zone_now(s):
        t = s.t
        for i, ph in enumerate(s.phases):
            if t < ph["se"] or i == len(s.phases) - 1:
                f = 0.0 if t < ph["ss"] else min(1.0, (t - ph["ss"]) / max(ph["se"] - ph["ss"], 1e-6))
                c = [ph["c0"][0] + (ph["c1"][0] - ph["c0"][0]) * f, ph["c0"][1] + (ph["c1"][1] - ph["c0"][1]) * f]
                r = ph["r0"] + (ph["r1"] - ph["r0"]) * f
                return dict(center=c, radius=r, next_center=ph["c1"], next_radius=ph["r1"], shrink_start=ph["ss"], shrink_end=ph["se"], dps=ph["dps"])

    # ---- physics
    def blocked(s, x, y, z, sc=1.0):
        W = s.W
        if not (W.minX + 3 < x < W.maxX - 3 and W.minY + 3 < y < W.maxY - 3): return True
        for ox, oy in ((0, 0), (2.2, 0), (-2.2, 0), (0, 2.2), (0, -2.2)):
            c = W.cell(x + ox, y + oy)
            if c < 0: return True
            if W.btop[c] > z + 1.2 and W.bbase[c] < z + 9 * sc: return True
        return False

    def floor(s, x, y, z):
        W = s.W
        g = W.ground(x, y)
        c = W.cell(x, y)
        if c >= 0 and W.btop[c] > g and W.btop[c] <= z + 1.2 and W.bbase[c] < g + 1.5:
            return W.btop[c]
        return g

    def step_mech(s, m):
        st = m.st
        mv = m.move; l = math.hypot(mv[0], mv[1])
        if l > 1: mv = [mv[0] / l, mv[1] / l]; l = 1
        charging = any(w and w["kind"] == "laser" and w.get("charging") for w in m.weapons)
        sp = st["walk_speed"]
        if m.boost and m.grounded and l > 0.05 and m.energy > 0:
            sp = st["boost_speed"]; m.energy -= st["boost_drain"] * DT; m.last_spend = s.t
        if charging: sp *= st["charge_move_mult"]
        dvx, dvy = mv[0] * sp - m.vel[0], mv[1] * sp - m.vel[1]
        acc = (st["ground_accel"] if m.grounded else st["air_accel"]) * DT
        dl = math.hypot(dvx, dvy)
        if dl > acc: dvx, dvy = dvx / dl * acc, dvy / dl * acc
        m.vel[0] += dvx; m.vel[1] += dvy
        if m.grounded and m.jump:
            m.vel[2] = st["jump_speed"]; m.grounded = False
        if not m.grounded:
            if m.boost and m.energy > 0:
                m.vel[2] = min(m.vel[2] + st["jet_accel"] * DT, st["max_rise_speed"]); m.energy -= st["jet_drain"] * DT; m.last_spend = s.t
            m.vel[2] -= st["gravity"] * DT
        m.energy = max(m.energy, 0)
        if s.t - m.last_spend > st["energy_regen_delay"]:
            m.energy = min(st["energy"], m.energy + st["energy_regen"] * DT)
        x, y, z = m.pos
        nx, ny = x + m.vel[0] * DT, y + m.vel[1] * DT
        sc = m.scale()
        if not s.blocked(nx, ny, z, sc): x, y = nx, ny
        elif not s.blocked(nx, y, z, sc): x = nx; m.vel[1] = 0
        elif not s.blocked(x, ny, z, sc): y = ny; m.vel[0] = 0
        else: m.vel[0] = m.vel[1] = 0
        fl = s.floor(x, y, z)
        if m.grounded:
            if fl < z - 1.5: m.grounded = False
            else: z = fl
        if not m.grounded:
            z += m.vel[2] * DT
            if z <= fl and m.vel[2] <= 0:
                z = fl; m.vel[2] = 0; m.grounded = True
        m.pos = [x, y, z]
        # aim slew
        dy = wrap(m.cmd_aim[0] - m.aim[0]); mx = st["turn_rate"] * DT
        m.aim[0] = wrap(m.aim[0] + max(-mx, min(mx, dy)))
        cp = max(st["min_pitch"], min(st["max_pitch"], m.cmd_aim[1]))
        dp = cp - m.aim[1]; mp = st["pitch_rate"] * DT
        m.aim[1] += max(-mp, min(mp, dp))

    # ---- damage
    def damage(s, tgt, amt, weapon, src, origin):
        if not tgt.alive: return
        mult = 1 + 0.04 * min(src.cubes, 10) if src else 1
        amt = amt * mult * (1 - tgt.st.get("armor", 0))
        rear = False
        if origin is not None:
            b = math.atan2(origin[1] - tgt.pos[1], origin[0] - tgt.pos[0])
            if abs(wrap(b - tgt.aim[0])) > math.radians(tgt.st["front_arc_deg"]) / 2:
                amt *= tgt.st["rear_mult"]; rear = True
        tgt.hp -= amt; tgt.last_combat = s.t
        tgt.taken[weapon] = tgt.taken.get(weapon, 0) + amt
        if src:
            src.last_combat = s.t; src.dealt += amt
            src.events.append(dict(type="hit", target=tgt.id, amount=round(amt), weapon=weapon))
        b = math.atan2(origin[1] - tgt.pos[1], origin[0] - tgt.pos[0]) if origin is not None else None
        ev = dict(type="damaged", amount=round(amt), weapon=weapon, bearing=b, by=src.id if src else None)
        if rear: ev["rear"] = True
        tgt.events.append(ev)
        if tgt is s.me: s.last_hit = (weapon, src.id if src else None, src.diff if src else None, hd(src.pos, tgt.pos) if src else 0)
        if tgt.hp <= 0: s.kill(tgt, src, weapon)

    def kill(s, tgt, src, weapon):
        tgt.alive = False
        for _ in range(tgt.cubes + 2):
            s.cubes[s.next_cube] = [tgt.pos[0] + s.rng.uniform(-3, 3), tgt.pos[1] + s.rng.uniform(-3, 3), tgt.pos[2]]; s.next_cube += 1
        for w in tgt.weapons:
            if w and w["kind"] != "gun":
                s.pickups[s.next_pick] = (w["kind"], list(tgt.pos)); s.next_pick += 1
        if src:
            src.events.append(dict(type="kill", target=tgt.id))
            if src is s.me: s.kills += 1
        if s.v: print(f"  t={s.t:6.1f} death {tgt.id}({tgt.diff}) by {src.id if src else 'zone'} {weapon} alive={sum(m.alive for m in s.mechs)}")

    def muzzle(s, m):
        yaw, pit = m.aim
        d = (math.cos(pit) * math.cos(yaw), math.cos(pit) * math.sin(yaw), math.sin(pit))
        o = (m.pos[0] + d[0] * 3, m.pos[1] + d[1] * 3, m.pos[2] + 7 * m.scale() + d[2] * 3)
        return o, d

    def ray_hit(s, o, d, maxr, owner):
        """first mech hit by ray; returns (mech, dist) honoring walls."""
        best = None
        for m in s.mechs:
            if not m.alive or m is owner: continue
            sc = m.scale(); rad = 2.5 * sc
            rx, ry = m.pos[0] - o[0], m.pos[1] - o[1]
            dh = math.hypot(d[0], d[1])
            if dh < 1e-6: continue
            tpar = (rx * d[0] + ry * d[1]) / (dh * dh)
            if tpar < 0 or tpar > maxr: continue
            px, py = o[0] + d[0] * tpar, o[1] + d[1] * tpar
            if math.hypot(px - m.pos[0], py - m.pos[1]) > rad: continue
            pz = o[2] + d[2] * tpar
            if not (m.pos[2] - 0.2 <= pz <= m.pos[2] + 9 * sc + 0.2): continue
            if best is None or tpar < best[1]: best = (m, tpar)
        if best:
            e = (o[0] + d[0] * best[1], o[1] + d[1] * best[1], o[2] + d[2] * best[1])
            if not s.W.los(o[0], o[1], o[2], e[0], e[1], e[2]): return None
        return best

    def weapons_step(s, m):
        for i, w in enumerate(m.weapons):
            if not w: continue
            w["cooldown"] = max(0.0, w["cooldown"] - DT)
            trig = m.fire[i]
            k = w["kind"]; ws = w["stats"]
            if k == "gun":
                if w["reload"] > 0:
                    w["reload"] -= DT
                    if w["reload"] <= 0: w["reload"] = 0; w["ammo"] = ws["mag"]
                    continue
                if w["ammo"] <= 0 or (m.reload_req and w["ammo"] < ws["mag"]):
                    w["reload"] = ws["reload"]; continue
                if trig and w["cooldown"] <= 0:
                    w["cooldown"] = ws["cooldown"]; w["ammo"] -= 1
                    o, d = s.muzzle(m)
                    sg = math.radians(ws["spread_deg"])
                    yaw = m.aim[0] + s.rng.gauss(0, sg); pit = m.aim[1] + s.rng.gauss(0, sg)
                    d = (math.cos(pit) * math.cos(yaw), math.cos(pit) * math.sin(yaw), math.sin(pit))
                    s.pid += 1
                    s.proj.append(dict(id=s.pid, o=m, pos=list(o), d=d, sp=ws["speed"], left=ws["range"], dmg=ws["damage"], org=o))
            elif k == "laser":
                if s.t < m.short_until:
                    w["charge"] = 0; w["charging"] = False; continue
                full = ws["charge"]
                if w["charging"]:
                    w["charge"] += DT
                    rel = (not trig and w["charge"] >= ws["min_charge"]) or w["charge"] >= full + ws["hold_s"]
                    if rel:
                        frac = ws["min_frac"] + (1 - ws["min_frac"]) * min(1.0, w["charge"] / full)
                        o, d = s.muzzle(m)
                        h = s.ray_hit(o, d, ws["range"], m)
                        if h: s.damage(h[0], ws["damage"] * frac, "laser", m, o)
                        m.shots_l = getattr(m, "shots_l", 0) + 1; m.hits_l = getattr(m, "hits_l", 0) + (1 if h else 0)
                        w["charge"] = 0; w["charging"] = False; w["cooldown"] = ws["cooldown"]
                elif trig and w["cooldown"] <= 0:
                    w["charging"] = True; w["charge"] = 0.0
            elif k == "lightning":
                if trig and w["cooldown"] <= 0 and m.energy >= ws["energy"]:
                    w["cooldown"] = ws["cooldown"]; m.energy -= ws["energy"]; m.last_spend = s.t
                    o, _ = s.muzzle(m)
                    best = None
                    for q in s.mechs:
                        if not q.alive or q is m: continue
                        dq = hd(q.pos, m.pos)
                        if dq > ws["range"]: continue
                        if abs(wrap(math.atan2(q.pos[1] - m.pos[1], q.pos[0] - m.pos[0]) - m.aim[0])) > math.radians(ws["cone_deg"]) / 2: continue
                        if not s.W.los(o[0], o[1], o[2], q.pos[0], q.pos[1], q.pos[2] + 4.5): continue
                        if best is None or dq < best[1]: best = (q, dq)
                    if best:
                        s.damage(best[0], ws["damage"], "lightning", m, o)
                        best[0].short_until = s.t + ws["short_s"] if any(x and x["kind"] == "laser" for x in best[0].weapons) else best[0].short_until
            elif k == "mortar":
                pass

    def proj_step(s):
        keep = []
        for p in s.proj:
            stepd = p["sp"] * DT
            o = p["pos"]; d = p["d"]
            h = s.ray_hit(o, d, stepd, p["o"])
            if h:
                s.damage(h[0], p["dmg"], "gun", p["o"], p["org"])
                if p["o"] is s.me: s.me.ghits = getattr(s.me, "ghits", 0) + 1
                continue
            e = [o[0] + d[0] * stepd, o[1] + d[1] * stepd, o[2] + d[2] * stepd]
            if not s.W.los(o[0], o[1], o[2], e[0], e[1], e[2], 2.0) or s.W.ground(e[0], e[1]) > e[2]:
                continue
            # crates
            hitc = None
            for cid, c in s.crates.items():
                if abs(c["pos"][0] - e[0]) < 8 and abs(c["pos"][1] - e[1]) < 8:
                    rx, ry = c["pos"][0] - o[0], c["pos"][1] - o[1]
                    tp = max(0, min(stepd, rx * d[0] + ry * d[1]))
                    if math.hypot(o[0] + d[0] * tp - c["pos"][0], o[1] + d[1] * tp - c["pos"][1]) < 1.7 and abs(o[2] + d[2] * tp - c["pos"][2] - 1.2) < 1.6:
                        hitc = cid; break
            if hitc is not None:
                c = s.crates[hitc]; c["hp"] -= p["dmg"]
                if c["hp"] <= 0:
                    del s.crates[hitc]
                    s.cubes[s.next_cube] = list(c["pos"]); s.next_cube += 1
                    if s.rng.random() < 0.5:
                        s.pickups[s.next_pick] = (s.rng.choice(["mortar", "laser", "lightning"]), list(c["pos"])); s.next_pick += 1
                continue
            p["pos"] = e; p["left"] -= stepd
            if p["left"] > 0: keep.append(p)
        s.proj = keep

    # ---- vision
    def sees(s, a, b_pos, top=9.0):
        d = hd(a.pos, b_pos)
        ez = a.pos[2] + 8 * a.scale()
        if d > 200 + a.st.get("vision_bonus", 0): return False
        if d > 45 + a.st.get("near_range_bonus", 0):
            if abs(wrap(math.atan2(b_pos[1] - a.pos[1], b_pos[0] - a.pos[0]) - a.aim[0])) > math.radians(60): return False
        W = s.W
        return W.los(a.pos[0], a.pos[1], ez, b_pos[0], b_pos[1], b_pos[2] + top * 0.5) or W.los(a.pos[0], a.pos[1], ez, b_pos[0], b_pos[1], b_pos[2] + top)

    # ---- enemy AI
    def hunters(s):
        alive = [m for m in s.order if m.alive]
        if s.t > LATE_T: return alive
        n = 1 + s.kills * HUNT_PER_KILL
        return alive[:int(n)]

    def ai(s, m, hunting, zone):
        me = s.me; D = DIFF[m.diff]
        t = s.t
        m.jump = False; m.boost = False; m.fire = [False, False]; m.reload_req = False
        d = hd(m.pos, me.pos)
        los = me.alive and d < 230 and s.W.los(m.pos[0], m.pos[1], m.pos[2] + 8, me.pos[0], me.pos[1], me.pos[2] + 4.5)
        if los: s.enemy_knows = t; s.last_known = list(me.pos)
        # passive growth
        if t > m.cube_t and m.cubes < 8:
            m.cubes += 1; m.cube_t = t + s.rng.uniform(35, 80); m.recompute(s.cfg)
        if m.sec_kind and t > m.sec_t and m.weapons[1] is None:
            m.weapons[1] = m.mk(m.sec_kind)
        nc, nr = zone["next_center"], zone["next_radius"]
        outside = hd(m.pos, nc) > max(nr - 15, nr * 0.6)
        goal = None
        engage = me.alive and los and (hunting or d < NONHUNT_R)
        if outside and (not engage or hd(m.pos, zone["center"]) > zone["radius"] - 10):
            goal = (nc[0] + (m.pos[0] - nc[0]) * 0.4, nc[1] + (m.pos[1] - nc[1]) * 0.4); engage_move = False
        else:
            engage_move = engage
        if engage:
            # aim with lead + persistent error
            if t > m.err_t:
                m.err = [s.rng.gauss(0, D["sig"]), s.rng.gauss(0, D["sig"] * 0.5)]; m.err_t = t + s.rng.uniform(0.2, 0.5)
            gsp = m.weapons[0]["stats"]["speed"]
            tt = d / gsp * D["lead"]
            px, py, pz = me.pos[0] + me.vel[0] * tt, me.pos[1] + me.vel[1] * tt, me.pos[2] + 4.5
            yaw = math.atan2(py - m.pos[1], px - m.pos[0]); pit = math.atan2(pz - (m.pos[2] + 7), max(d, 1))
            m.cmd_aim = [yaw + m.err[0], pit + m.err[1]]
            if t > m.burst_t:
                m.burst_on = s.rng.random() < D["duty"]; m.burst_t = t + s.rng.uniform(0.6, 1.4)
            if abs(wrap(m.aim[0] - yaw)) < 0.12 and d < 225 and m.burst_on: m.fire[0] = True
            w1 = m.weapons[1]
            if w1:
                if w1["kind"] == "lightning" and d < 43: m.fire[1] = True
                if w1["kind"] == "laser":
                    m.fire[1] = (w1["charge"] < w1["stats"]["charge"]) if w1["charging"] else (w1["cooldown"] <= 0 and d < 260)
        else:
            if hunting and s.last_known:
                m.cmd_aim = [math.atan2(s.last_known[1] - m.pos[1], s.last_known[0] - m.pos[0]), 0]
            elif math.hypot(m.vel[0], m.vel[1]) > 1:
                m.cmd_aim = [math.atan2(m.vel[1], m.vel[0]), 0]
            if m.weapons[0]["ammo"] < 6: m.reload_req = True
        if engage_move:
            if t > m.strafe_t:
                m.strafe = s.rng.choice((-1, 1)); m.strafe_t = t + s.rng.uniform(1.0, 2.5)
            to = math.atan2(me.pos[1] - m.pos[1], me.pos[0] - m.pos[0])
            pref = m.pref if hunting else 90
            w1 = m.weapons[1]
            if w1 and w1["kind"] == "lightning" and hunting: pref = 30
            rad = 1.5 if d > pref + 10 else (-0.7 if d < pref - 10 else 0)
            a = to + m.strafe * math.pi / 2
            mx, my = math.cos(a) + rad * math.cos(to), math.sin(a) + rad * math.sin(to)
            n = math.hypot(mx, my) or 1
            m.move = [mx / n, my / n]
            m.boost = hunting and d > pref + 40 and m.energy > 40
            return
        if goal is None and hunting and s.last_known:
            goal = (s.last_known[0], s.last_known[1])
            if hd(m.pos, goal) < 15 and not los:
                goal = None; 
                if s.rng.random() < 0.02: s.last_known = list(me.pos)  # squad re-acquires eventually
            m.boost = m.energy > 60
        if goal is None:
            if m.goal is None or hd(m.pos, m.goal) < 10 or hd(m.goal, nc) > nr * 0.8:
                m.goal = s.free_point(nc, max(nr * 0.7, 5))
            goal = m.goal
        if m.path is None or t - m.path_t > 2.5 or hd(m.path_goal, goal) > 15:
            m.path = s.W.astar(m.pos[0], m.pos[1], goal[0], goal[1], 2500); m.path_t = t + s.rng.uniform(0, 0.5); m.path_goal = goal
        p = m.path
        tg = goal
        if p:
            while len(p) > 1 and hd(m.pos, p[0]) < 5: p.pop(0)
            tg = p[0] if len(p) > 1 or hd(p[0], goal) > 4 else goal
        dd = hd(m.pos, tg)
        m.move = [(tg[0] - m.pos[0]) / dd, (tg[1] - m.pos[1]) / dd] if dd > 1 else [0, 0]
        if math.hypot(m.vel[0], m.vel[1]) < 1.5 and dd > 3:
            m.stk = getattr(m, "stk", 0) + 1
            if m.stk > 16: m.jump = True; m.boost = True
            if m.stk > 60: m.path = None; m.stk = 0
        else: m.stk = 0

    # ---- bot io
    def make_obs(s, zone):
        me = s.me
        ms = []
        for m in s.mechs[1:]:
            if not m.alive: continue
            if s.sees(me, m.pos, 9 * m.scale()): s.seen[m.id] = s.t
            if s.t - s.seen.get(m.id, -99) <= 5.0:
                ms.append(dict(id=m.id, kind="mech", pos=[round(x, 2) for x in m.pos], vel=[round(x, 2) for x in m.vel], aim=[round(m.aim[0], 3), round(m.aim[1], 3)],
                               hp=round(m.hp), max_hp=round(m.max_hp), cubes=m.cubes, weapons=[w["kind"] for w in m.weapons if w],
                               charging=any(w and w["kind"] == "laser" and w.get("charging") for w in m.weapons)))
        wl = []
        for w in me.weapons:
            if not w: wl.append(None); continue
            d = dict(kind=w["kind"], cooldown=round(w["cooldown"], 3), stats=dict(w["stats"]))
            if w["kind"] == "gun": d["ammo"] = w["ammo"]; d["reload"] = round(max(w["reload"], 0), 3)
            if w["kind"] == "laser": d["charge"] = round(w["charge"], 3) if w["charging"] else 0
            wl.append(d)
        st = dict(me.st); st["eye_height"] = 8 * me.scale(); st["muzzle_height"] = 7 * me.scale()
        zc = zone["center"]
        obs = dict(type="obs", tick=s.tick, t=round(s.t, 2), alive=sum(m.alive for m in s.mechs),
                   self=dict(id=0, pos=[round(x, 2) for x in me.pos], vel=[round(x, 2) for x in me.vel], aim=[round(me.aim[0], 4), round(me.aim[1], 4)],
                             hp=round(me.hp), max_hp=round(me.max_hp), energy=round(me.energy), max_energy=me.st["energy"], grounded=me.grounded,
                             cubes=me.cubes, upgrades=list(me.ups), in_zone=hd(me.pos, zc) <= zone["radius"], weapons=wl, stats=st),
                   mechs=ms, projectiles=[],
                   crates=[dict(id=i, pos=c["pos"], hp=c["hp"]) for i, c in s.crates.items() if hd(c["pos"], me.pos) < 200 and s.sees(me, c["pos"], 2.4)],
                   cubes=[dict(id=i, pos=p) for i, p in s.cubes.items() if hd(p, me.pos) < 200 and s.sees(me, p, 1.0)],
                   pickups=[dict(id=i, kind=k, pos=p) for i, (k, p) in s.pickups.items() if hd(p, me.pos) < 200 and s.sees(me, p, 1.0)],
                   sounds=[], beams=[], events=me.events,
                   drops=[dict(id=d["id"], pos=d["pos"], radius=8, lands_in=round(max(0.0, d["land"] - s.t), 2), capture_s=4, progress=round(d["prog"], 2), holder=None) for d in s.drops],
                   zone=zone, offer=me.offer)
        me.events = []
        return obs

    def apply(s, a):
        me = s.me
        me.move = list(a.get("move", [0, 0])); me.boost = bool(a.get("boost")); me.jump = bool(a.get("jump"))
        if "aim" in a: me.cmd_aim = list(a["aim"])
        f = a.get("fire", [False, False]); me.fire = [bool(f[0]), bool(f[1]) if len(f) > 1 else False]
        me.reload_req = bool(a.get("reload"))
        if me.offer and "pick" in a:
            ch = me.offer["choices"][int(a["pick"])]["id"]
            me.ups.append(ch); me.recompute(s.cfg); me.events.append(dict(type="upgrade", id=ch)); me.offer = None
        if "take" in a:
            pid = a["take"]; pk = s.pickups.get(pid)
            if not pk: me.events.append(dict(type="take_failed", id=pid, reason="gone"))
            elif hd(pk[1], me.pos) > 4 or abs(pk[1][2] - me.pos[2]) > 4: me.events.append(dict(type="take_failed", id=pid, reason="range"))
            elif any(w and w["kind"] == pk[0] for w in me.weapons): me.events.append(dict(type="take_failed", id=pid, reason="have"))
            else:
                slot = a.get("drop")
                if slot is None:
                    slot = 0 if me.weapons[0] is None else (1 if me.weapons[1] is None else None)
                if slot is None: me.events.append(dict(type="take_failed", id=pid, reason="full"))
                else:
                    slot = int(slot); old = me.weapons[slot]
                    if old:
                        s.pickups[s.next_pick] = (old["kind"], list(me.pos)); s.next_pick += 1
                    w = me.mk(pk[0]); w["cooldown"] = max(1.0, w["stats"].get("cooldown", 0)); me.weapons[slot] = w
                    del s.pickups[pid]; me.events.append(dict(type="took", id=pid, kind=pk[0], slot=slot))
                    if s.log["first_w"] is None: s.log["first_w"] = (round(s.t, 1), pk[0])

    def offers(s, m):
        th = s.cfg["cubes"]["upgrade_thresholds"]
        while m.thresh_i < len(th) and m.cubes >= th[m.thresh_i]:
            m.thresh_i += 1
            if m is s.me:
                held = {w["kind"] for w in m.weapons if w}
                av = [u for u in s.cfg["upgrades"] if m.ups.count(u["id"]) < u["max"] and (u.get("requires") is None or u["requires"] in held)]
                m.offers.append(dict(id=m.thresh_i, expires=0, choices=[dict(id=u["id"], name=u["name"], desc=u["desc"]) for u in s.rng.sample(av, 3)]))
            else:
                m.ups.append(s.rng.choice(["plating", "armor", "gun_damage", "gun_rate"])); m.recompute(s.cfg)
        if m is s.me and m.offer is None and m.offers:
            m.offer = m.offers.pop(0); m.offer["expires"] = s.t + 5

    def run(s):
        me = s.me; cfg = s.cfg
        s.last_hit = None
        while s.t < cfg["match"]["duration_s"]:
            zone = s.zone_now()
            if s.tick % 2 == 0:
                obs = s.make_obs(zone)
                try:
                    a = s.bot.act(obs)
                except Exception as e:
                    import traceback; traceback.print_exc(); a = {}
                    s.log["exc"] = repr(e)
                s.apply(a)
                md = getattr(s.bot, "mode", "?") + ("+rec" if getattr(s.bot, "recovering", False) else "")
                s.log["modes"][md] = s.log["modes"].get(md, 0) + 1
                if math.hypot(me.move[0], me.move[1]) > 0.3 and math.hypot(me.vel[0], me.vel[1]) < 1.5: s.log["stuck"] += 0.1
                hs = s.hunters()
                hset = {m.id for m in hs}
                for m in s.mechs[1:]:
                    if m.alive: s.ai(m, m.id in hset, zone)
            for m in s.mechs:
                if not m.alive: continue
                s.step_mech(m); s.weapons_step(m)
            s.proj_step()
            dc = cfg["drops"]
            if s.drop_i < len(dc["times"]) and s.t >= dc["times"][s.drop_i]:
                p = s.free_point(zone["next_center"], zone["next_radius"] * dc["spread"])
                s.drops.append(dict(id=20000 + s.drop_i, pos=[p[0], p[1], s.W.ground(*p)], land=s.t + dc["fall_s"], prog=0.0)); s.drop_i += 1
            for d in list(s.drops):
                if s.t < d["land"]: continue
                inside = [m for m in s.mechs if m.alive and hd(m.pos, d["pos"]) <= 8]
                if len(inside) == 1 and inside[0] is me:
                    d["prog"] += DT
                    if d["prog"] >= dc["capture_s"]:
                        me.cubes += dc["cubes"]; me.recompute(cfg); me.hp = me.max_hp; me.energy = me.st["energy"]
                        me.events.append(dict(type="drop", id=d["id"], cubes=me.cubes)); s.drops.remove(d); s.drops_taken += 1
                elif len(inside) != 1 or inside[0] is not me:
                    if not inside or len(inside) == 1: d["prog"] = 0.0
            zc, zr = zone["center"], zone["radius"]
            for m in s.mechs:
                if not m.alive: continue
                inz = hd(m.pos, zc) <= zr
                if not inz:
                    m.hp -= zone["dps"] * DT; m.last_combat = s.t; m.zone_dmg += zone["dps"] * DT
                    if m is me: s.last_hit = ("zone", None, None, 0)
                    if m.hp <= 0: s.kill(m, None, "zone")
                elif s.t - m.last_combat > m.st["regen_delay"]:
                    m.hp = min(m.max_hp, m.hp + m.st["regen_per_s"] * DT)
                # cubes
                if m is me:
                    for cid in [c for c, p in s.cubes.items() if hd(p, m.pos) < 3.5 and abs(p[2] - m.pos[2]) < 4]:
                        del s.cubes[cid]; m.cubes += 1; m.recompute(cfg); m.events.append(dict(type="pickup", cubes=m.cubes))
                s.offers(m)
            s.t += DT; s.tick += 1
            alive = sum(m.alive for m in s.mechs)
            if not me.alive or alive == 1: break
        alive = sum(m.alive for m in s.mechs)
        place = alive + (0 if me.alive else 1)
        if me.alive and alive > 1:  # timeout: rank by hp
            place = 1 + sum(1 for m in s.mechs[1:] if m.alive and m.hp > me.hp)
        return dict(seed=s.seed, score=16 - place, kills=s.kills, t=round(s.t, 1), last=s.last_hit if not me.alive else None,
                    hunters=len(s.hunters()), dealt=round(me.dealt), taken={k: round(v) for k, v in me.taken.items()}, zone=round(me.zone_dmg),
                    stuck=round(s.log["stuck"], 1), first_w=s.log["first_w"], loadout=[w["kind"] for w in me.weapons if w], cubes=me.cubes, drops=s.drops_taken,
                    ups=me.ups, modes=s.log["modes"], gun=(getattr(me, "ghits", 0)), laser=(getattr(me, "hits_l", 0), getattr(me, "shots_l", 0)), exc=s.log.get("exc"))


LATE_T = float(os.environ.get("SIM_LATE", 255.0))
NONHUNT_R = float(os.environ.get("SIM_NHR", 50))
HUNT_PER_KILL = float(os.environ.get("SIM_HPK", 0.5))


def one(args):
    botdir, seed, v = args
    sys.stderr = open(os.devnull, "w")
    return Sim(botdir, seed, v).run()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", default=ROOT); ap.add_argument("--n", type=int, default=16); ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--v", action="store_true"); ap.add_argument("--j", type=int, default=8)
    a = ap.parse_args()
    t0 = time.time()
    jobs = [(os.path.abspath(a.bot), a.seed + i, a.v) for i in range(a.n)]
    if a.n == 1:
        res = [Sim(*jobs[0]).run()]
    else:
        with Pool(a.j) as p: res = p.map(one, jobs)
    for r in res:
        if a.v or a.n <= 24: print(json.dumps(r))
    n = len(res)
    causes = {}
    for r in res:
        k = (r["last"][0] if r["last"] else "survived"); causes[k] = causes.get(k, 0) + 1
    print(f"\nMEAN score {sum(r['score'] for r in res)/n:.2f}  kills {sum(r['kills'] for r in res)/n:.2f}  death_t {sum(r['t'] for r in res)/n:.0f}s  "
          f"cubes {sum(r['cubes'] for r in res)/n:.1f} drops {sum(r['drops'] for r in res)/n:.2f} zone_dmg {sum(r['zone'] for r in res)/n:.0f}  stuck {sum(r['stuck'] for r in res)/n:.1f}s  causes {causes}  ({time.time()-t0:.0f}s)")
