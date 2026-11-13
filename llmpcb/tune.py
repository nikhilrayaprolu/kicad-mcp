"""Length tuning: serpentine (trombone) meanders on the shorter leg of a differential pair.

For each diffpair constraint with |len(P) - len(N)| > tol, the shorter net gets one or more single-sided
meanders on its longest straight segments.  Each meander's height/period are sized to the missing length,
and every meander point is checked against copper on its layer (other nets' tracks, pads, vias, holes,
board edge) with the netclass clearance before it is committed.
"""
from __future__ import annotations

import math
from collections import defaultdict

from . import board as brd
from .ir import Project, Track


def net_length(prj: Project, net: str) -> float:
    return sum(math.hypot(a[0] - b[0], a[1] - b[1]) for t in prj.layout.tracks if t.net == net
               for a, b in zip(t.pts, t.pts[1:]))


class LayerObstacles:
    def __init__(self, prj: Project, layer: str, net: str):
        self.items = []     # (kind, geom, halfwidth, clearance)
        d = prj.design
        for p in brd.abs_pads(prj):
            if p.net == net or layer not in p.layers:
                continue
            c = d.netclass_of(p.net).clearance if p.net else 0.2
            self.items.append(("rect", (p.x - p.hx, p.y - p.hy, p.x + p.hx, p.y + p.hy), 0.0, c))
            if p.drill:
                self.items.append(("circ", (p.x, p.y), p.drill / 2, 0.2))
        for t in prj.layout.tracks:
            if t.net == net or t.layer != layer:
                continue
            c = d.netclass_of(t.net).clearance
            for a, b in zip(t.pts, t.pts[1:]):
                self.items.append(("seg", (a[0], a[1], b[0], b[1]), t.width / 2, c))
        for v in prj.layout.vias:
            if v.net == net:
                continue
            self.items.append(("circ", (v.x, v.y), v.d / 2, d.netclass_of(v.net).clearance))
        for mh in d.board.mounting_holes:
            self.items.append(("circ", (mh[0], mh[1]), (mh[2] if len(mh) > 2 else d.board.mh_drill) / 2, 0.3))
        self.W, self.H = d.board.width, d.board.height

    def ok(self, x, y, hw, clr, edge=0.35) -> bool:
        if x < edge + hw or y < edge + hw or x > self.W - edge - hw or y > self.H - edge - hw:
            return False
        for kind, g, ghw, gclr in self.items:
            need = hw + max(clr, gclr) + 0.015
            if kind == "rect":
                dx = max(g[0] - x, 0, x - g[2]); dy = max(g[1] - y, 0, y - g[3])
                if math.hypot(dx, dy) < need:
                    return False
            elif kind == "seg":
                ax, ay, bx, by = g
                vx, vy = bx - ax, by - ay
                L2 = vx * vx + vy * vy
                t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - ax) * vx + (y - ay) * vy) / L2))
                if math.hypot(x - (ax + t * vx), y - (ay + t * vy)) - ghw < need:
                    return False
            else:
                if math.hypot(x - g[0], y - g[1]) - ghw < need:
                    return False
        return True

    def path_ok(self, pts, hw, clr) -> bool:
        for a, b in zip(pts, pts[1:]):
            L = math.hypot(b[0] - a[0], b[1] - a[1])
            n = max(1, int(L / 0.04))
            for k in range(n + 1):
                if not self.ok(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n, hw, clr):
                    return False
        return True


def _meander(a, b, start, k, s, h, side, c=0.0):
    """Points of a single-sided trombone on segment a->b: starting `start` mm from a, k periods of length 2s.
    c > 0 bevels every corner at 45 degrees (expert look); each bevel shortens the path by (2 - sqrt2) * c."""
    L = math.hypot(b[0] - a[0], b[1] - a[1])
    ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
    nx, ny = -uy * side, ux * side
    P = lambda t, o: [round(a[0] + ux * t + nx * o, 4), round(a[1] + uy * t + ny * o, 4)]
    pts = [P(start - c, 0)]
    t = start
    for _ in range(k):
        if c > 0:
            pts += [P(t, c), P(t, h - c), P(t + c, h), P(t + s - c, h), P(t + s, h - c), P(t + s, c),
                    P(t + s + c, 0), P(t + 2 * s - c, 0)]
        else:
            pts += [P(t, h), P(t + s, h), P(t + s, 0), P(t + 2 * s, 0)]
        t += 2 * s
    return pts, t


