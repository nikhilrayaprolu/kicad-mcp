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


def _meander(a, b, start, k, s, h, side):
    """Points of a single-sided trombone on segment a->b: starting `start` mm from a, k periods of length 2s."""
    L = math.hypot(b[0] - a[0], b[1] - a[1])
    ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
    nx, ny = -uy * side, ux * side
    P = lambda t, o: [round(a[0] + ux * t + nx * o, 4), round(a[1] + uy * t + ny * o, 4)]
    pts = [P(start, 0)]
    t = start
    for _ in range(k):
        pts += [P(t, h), P(t + s, h), P(t + s, 0), P(t + 2 * s, 0)]
        t += 2 * s
    return pts, t


def tune_pair(prj: Project, p: str, n: str, tol: float) -> dict:
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
    # candidate straight segments of the short net, longest first
    cands = []
    for ti, t in enumerate(prj.layout.tracks):
        if t.net != short:
            continue
        for si, (a, b) in enumerate(zip(t.pts, t.pts[1:])):
            L = math.hypot(b[0] - a[0], b[1] - a[1])
            if L >= 1.5:
                cands.append((L, ti, si))
    cands.sort(reverse=True)
    edits = 0
    for L, ti, si in cands:
        if need - added <= 0.02:
            break
        t = prj.layout.tracks[ti]
        hw = t.width / 2
        s = max(3 * t.width, t.width + clr + 0.05, 0.3)       # leg spacing (same-net coupling kept low)
        a, b = t.pts[si], t.pts[si + 1]
        margin = max(0.4, s)
        usable = L - 2 * margin
        if usable < 2 * s:
            continue
        obs = obs_cache.setdefault(t.layer, LayerObstacles(prj, t.layer, short))
        best = None
        for h in (1.2, 1.0, 0.8, 0.6, 0.5, 0.4, 0.3, 0.25, 0.2):
            k_max = int(usable // (2 * s))
            if k_max < 1:
                break
            rem = need - added
            k = max(1, min(k_max, math.ceil(rem / (2 * h))))
            hh = min(h, rem / (2 * k)) if k * 2 * h > rem else h
            hh = max(hh, 0.15)
            for side in (1, -1):
                for start in (margin, (L - k * 2 * s) / 2, L - margin - k * 2 * s):
                    if start < margin - 1e-9 or start + k * 2 * s > L - margin + 1e-9:
                        continue
                    pts, end_t = _meander(a, b, start, k, s, hh, side)
                    if obs.path_ok(pts, hw, clr):
                        best = (pts, k * 2 * hh)
                        break
                if best:
                    break
            if best:
                break
        if not best:
            continue
        pts, gain = best
        new_pts = t.pts[:si + 1] + pts + t.pts[si + 1:]
        # drop duplicate consecutive points
        clean = [new_pts[0]]
        for q in new_pts[1:]:
            if abs(q[0] - clean[-1][0]) > 1e-6 or abs(q[1] - clean[-1][1]) > 1e-6:
                clean.append(q)
        t.pts = clean
        added += gain
        edits += 1
        obs_cache.pop(t.layer, None)
    lp2, ln2 = net_length(prj, p), net_length(prj, n)
    return {"pair": f"{p}/{n}", "skew_before": round(skew, 2), "skew_after": round(abs(lp2 - ln2), 2),
            "tuned_net": short, "meanders": edits, "added_mm": round(added, 2), "tol": tol}


def tune_all(prj: Project, default_tol: float = 0.5, only: list[str] | None = None) -> dict:
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
        out.append(tune_pair(prj, c.p, c.n, c.tol_mm or default_tol))
    return {"pairs": out}
