"""Constraint-driven, double-sided placement engine.

The LLM supplies *intent* (floorplan regions per block, edge connectors, `near` constraints for
decoupling/crystals, `side` permissions, fixed parts); this engine produces legal coordinates:

  1. anchors: fixed / edge-constrained parts (connectors face outward)
  2. rigid cluster macros: parts with tight `near` constraints are packed around their hub
     (buck IC + L/C/FB, SoC + decaps + crystal) - members may go on the *other* side (e.g. 0402
     decoupling directly under a QFN, avoiding its thermal vias)
  3. simulated annealing over (x, y, orientation) where orientation = rotation + side, minimising
        wirelength (HPWL, big/power nets down-weighted) + near-constraint violation
      + same-side courtyard overlap (+ through-hole parts block both sides, plated holes of SMD parts block
        the opposite side locally) + region/board containment
  4. spiral legalisation, then legal-only refinement with targeted jumps

Orientation code: o = rot (top) or 1000 + rot (bottom).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

import numpy as np

from . import libs
from .ir import Placement, Project

ROTS = (0.0, 90.0, 180.0, 270.0)
BOT = 1000.0


def o_side(o: float) -> int:
    return 1 if o >= BOT else 0


def o_rot(o: float) -> float:
    return o - BOT if o >= BOT else o


def o_make(rot: float, side: str) -> float:
    return (BOT if side == "bottom" else 0.0) + (rot % 360)


@dataclass
class Item:
    ref: str
    w: dict          # orient -> (x1,y1,x2,y2) local courtyard bbox incl. margin
    pins: dict       # orient -> {pin_number: (dx,dy)}
    holes: dict      # orient -> list of local rects of plated holes (block the other side)
    both: bool       # through-hole part: occupies both sides
    fixed: bool
    region: tuple | None
    orients: tuple
    area: float


def _bbox_with_margin(bb, m):
    return (bb[0] - m, bb[1] - m, bb[2] + m, bb[3] + m)


def _allowed_sides(d, ref) -> str:
    from fnmatch import fnmatchcase
    comp = d.components[ref]
    out = "top"
    for c in d.constraints:
        if c.kind != "side":
            continue
        if (c.ref and fnmatchcase(ref, c.ref) and (not c.block or c.block == comp.block)) or \
                (not c.ref and c.block and c.block == comp.block):
            out = c.side or "top"
    return out


def place(prj: Project, effort: float = 1.0, seed: int = 1, margin: float = 0.2, only_unlocked: bool = True,
          iterations: int | None = None, macros: bool = True, macro_max_mm: float = 4.5,
          fanout_halo_mm: float = 1.0) -> dict:
    d, L = prj.design, prj.layout
    rnd = random.Random(seed)
    W, H = d.board.width, d.board.height
    edge_clear = 0.5
    cons = d.constraints
    regions = {c.block: tuple(c.rect) for c in cons if c.kind == "region" and c.block and c.rect}
    keepouts = [tuple(c.rect) for c in cons if c.kind == "keepout" and c.rect and "no_pour" not in (c.note or "") and
                (not c.layers or "F.Cu" in c.layers or "F.CrtYd" in (c.layers or []))]   # no_pour = routing/pour only
    for mh in d.board.mounting_holes:
        r = (mh[2] if len(mh) > 2 else d.board.mh_drill) / 2 + 1.6
        keepouts.append((mh[0] - r, mh[1] - r, mh[0] + r, mh[1] + r))

    items: dict[str, Item] = {}
    for ref, c in d.components.items():
        allow = _allowed_sides(d, ref)
        sides = {"top": ["top"], "bottom": ["bottom"], "any": ["top", "bottom"]}.get(allow, ["top"])
        pl0 = L.placement.get(ref)
        if pl0 is not None and pl0.locked and pl0.side not in sides:
            sides = [pl0.side]
        wd, pd, hd = {}, {}, {}
        both = False
        for sd in ("top", "bottom"):
            fp = libs.get_footprint_side(c.footprint, sd)
            tht = [p for p in fp.pads if p.kind in ("thru_hole", "np_thru_hole")]
            both = len(tht) >= max(2, len(fp.pads) // 2)
            for r in ROTS:
                o = o_make(r, sd)
                wd[o] = _bbox_with_margin(libs.bbox_rot(fp.courtyard, r), margin)
                pd[o] = {}
                for p in fp.pads:
                    if p.number and p.number not in pd[o]:
                        pd[o][p.number] = libs.rot(p.x, p.y, r)
                hd[o] = []
                if not both:
                    for p in tht:
                        hx, hy = libs.rot(p.x, p.y, r)
                        rr = max(p.w, p.h, p.drill) / 2 + 0.15
                        hd[o].append((hx - rr, hy - rr, hx + rr, hy + rr))
        orients = tuple(o_make(r, sd) for sd in sides for r in ROTS)
        fixed = bool(pl0 and pl0.locked)
        x1, y1, x2, y2 = wd[0.0]
        items[ref] = Item(ref, wd, pd, hd, both, fixed, regions.get(c.block), orients, (x2 - x1) * (y2 - y1))

    pos: dict[str, list[float]] = {}
    # ---------- anchors
    for c in cons:
        if c.ref not in items:
            continue
        it = items[c.ref]
        if c.kind == "fixed":
            pos[c.ref] = [c.x, c.y, o_make(c.rot or 0.0, "top")]
            it.fixed = True
        elif c.kind == "edge":
            rot = c.rot if c.rot is not None else {"bottom": 0.0, "top": 180.0, "left": 270.0, "right": 90.0}[c.side]
            x1, y1, x2, y2 = it.w[rot]
            off = c.offset
            ov = c.max_mm or 0.0      # allowed overhang beyond edge (connectors)
            if c.side == "left":
                pos[c.ref] = [-x1 - ov + margin, off if off is not None else H / 2, rot]
            elif c.side == "right":
                pos[c.ref] = [W - x2 + ov - margin, off if off is not None else H / 2, rot]
            elif c.side == "top":
                pos[c.ref] = [off if off is not None else W / 2, -y1 - ov + margin, rot]
            else:
                pos[c.ref] = [off if off is not None else W / 2, H - y2 + ov - margin, rot]
            it.fixed = True
    for ref, it in items.items():
        if it.fixed and ref not in pos:
            pl = L.placement[ref]
            pos[ref] = [pl.x, pl.y, o_make(pl.rot, pl.side)]

    # ---------- nets
    nets = d.nets()
    net_w = {}
    for n, nodes in nets.items():
        if len(nodes) < 2:
            continue
        cls = d.netclass_of(n)
        big = len(nodes) > 12 or n.upper().startswith("GND")
        net_w[n] = 0.05 if big else (0.5 if cls.track >= 0.3 else 1.0)
    part_nets: dict[str, list[str]] = {r: [] for r in items}
    for n in net_w:
        for ref, _ in nets[n]:
            if n not in part_nets[ref]:
                part_nets[ref].append(n)
    # near constraints: (ref, target_ref, target_pin|None, my_pin|None, max_mm)
    nears = []
    for c in cons:
        if c.kind == "near" and c.ref in items and c.target:
            if "." in c.target:
                tref, tpin = c.target.split(".", 1)
                if tref in d.components and tpin not in items[tref].pins[0.0]:
                    sym = libs.get_symbol(d.components[tref].symbol)
                    nums = [p.number for p in sym.pins if p.name == tpin]
                    tpin = nums[0] if nums else None
            else:
                tref, tpin = c.target, None
            if tref in items:
                mypin = None
                if tpin:
                    tnet = d.components[tref].pins.get(tpin)
                    for pn, nn in d.components[c.ref].pins.items():
                        if nn == tnet and nn:
                            mypin = pn
                            break
                nears.append((c.ref, tref, tpin, mypin, c.max_mm or 3.0))
    near_by_ref: dict[str, list[int]] = {r: [] for r in items}
    for i, nr in enumerate(nears):
        near_by_ref[nr[0]].append(i)
        near_by_ref[nr[1]].append(i)

    # ---------- seed
    movable = [r for r, it in items.items() if not it.fixed]
    blocks: dict[str, list[str]] = {}
    for r in movable:
        blocks.setdefault(d.components[r].block, []).append(r)
    for b, refs_ in blocks.items():
        reg = regions.get(b, (edge_clear, edge_clear, W - edge_clear, H - edge_clear))
        refs_.sort(key=lambda r: -items[r].area)
        cx, cy = (reg[0] + reg[2]) / 2, (reg[1] + reg[3]) / 2
        for i, r in enumerate(refs_):
            if r in pos:
                continue
            o0 = items[r].orients[0]
            if i == 0:
                pos[r] = [cx, cy, o0]
            else:
                pos[r] = [rnd.uniform(reg[0], reg[2]), rnd.uniform(reg[1], reg[3]), rnd.choice(items[r].orients)]
    for (ref, tref, tpin, mypin, mx) in nears:
        if ref in movable and tref in pos:
            tx, ty = _pin_xy(items, pos, tref, tpin)
            pos[ref] = [tx + rnd.uniform(-2, 2), ty + rnd.uniform(-2, 2), pos[ref][2]]
    refs = list(items)
    idx = {r: i for i, r in enumerate(refs)}
    N = len(refs)
    rects = np.zeros((N, 4))
    side_a = np.zeros(N, dtype=np.int8)
    both_a = np.array([items[r].both for r in refs], dtype=bool)
    holes_abs: dict[int, np.ndarray] = {}

    def upd_rect(r):
        x, y, o = pos[r]
        b = items[r].w[o]
        i = idx[r]
        rects[i] = (x + b[0], y + b[1], x + b[2], y + b[3])
        side_a[i] = o_side(o)
        hl = items[r].holes[o]
        if hl:
            holes_abs[i] = np.array([(x + h[0], y + h[1], x + h[2], y + h[3]) for h in hl])
        else:
            holes_abs.pop(i, None)

    for r in refs:
        upd_rect(r)

    def ov_area(i, rect=None) -> float:
        a = rects[i] if rect is None else rect
        si, bi = side_a[i], both_a[i]
        m = both_a | bi | (side_a == si)
        ox = np.minimum(rects[:, 2], a[2]) - np.maximum(rects[:, 0], a[0])
        oy = np.minimum(rects[:, 3], a[3]) - np.maximum(rects[:, 1], a[1])
        ov = np.clip(ox, 0, None) * np.clip(oy, 0, None) * m
        ov[i] = 0.0
        s = float(ov.sum())
        if not bi:
            for j, hr in holes_abs.items():      # other parts' plated holes on my side
                if j == i or side_a[j] == si:
                    continue
                hx = np.minimum(hr[:, 2], a[2]) - np.maximum(hr[:, 0], a[0])
                hy = np.minimum(hr[:, 3], a[3]) - np.maximum(hr[:, 1], a[1])
                s += float((np.clip(hx, 0, None) * np.clip(hy, 0, None)).sum())
            if i in holes_abs:                    # my plated holes vs parts on the other side
                for h in holes_abs[i]:
                    mm = (side_a != si) & ~both_a
                    mm[i] = False
                    hx = np.minimum(rects[:, 2], h[2]) - np.maximum(rects[:, 0], h[0])
                    hy = np.minimum(rects[:, 3], h[3]) - np.maximum(rects[:, 1], h[1])
                    s += float((np.clip(hx, 0, None) * np.clip(hy, 0, None) * mm).sum())
        for k in keepouts:
            kx = min(k[2], a[2]) - max(k[0], a[0])
            ky = min(k[3], a[3]) - max(k[1], a[1])
            if kx > 0 and ky > 0:
                s += kx * ky
        return s

    def in_board(a) -> bool:
        return not (a[0] < edge_clear - 1e-6 or a[1] < edge_clear - 1e-6 or a[2] > W - edge_clear + 1e-6
                    or a[3] > H - edge_clear + 1e-6)

    def legal(r) -> bool:
        i = idx[r]
        return in_board(rects[i]) and ov_area(i) <= 1e-9

    # ---------- cluster macros
    members: dict[str, list] = {}       # hub -> [(ref, dx, dy, orient)]
    member_of: dict[str, str] = {}
    if macros:
        parent: dict[str, str] = {}
        for (ref, tref, tpin, mypin, mx) in nears:
            if mx <= macro_max_mm and ref != tref and ref not in parent and not items[ref].fixed:
                parent[ref] = tref

        def root(r):
            seen = set()
            while r in parent and r not in seen:
                seen.add(r)
                r = parent[r]
            return r
        groups: dict[str, list[str]] = {}
        for r in parent:
            h = root(r)
            if h != r:
                groups.setdefault(h, []).append(r)
        for hub, mem in groups.items():
            if not items[hub].fixed:           # canonical hub orientation -> identical cells for identical blocks
                pos[hub][2] = items[hub].orients[0]
                upd_rect(hub)
            hx, hy, ho = pos[hub]
            hi = idx[hub]
            placed = {hub}
            hub_fine = len(libs.get_footprint(d.components[hub].footprint).pads) >= 32
            # local occupancy: (rect, side, both)
            hrect = rects[hi].copy()
            if hub_fine:
                hrect_halo = hrect + np.array([-fanout_halo_mm, -fanout_halo_mm, fanout_halo_mm, fanout_halo_mm])
            else:
                hrect_halo = hrect
            local = [(hrect_halo, int(side_a[hi]), bool(both_a[hi]))]
            hub_holes = holes_abs.get(hi, np.zeros((0, 4)))

            def dep(r):
                k = 0
                while r != hub and r in parent and k < 20:
                    r = parent[r]; k += 1
                return k
            order = sorted(mem, key=lambda r: (dep(r), min([nr[4] for nr in nears if nr[0] == r] + [99]), -items[r].area))
            for m in order:
                my_nears = [nr for nr in nears if nr[0] == m and (nr[1] in placed)]
                if not my_nears:
                    continue
                tgts = [(_pin_xy(items, pos, nr[1], nr[2]), nr[3], nr[4]) for nr in my_nears]
                tx0, ty0 = tgts[0][0]
                best = None
                for rad in np.arange(0.0, 9.0, 0.15):
                    nang = max(8, int(2 * math.pi * max(rad, 0.15) / 0.3))
                    for k in range(nang):
                        a = 2 * math.pi * k / nang
                        for o in items[m].orients:
                            b = items[m].w[o]
                            s_ = o_side(o)
                            off = items[m].pins[o].get(tgts[0][1], (0.0, 0.0)) if tgts[0][1] else (0.0, 0.0)
                            cx = round((tx0 + rad * math.cos(a) - off[0]) / 0.1) * 0.1
                            cy = round((ty0 + rad * math.sin(a) - off[1]) / 0.1) * 0.1
                            rr = (cx + b[0], cy + b[1], cx + b[2], cy + b[3])
                            if not in_board(rr):
                                continue
                            clash = False
                            for (lr, ls, lb) in local:
                                if (lb or items[m].both or ls == s_) and \
                                        min(lr[2], rr[2]) - max(lr[0], rr[0]) > 1e-6 and min(lr[3], rr[3]) - max(lr[1], rr[1]) > 1e-6:
                                    clash = True
                                    break
                            if not clash and s_ != side_a[hi] and len(hub_holes):
                                hxx = np.minimum(hub_holes[:, 2], rr[2]) - np.maximum(hub_holes[:, 0], rr[0])
                                hyy = np.minimum(hub_holes[:, 3], rr[3]) - np.maximum(hub_holes[:, 1], rr[1])
                                clash = bool(((hxx > 1e-6) & (hyy > 1e-6)).any())
                            if clash:
                                continue
                            cst = 0.0
                            for (tp, mp, mxx) in tgts:
                                o2 = items[m].pins[o].get(mp, (0.0, 0.0)) if mp else (0.0, 0.0)
                                dd = math.hypot(cx + o2[0] - tp[0], cy + o2[1] - tp[1])
                                cst += dd + 10 * max(0.0, dd - mxx)
                            cst += 0.3 * s_          # slight preference for the top side
                            cst += 0.15 * (o_rot(o) in (180.0, 270.0))   # readable, consistent orientations
                            if best is None or cst < best[0]:
                                best = (cst, cx, cy, o)
                    if best is not None and rad > best[0] + 0.5:
                        break
                if best is None:
                    continue
                _, cx, cy, o = best
                pos[m] = [cx, cy, o]
                upd_rect(m)
                local.append((rects[idx[m]].copy(), int(side_a[idx[m]]), bool(both_a[idx[m]])))
                placed.add(m)
            ms = [(m, pos[m][0] - hx, pos[m][1] - hy, pos[m][2]) for m in order if m in placed and m != hub]
            if ms:
                members[hub] = ms
                for m, *_ in ms:
                    member_of[m] = hub

    def move_members(h):
        hx, hy, _ = pos[h]
        for m, dx, dy, o in members.get(h, ()):
            pos[m] = [hx + dx, hy + dy, o]
            upd_rect(m)

    def net_cost(n):
        xs, ys = [], []
        for ref, pin in nets[n]:
            x, y = _pin_xy(items, pos, ref, pin)
            xs.append(x); ys.append(y)
        return net_w[n] * ((max(xs) - min(xs)) + (max(ys) - min(ys)))

    def near_cost(i):
        ref, tref, tpin, mypin, mx = nears[i]
        tx, ty = _pin_xy(items, pos, tref, tpin)
        mx_, my_ = _pin_xy(items, pos, ref, mypin)
        dd = math.hypot(tx - mx_, ty - my_)
        ex = max(0.0, dd - mx)
        return 40.0 * ex * ex + 8.0 * ex + 0.6 * dd

    def contain_cost(r):
        a = rects[idx[r]]
        s = max(0, edge_clear - a[0]) + max(0, edge_clear - a[1]) + max(0, a[2] - (W - edge_clear)) + max(0, a[3] - (H - edge_clear))
        reg = items[r].region
        if reg:
            s += 0.3 * (max(0, reg[0] - a[0]) + max(0, reg[1] - a[1]) + max(0, a[2] - reg[2]) + max(0, a[3] - reg[3]))
        return s

    W_OV, W_CT = 30.0, 30.0
    BOTTOM_COST = 0.4      # mild preference for top-side assembly

    def part_cost(r, ov_scale):
        c = sum(net_cost(n) for n in part_nets[r])
        c += sum(near_cost(i) for i in near_by_ref[r])
        c += ov_scale * W_OV * ov_area(idx[r]) + W_CT * contain_cost(r) * 10
        c += BOTTOM_COST * side_a[idx[r]]
        c += 0.3 * (o_rot(pos[r][2]) in (180.0, 270.0))     # prefer 0/90: rows read consistently
        return c

    sa_movable = [r for r in movable if r not in member_of]
    total_iters = iterations or int(4000 * max(1, len(movable)) * effort)
    total_iters = min(total_iters, 900_000)
    T0, T1 = max(W, H) * 0.25, 0.01
    accepted = 0
    if sa_movable:
        for k in range(total_iters):
            frac = k / total_iters
            T = T0 * (T1 / T0) ** frac
            ov_scale = 0.3 + 30.0 * frac * frac
            r = rnd.choice(sa_movable)
            if r in members:      # rigid macro: translate hub + members together
                grp = [r] + [m for m, *_ in members[r]]
                olds = {g: list(pos[g]) for g in grp}
                before = sum(part_cost(g, ov_scale) for g in grp)
                s_ = max(0.05, T * 0.6)
                pos[r][0] += rnd.gauss(0, s_)
                pos[r][1] += rnd.gauss(0, s_)
                upd_rect(r)
                move_members(r)
                after = sum(part_cost(g, ov_scale) for g in grp)
                if after <= before or rnd.random() < math.exp(-(after - before) / max(T, 1e-9)):
                    accepted += 1
                else:
                    for g in grp:
                        pos[g] = olds[g]
                        upd_rect(g)
                continue
            old = list(pos[r])
            before = part_cost(r, ov_scale)
            mv = rnd.random()
            if mv < 0.15:
                pos[r][2] = rnd.choice(items[r].orients)
            elif mv < 0.25 and len(sa_movable) > 1:
                r2 = rnd.choice(sa_movable)
                if r2 == r or r2 in members:
                    continue
                old2 = list(pos[r2])
                before += part_cost(r2, ov_scale)
                pos[r][0], pos[r][1], pos[r2][0], pos[r2][1] = old2[0], old2[1], old[0], old[1]
                upd_rect(r); upd_rect(r2)
                after = part_cost(r, ov_scale) + part_cost(r2, ov_scale)
                if after <= before or rnd.random() < math.exp(-(after - before) / max(T, 1e-9)):
                    accepted += 1
                else:
                    pos[r], pos[r2] = old, old2
                    upd_rect(r); upd_rect(r2)
                continue
            else:
                s = max(0.05, T * 0.6)
                pos[r][0] += rnd.gauss(0, s)
                pos[r][1] += rnd.gauss(0, s)
            upd_rect(r)
            after = part_cost(r, ov_scale)
            if after <= before or rnd.random() < math.exp(-(after - before) / max(T, 1e-9)):
                accepted += 1
            else:
                pos[r] = old
                upd_rect(r)
    # ---------- snap to 0.05 mm grid, then legalise
    for r in movable:
        if r in member_of:
            continue
        pos[r][0] = round(pos[r][0] / 0.1) * 0.1
        pos[r][1] = round(pos[r][1] / 0.1) * 0.1
        upd_rect(r)
        if r in members:
            move_members(r)

    def legalise(order_refs) -> int:
        moved = 0
        for r in sorted(order_refs, key=lambda r: items[r].area):
            if legal(r):
                continue
            x0, y0, o0 = pos[r]
            best = None
            for rad in np.arange(0.1, max(W, H), 0.1):
                nstep = max(8, int(2 * math.pi * rad / 0.1))
                for k in range(nstep):
                    a = 2 * math.pi * k / nstep
                    for o in (o0,) + tuple(x for x in items[r].orients if x != o0):
                        pos[r] = [x0 + rad * math.cos(a), y0 + rad * math.sin(a), o]
                        upd_rect(r)
                        if legal(r):
                            best = list(pos[r])
                            break
                    if best:
                        break
                if best:
                    break
            pos[r] = best if best else [x0, y0, o0]
            upd_rect(r)
            moved += bool(best)
        return moved

    free = [r for r in movable if r not in member_of and r not in members]
    moved = legalise(free)
    moved += legalise([r for r in movable if r in member_of or r in members])

    # ---------- phase 3: legal-only greedy refinement
    def soft_cost(r):
        c = sum(net_cost(n) for n in part_nets[r]) + sum(near_cost(i) for i in near_by_ref[r])
        return c + W_CT * contain_cost(r) + BOTTOM_COST * side_a[idx[r]] + 0.3 * (o_rot(pos[r][2]) in (180.0, 270.0))

    refine_iters = int(len(movable) * 400 * max(1.0, effort))
    improved = 0
    for k in range(refine_iters):
        if not movable:
            break
        r = rnd.choice(movable)
        if r in members:
            continue
        old = list(pos[r])
        before = soft_cost(r)
        tgt = None
        if near_by_ref[r] and rnd.random() < 0.6:
            ni = rnd.choice(near_by_ref[r])
            ref_, tref, tpin, mypin, mx = nears[ni]
            if ref_ == r:
                tgt = _pin_xy(items, pos, tref, tpin)
        step = rnd.choice((0.05, 0.1, 0.25, 0.5, 1.0, 2.0))
        if tgt is not None and rnd.random() < 0.35:
            best_c, best_p = before, None
            for _ in range(24):
                rad = rnd.uniform(0.3, 6.0)
                a = rnd.uniform(0, 2 * math.pi)
                pos[r] = [round((tgt[0] + rad * math.cos(a)) / 0.1) * 0.1,
                          round((tgt[1] + rad * math.sin(a)) / 0.1) * 0.1, rnd.choice(items[r].orients)]
                upd_rect(r)
                if legal(r):
                    c = soft_cost(r)
                    if c < best_c - 1e-6:
                        best_c, best_p = c, list(pos[r])
            pos[r] = best_p if best_p else old
            improved += bool(best_p)
            upd_rect(r)
            continue
        if tgt is not None:
            dx, dy = tgt[0] - pos[r][0], tgt[1] - pos[r][1]
            dd = math.hypot(dx, dy) or 1.0
            pos[r][0] += dx / dd * step + rnd.gauss(0, step * 0.3)
            pos[r][1] += dy / dd * step + rnd.gauss(0, step * 0.3)
        else:
            pos[r][0] += rnd.gauss(0, step)
            pos[r][1] += rnd.gauss(0, step)
        if rnd.random() < 0.15:
            pos[r][2] = rnd.choice(items[r].orients)
        pos[r][0] = round(pos[r][0] / 0.1) * 0.1
        pos[r][1] = round(pos[r][1] / 0.1) * 0.1
        upd_rect(r)
        if legal(r) and soft_cost(r) < before - 1e-6:
            improved += 1
        else:
            pos[r] = old
            upd_rect(r)
    for r, (x, y, o) in pos.items():
        lk = L.placement.get(r).locked if r in L.placement else False
        L.placement[r] = Placement(x=round(x, 4), y=round(y, 4), rot=o_rot(o), side="bottom" if o_side(o) else "top",
                                   locked=lk or items[r].fixed)
    # report
    hpwl = float(sum(net_cost(n) / net_w[n] for n in net_w if net_w[n] >= 0.5))
    viol = []
    excess = 0.0
    for (ref, tref, tpin, mypin, mx) in nears:
        tx, ty = _pin_xy(items, pos, tref, tpin)
        ax, ay = _pin_xy(items, pos, ref, mypin)
        dd = math.hypot(tx - ax, ty - ay)
        excess += max(0.0, dd - mx)
        if dd > mx:
            viol.append(f"{ref}->{tref}{'.' + tpin if tpin else ''}: {dd:.1f}>{mx}")
    overl = [r for r in refs if ov_area(idx[r]) > 0.01]
    out_of = [r for r in refs if contain_cost(r) > 0.01 and not items[r].fixed]
    util_top = sum(items[r].area for r in refs if side_a[idx[r]] == 0 or both_a[idx[r]]) / (W * H)
    util_bot = sum(items[r].area for r in refs if side_a[idx[r]] == 1 or both_a[idx[r]]) / (W * H)
    L.history.append({"op": "place", "iters": total_iters, "hpwl": round(hpwl, 1)})
    return {"placed": len(pos), "movable": len(movable), "iterations": total_iters,
            "accept_rate": round(accepted / max(1, total_iters), 3),
            "signal_hpwl_mm": round(hpwl, 1), "near_violations": viol[:20], "n_near_violations": len(viol),
            "near_excess_mm": round(excess, 1), "seed": seed, "overlaps": overl[:20], "outside": out_of[:20],
            "legalised_moves": moved, "refine_improvements": improved,
            "macros": {h: len(m) for h, m in members.items()},
            "bottom_side": sorted(r for r in refs if side_a[idx[r]] == 1),
            "area_utilisation": {"top": round(util_top, 2), "bottom": round(util_bot, 2)}}


def _pin_xy(items, pos, ref, pin):
    x, y, o = pos[ref]
    if pin is None:
        return x, y
    off = items[ref].pins[o].get(pin)
    if off is None:
        return x, y
    return x + off[0], y + off[1]


# ------------------------------------------------------------------ parallel portfolio
def _score(r: dict) -> float:
    """Lower is better: legality first, then constraint excess, then wirelength."""
    return 1e6 * len(r["overlaps"]) + 40.0 * len(r["outside"]) + 60.0 * r["near_excess_mm"] + r["signal_hpwl_mm"] \
        + 2.0 * len(r.get("bottom_side", []))


def _worker(args):
    root, effort, seed, kw = args
    import os
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    prj = Project(root)
    r = place(prj, effort=effort, seed=seed, **kw)
    return r, {k: v.model_dump() for k, v in prj.layout.placement.items()}


def place_parallel(prj: Project, seeds: int = 32, effort: float = 1.0, workers: int | None = None, **kw) -> dict:
    """Run `seeds` independent annealing chains on separate cores; keep the best by _score (top-16 saved)."""
    import json as _json
    import multiprocessing as mp
    import os
    from concurrent.futures import ProcessPoolExecutor
    prj.save()   # workers read the current IR/layout from disk
    workers = workers or min(seeds, max(1, (os.cpu_count() or 2) - 2))
    ctx = mp.get_context("fork")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
        results = list(ex.map(_worker, [(str(prj.root), effort, s, kw) for s in range(1, seeds + 1)]))
    ranked = sorted(results, key=lambda rp: _score(rp[0]))
    cdir = prj.root / "out" / "placements"
    cdir.mkdir(parents=True, exist_ok=True)
    for f in cdir.glob("rank*.json"):
        f.unlink()
    for i, (r, pl) in enumerate(ranked[:16]):
        (cdir / f"rank{i:02d}.json").write_text(_json.dumps({"seed": r["seed"], "score": _score(r), "placement": pl}))
    best, bestpl = ranked[0]
    prj.layout.placement = {k: Placement(**v) for k, v in bestpl.items()}
    prj.layout.history.append({"op": "place_parallel", "seeds": seeds, "best_seed": best["seed"]})
    best = dict(best)
    best["portfolio"] = {"seeds": seeds, "workers": workers,
                         "score_best": round(_score(ranked[0][0]), 1), "score_median": round(_score(ranked[len(ranked) // 2][0]), 1),
                         "score_worst": round(_score(ranked[-1][0]), 1)}
    return best