MITER_LOSS = 2 - math.sqrt(2)


def tune_pair(prj: Project, p: str, n: str, tol: float, miter: bool = True) -> dict:
    lp, ln = net_length(prj, p), net_length(prj, n)
    skew = abs(lp - ln)
    if skew <= tol:
        return {"pair": f"{p}/{n}", "skew_before": round(skew, 2), "action": "within tolerance"}
    short = p if lp < ln else n
    need = skew - tol * 0.3          # land comfortably inside the tolerance
    nc = prj.design.netclass_of(short)
    clr = nc.clearance
    added = 0.0
    obs_cache: dict[str, LayerObstacles] = {}
    edits = 0
    tried: set = set()
    while need - added > 0.02:
        # candidate straight segments of the short net, longest first (recomputed after every edit: indices shift)
        cands = []
        for ti, t in enumerate(prj.layout.tracks):
            if t.net != short:
                continue
            for si, (a, b) in enumerate(zip(t.pts, t.pts[1:])):
                L = math.hypot(b[0] - a[0], b[1] - a[1])
                key = (t.layer, tuple(a), tuple(b))
                if L >= 1.2 and key not in tried:
                    cands.append((L, ti, si, key))
        cands.sort(reverse=True)
        progress = False
        for L, ti, si, key in cands:
            tried.add(key)
            t = prj.layout.tracks[ti]
            hw = t.width / 2
            a, b = t.pts[si], t.pts[si + 1]
            obs = obs_cache.setdefault(t.layer, LayerObstacles(prj, t.layer, short))
            best = None
            # leg spacing: 3w keeps same-net coupling low; fall back to the clearance minimum when space is tight
            for s in sorted({max(3 * t.width, t.width + clr + 0.05, 0.3), max(2 * t.width, t.width + clr + 0.03)},
                            reverse=True):
                margin = max(0.4, s)
                usable = L - 2 * margin
                k_max = int(usable // (2 * s))
                if k_max < 1:
                    continue
                for h in (1.5, 1.2, 1.0, 0.8, 0.6, 0.5, 0.4, 0.3, 0.25, 0.2):
                    rem = need - added
                    k = max(1, min(k_max, math.ceil(rem / (2 * h))))
                    hh = min(h, rem / (2 * k)) if k * 2 * h > rem else h
                    hh = max(hh, 0.15)
                    c = round(min(s / 3, hh / 3, 0.15), 3) if miter else 0.0
                    if c > 0:                         # compensate the bevel loss (4 corners per period)
                        hh = hh + (4 * MITER_LOSS * c) / 2
                    for side in (1, -1):
                        for start in (margin, (L - k * 2 * s) / 2, L - margin - k * 2 * s):
                            if start < margin - 1e-9 or start + k * 2 * s > L - margin + 1e-9:
                                continue
                            pts, end_t = _meander(a, b, start, k, s, hh, side, c)
                            if obs.path_ok(pts, hw, clr):
                                best = (pts, k * (2 * hh - 4 * MITER_LOSS * c))
                                break
                        if best:
                            break
                    if best:
                        break
                if best:
                    break
            if not best:
                continue
            pts, gain = best
            new_pts = t.pts[:si + 1] + pts + t.pts[si + 1:]
            clean = [new_pts[0]]
            for q in new_pts[1:]:
                if abs(q[0] - clean[-1][0]) > 1e-6 or abs(q[1] - clean[-1][1]) > 1e-6:
                    clean.append(q)
            t.pts = clean
            added += gain
            edits += 1
            obs_cache.pop(t.layer, None)
            progress = True
            break
        if not progress:
            break
    lp2, ln2 = net_length(prj, p), net_length(prj, n)
    return {"pair": f"{p}/{n}", "skew_before": round(skew, 2), "skew_after": round(abs(lp2 - ln2), 2),
            "tuned_net": short, "meanders": edits, "added_mm": round(added, 2), "tol": tol}



def _pinned_fn(prj: Project):
    L = prj.layout
    pads = brd.abs_pads(prj)
    joints = defaultdict(int)
    for t in L.tracks:
        for p in t.pts:
            joints[(t.net, t.layer, round(p[0], 3), round(p[1], 3))] += 1
    via_at = {(v.net, round(v.x, 3), round(v.y, 3)) for v in L.vias}

    def pinned(t, p):
        k = (t.net, t.layer, round(p[0], 3), round(p[1], 3))
        if joints[k] > 1 or (t.net, round(p[0], 3), round(p[1], 3)) in via_at:
            return True
        return any(q.net == t.net and t.layer in q.layers and abs(q.x - p[0]) <= q.hx + 0.05
                   and abs(q.y - p[1]) <= q.hy + 0.05 for q in pads)
    return pinned


def _axis(v):
    return abs(v[0]) < 1e-4 or abs(v[1]) < 1e-4


def _sub(a, b):
    return (b[0] - a[0], b[1] - a[1])


def reset_tuning(prj: Project, nets: list[str]) -> dict:
    """Undo earlier tuning on `nets` so they can be re-tuned cleanly: (1) square up short 45-degree corner bevels,
    (2) remove trombone bumps (out-and-back detours of <= 1.4 mm on a straight run).  Every change is
    clearance-checked and points shared with other copper (vias, pads, junctions) are never moved."""
    pinned = _pinned_fn(prj)
    squared = stripped = 0
    for t in prj.layout.tracks:
        if t.net not in nets:
            continue
        obs = LayerObstacles(prj, t.layer, t.net)
        clr = prj.design.netclass_of(t.net).clearance
        hw = t.width / 2
        changed = True
        while changed:
            changed = False
            P = t.pts
            # (1) bevel p[i]->p[i+1] between perpendicular axis-aligned segments -> single right-angle corner
            for i in range(1, len(P) - 2):
                v0, v1, v2 = _sub(P[i - 1], P[i]), _sub(P[i], P[i + 1]), _sub(P[i + 1], P[i + 2])
                l1 = math.hypot(*v1)
                if not (_axis(v0) and _axis(v2)) or _axis(v1) or l1 > 0.9 or abs(abs(v1[0]) - abs(v1[1])) > 1e-3:
                    continue
                if abs(v0[0] * v2[0] + v0[1] * v2[1]) > 1e-6 or pinned(t, P[i]) or pinned(t, P[i + 1]):
                    continue
                corner = [P[i + 1][0], P[i][1]] if abs(v0[1]) < 1e-4 else [P[i][0], P[i + 1][1]]
                if obs.path_ok([P[i - 1], corner, P[i + 2]], hw, clr):
                    t.pts = P[:i] + [corner] + P[i + 2:]
                    squared += 1
                    changed = True
                    break
            if changed:
                continue
            # (2) trombone bump p[i]..p[i+3]: out, along, back (equal and opposite legs), base collinear
            for i in range(0, len(P) - 3):
                a, b, c, d = P[i], P[i + 1], P[i + 2], P[i + 3]
                up, al, dn = _sub(a, b), _sub(b, c), _sub(c, d)
                if abs(up[0] + dn[0]) > 1e-3 or abs(up[1] + dn[1]) > 1e-3 or math.hypot(*up) > 1.4:
                    continue
                if abs(up[0] * al[0] + up[1] * al[1]) > 1e-6 or math.hypot(*al) < 1e-6:
                    continue
                if pinned(t, b) or pinned(t, c):
                    continue
                if obs.path_ok([a, d], hw, clr):
                    t.pts = P[:i + 1] + P[i + 3:]
                    stripped += 1
                    changed = True
                    break
        # merge collinear interior points
        out = [t.pts[0]]
        for k in range(1, len(t.pts) - 1):
            a, b, c = out[-1], t.pts[k], t.pts[k + 1]
            cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
            dot = (b[0] - a[0]) * (c[0] - b[0]) + (b[1] - a[1]) * (c[1] - b[1])
            if abs(cross) < 1e-6 and dot >= 0 and not pinned(t, b):
                continue
            if abs(b[0] - a[0]) < 1e-6 and abs(b[1] - a[1]) < 1e-6:
                continue
            out.append(b)
        out.append(t.pts[-1])
        t.pts = out
    return {"squared_bevels": squared, "removed_meanders": stripped}


def tune_all(prj: Project, default_tol: float = 0.5, only: list[str] | None = None, reset: bool = False) -> dict:
    """reset=True first removes earlier meanders/bevels on the pairs (re-tune from the routed geometry)."""
    out = []
    for c in prj.design.constraints:
        if c.kind != "diffpair" or not c.p or not c.n:
            continue
        if only and c.p not in only and c.n not in only:
            continue
        # multi-pin nets (e.g. USB with ESD branches) have no single path length to match
        pads = defaultdict(int)
        for q in brd.abs_pads(prj):
            if q.net in (c.p, c.n):
                pads[q.net] += 1
        if pads[c.p] != 2 or pads[c.n] != 2:
            out.append({"pair": f"{c.p}/{c.n}", "action": "skipped (multi-pin net: total copper length is not a path length)"})
            continue
        r = reset_tuning(prj, [c.p, c.n]) if reset else None
        res = tune_pair(prj, c.p, c.n, c.tol_mm or default_tol)
        if r:
            res["reset"] = r
        out.append(res)
    return {"pairs": out}


def smooth_tracks(prj: Project, max_chamfer: float = 0.6) -> dict:
    """Expert finish: merge collinear points and chamfer 90-degree corners into 45-degree bevels.
    Corners where anything else joins (other tracks' points, vias, pads) are kept, so connectivity is untouched;
    every bevel is clearance-checked on its layer."""
    L = prj.layout
    pads = brd.abs_pads(prj)
    joints = defaultdict(int)
    for t in L.tracks:
        for p in t.pts:
            joints[(t.net, t.layer, round(p[0], 3), round(p[1], 3))] += 1
    via_at = {(v.net, round(v.x, 3), round(v.y, 3)) for v in L.vias}

    def pinned(t, p):
        k = (t.net, t.layer, round(p[0], 3), round(p[1], 3))
        if joints[k] > 1 or (t.net, round(p[0], 3), round(p[1], 3)) in via_at:
            return True
        return any(q.net == t.net and t.layer in q.layers and abs(q.x - p[0]) <= q.hx + 0.05 and abs(q.y - p[1]) <= q.hy + 0.05
                   for q in pads)

    merged = chamfered = 0
    obs_cache: dict = {}
    tuned = {x for c in prj.design.constraints if c.kind == "diffpair" for x in (c.p, c.n) if x}
    for t in L.tracks:
        if t.net in tuned:          # length-matched: meanders already carry their own 45-degree bevels
            continue
        # 1) merge collinear interior points (only unpinned)
        pts = [t.pts[0]]
        for k in range(1, len(t.pts) - 1):
            a, b, c = pts[-1], t.pts[k], t.pts[k + 1]
            cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
            dot = (b[0] - a[0]) * (c[0] - b[0]) + (b[1] - a[1]) * (c[1] - b[1])
            if abs(cross) < 1e-6 and dot > 0 and not pinned(t, b):
                merged += 1
                continue
            pts.append(b)
        pts.append(t.pts[-1])
        # 2) chamfer right-angle corners
        out = [pts[0]]
        for k in range(1, len(pts) - 1):
            a, b, c = out[-1], pts[k], pts[k + 1]
            v1 = (b[0] - a[0], b[1] - a[1]); v2 = (c[0] - b[0], c[1] - b[1])
            l1, l2 = math.hypot(*v1), math.hypot(*v2)
            if l1 < 1e-6 or l2 < 1e-6:
                continue
            cosang = (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2)
            if abs(cosang) > 0.05 or pinned(t, b):
                out.append(b)
                continue
            cl = min(max_chamfer, l1 * 0.45, l2 * 0.45)
            if cl < 0.1:
                out.append(b)
                continue
            p1 = [round(b[0] - v1[0] / l1 * cl, 4), round(b[1] - v1[1] / l1 * cl, 4)]
            p2 = [round(b[0] + v2[0] / l2 * cl, 4), round(b[1] + v2[1] / l2 * cl, 4)]
            key = (t.layer, t.net)
            if key not in obs_cache:
                obs_cache[key] = LayerObstacles(prj, t.layer, t.net)
            if obs_cache[key].path_ok([p1, p2], t.width / 2, prj.design.netclass_of(t.net).clearance):
                out += [p1, p2]
                chamfered += 1
            else:
                out.append(b)
        out.append(pts[-1])
        t.pts = out
    return {"merged_points": merged, "chamfered_corners": chamfered}
