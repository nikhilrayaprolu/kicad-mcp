"""Silkscreen legaliser: expert-style placement of reference designators and board labels.

A human layout engineer's last pass is the silkscreen. Every designator should sit next to its own part, in one
consistent size and reading direction. Designators must never touch pads (mask openings), other parts' outlines,
other text, mounting holes or the board edge. A designator with no clean spot moves to the fab layer.
This module computes those positions from the placed footprints. board.build() applies them.

Geometry is approximate but conservative. Text boxes use the KiCad stroke-font advance. Silk lines are boxed
with their stroke. Pads grow by the silk-to-mask clearance.
"""
from __future__ import annotations

import math

import numpy as np

from . import libs
from .ir import Project

PAD_CLR = 0.15       # silk to solder-mask opening
SILK_CLR = 0.12      # silk to silk
EDGE_CLR = 0.45      # silk to board edge
REF_SIZES = (0.8, 0.65)
THICK = 0.12


def text_box(text: str, size: float, ang: float) -> tuple[float, float]:
    """Half extents (hx, hy) of a KiCad stroke-font string centred on its anchor."""
    w = len(text) * 0.95 * size + THICK
    h = 1.08 * size + THICK
    if round(ang) % 180 == 90:
        w, h = h, w
    return w / 2, h / 2


class _Rects:
    def __init__(self):
        self.r: list[tuple[float, float, float, float]] = []
        self._a = None

    def add(self, x1, y1, x2, y2):
        self.r.append((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
        self._a = None

    def arr(self):
        if self._a is None:
            self._a = np.array(self.r, dtype=float).reshape(-1, 4)
        return self._a

    def hits(self, x1, y1, x2, y2, clr=0.0) -> bool:
        a = self.arr()
        if not len(a):
            return False
        return bool(np.any((a[:, 0] < x2 + clr) & (a[:, 2] > x1 - clr) & (a[:, 1] < y2 + clr) & (a[:, 3] > y1 - clr)))

    def overlap_area(self, x1, y1, x2, y2) -> float:
        a = self.arr()
        if not len(a):
            return 0.0
        w = np.clip(np.minimum(a[:, 2], x2) - np.maximum(a[:, 0], x1), 0, None)
        h = np.clip(np.minimum(a[:, 3], y2) - np.maximum(a[:, 1], y1), 0, None)
        return float((w * h).sum())


def _fp_silk_and_courtyard(fp, x, y, rot, side):
    silk, crt = [], None
    want = "B.SilkS" if side == "bottom" else "F.SilkS"
    for c in fp.node:
        if not isinstance(c, list) or libs.sx.head(c) not in ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly"):
            continue
        lay = libs.sx.find(c, "layer")
        if not lay or str(lay[1]) != want:
            continue
        st = libs.sx.find(c, "stroke")
        wd = libs.sx.find(st, "width") if st else libs.sx.find(c, "width")
        hw = (float(wd[1]) if wd else 0.12) / 2
        pts = [libs.rot(px, py, rot) for px, py in libs._geom_points(c)]
        if libs.sx.head(c) == "fp_rect" and len(pts) == 2:   # four sides, not one filled box
            (ax, ay), (bx, by) = libs._geom_points(c)
            corners = [libs.rot(px, py, rot) for px, py in ((ax, ay), (bx, ay), (bx, by), (ax, by))]
            for i in range(4):
                p, q = corners[i], corners[(i + 1) % 4]
                silk.append((x + min(p[0], q[0]) - hw, y + min(p[1], q[1]) - hw,
                             x + max(p[0], q[0]) + hw, y + max(p[1], q[1]) + hw))
            continue
        if libs.sx.head(c) == "fp_line" and len(pts) == 2:
            p, q = pts
            L = math.hypot(q[0] - p[0], q[1] - p[1])
            n = max(1, int(L / 0.5)) if abs(p[0] - q[0]) > 1e-3 and abs(p[1] - q[1]) > 1e-3 else 1
            for k in range(n):         # diagonal lines: chain of small boxes instead of one big bbox
                a = (p[0] + (q[0] - p[0]) * k / n, p[1] + (q[1] - p[1]) * k / n)
                b = (p[0] + (q[0] - p[0]) * (k + 1) / n, p[1] + (q[1] - p[1]) * (k + 1) / n)
                silk.append((x + min(a[0], b[0]) - hw, y + min(a[1], b[1]) - hw,
                             x + max(a[0], b[0]) + hw, y + max(a[1], b[1]) + hw))
            continue
        if pts:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            silk.append((x + min(xs) - hw, y + min(ys) - hw, x + max(xs) + hw, y + max(ys) + hw))
    bb = libs.bbox_rot(fp.courtyard, rot)
    crt = (x + bb[0], y + bb[1], x + bb[2], y + bb[3])
    return silk, crt


def plan(prj: Project) -> dict:
    """Return {"refs": {ref: (X, Y, ang, size) | None}, "texts": [(x, y, size) | None per silk_text],
    "title": (x, y, size)}; None = hide (fab layer) / keep as given."""
    d, L = prj.design, prj.layout
    W, H = d.board.width, d.board.height
    pads = {"top": _Rects(), "bottom": _Rects()}
    silk = {"top": _Rects(), "bottom": _Rects()}
    crt = {"top": _Rects(), "bottom": _Rects()}
    own_silk: dict[str, list] = {}
    own_crt: dict[str, tuple] = {}
    for ref, c in d.components.items():
        pl = L.placement.get(ref)
        if pl is None:
            continue
        fp = libs.get_footprint_side(c.footprint, pl.side)
        for p in fp.pads:
            px, py = libs.pad_abs(p, pl.x, pl.y, pl.rot)
            hx, hy = libs.pad_extent(p, pl.rot)
            cu = [l for l in p.layers if l.endswith(".Cu")]
            tht = p.kind in ("thru_hole", "np_thru_hole") or "*.Cu" in p.layers
            for s in ("top", "bottom"):
                lay = "F.Cu" if s == "top" else "B.Cu"
                if tht or lay in cu or (not cu and s == pl.side):
                    pads[s].add(px - hx, py - hy, px + hx, py + hy)
        sl, cr = _fp_silk_and_courtyard(fp, pl.x, pl.y, pl.rot, pl.side)
        own_silk[ref], own_crt[ref] = sl, cr
        for r in sl:
            silk[pl.side].add(*r)
        crt[pl.side].add(*cr)
        thru = any(p.kind == "thru_hole" for p in fp.pads)
        if thru:   # THT bodies block the far side's silk too (leads, solder fillets)
            other = "bottom" if pl.side == "top" else "top"
            crt[other].add(*cr)
    holes = _Rects()
    for mh in d.board.mounting_holes:
        r = (mh[2] if len(mh) > 2 else d.board.mh_drill) / 2 + 0.9     # drill + keep-out ring
        holes.add(mh[0] - r, mh[1] - r, mh[0] + r, mh[1] + r)
    for fd in d.board.fiducials:
        holes.add(fd[0] - 1.2, fd[1] - 1.2, fd[0] + 1.2, fd[1] + 1.2)
    texts = {"top": _Rects(), "bottom": _Rects()}

    def legal(side, x1, y1, x2, y2, skip_ref=None):
        if x1 < EDGE_CLR or y1 < EDGE_CLR or x2 > W - EDGE_CLR or y2 > H - EDGE_CLR:
            return False
        r = d.board.corner_r
        if r > 0:   # rounded corners: every box corner inside a corner square must stay inside the arc
            for px in (x1, x2):
                for py in (y1, y2):
                    cx = r if px < r else (W - r if px > W - r else None)
                    cy = r if py < r else (H - r if py > H - r else None)
                    if cx is not None and cy is not None and math.hypot(px - cx, py - cy) > r - EDGE_CLR:
                        return False
        if pads[side].hits(x1, y1, x2, y2, PAD_CLR) or holes.hits(x1, y1, x2, y2, 0.1):
            return False
        if silk[side].hits(x1, y1, x2, y2, SILK_CLR) or texts[side].hits(x1, y1, x2, y2, SILK_CLR):
            return False
        return True

    out_texts = []
    for st in d.board.silk_texts:            # intent labels first: they are anchored by the agent
        if st.get("ref"):
            pl_ = L.placement.get(st["ref"])
            if pl_ is None:
                out_texts.append(None)
                continue
            x0, y0, side = pl_.x + st.get("dx", 0.0), pl_.y + st.get("dy", 0.0), st.get("side", pl_.side)
        else:
            x0, y0, side = st.get("x", 0.0), st.get("y", 0.0), st.get("side", "top")
        size0 = st.get("size", 0.8)
        ang = st.get("rot", 0)
        best = None
        for size in (size0, round(size0 * 0.85, 2), round(size0 * 0.7, 2)):
            hx, hy = text_box(st["text"], size, ang)
            for rad in [0.0] + [0.25 * k for k in range(1, 15)]:
                steps = 1 if rad == 0 else max(8, int(2 * math.pi * rad / 0.25))
                for k in range(steps):
                    a = 2 * math.pi * k / steps
                    x, y = x0 + rad * math.cos(a), y0 + rad * math.sin(a)
                    if legal(side, x - hx, y - hy, x + hx, y + hy):
                        cost = rad + (size0 - size) * 4
                        if best is None or cost < best[0]:
                            best = (cost, round(x, 3), round(y, 3), size)
                if best and best[0] <= rad:
                    break
            if best:
                break
        if best:
            _, x, y, size = best
            hx, hy = text_box(st["text"], size, ang)
            texts[side].add(x - hx, y - hy, x + hx, y + hy)
            out_texts.append((x, y, size))
        else:
            out_texts.append(None)

    # designators: biggest parts first, they have the most constrained surroundings
    refs_out: dict = {}
    order = sorted(own_crt, key=lambda r: -(own_crt[r][2] - own_crt[r][0]) * (own_crt[r][3] - own_crt[r][1]))
    small_hide = getattr(d.board, "silk_refs", "all") == "large"
    for ref in order:
        c = d.components[ref]
        pl = L.placement[ref]
        cx1, cy1, cx2, cy2 = own_crt[ref]
        is_tp = c.footprint.startswith("TestPoint:")      # test points keep a label: probing needs it
        if small_hide and (cx2 - cx1) * (cy2 - cy1) < 4.5 and not is_tp:
            refs_out[ref] = None
            continue
        if c.footprint.startswith(("Fiducial:", "MountingHole:")):
            refs_out[ref] = None
            continue
        cx, cy = (cx1 + cx2) / 2, (cy1 + cy2) / 2
        tall = (cy2 - cy1) > 1.6 * (cx2 - cx1)
        best = None
        for size in REF_SIZES:
            for ang in ((90, 0) if tall else (0, 90)):
                hx, hy = text_box(ref, size, ang)
                cands = []
                for g in (0.05, 0.3, 0.6):
                    cands += [("above", cx, cy1 - hy - g), ("below", cx, cy2 + hy + g),
                              ("left", cx1 - hx - g, cy), ("right", cx2 + hx + g, cy)]
                    for f in (-0.35, 0.35):
                        cands += [("above", cx + f * (cx2 - cx1), cy1 - hy - g), ("below", cx + f * (cx2 - cx1), cy2 + hy + g),
                                  ("left", cx1 - hx - g, cy + f * (cy2 - cy1)), ("right", cx2 + hx + g, cy + f * (cy2 - cy1))]
                # inside the outline of big parts (connector bodies, modules): clear of pads and silk only
                if (cx2 - cx1) * (cy2 - cy1) > 25:
                    cands += [("inside", cx, cy), ("inside", cx, cy - (cy2 - cy1) / 4), ("inside", cx, cy + (cy2 - cy1) / 4)]
                for i, (where, x, y) in enumerate(cands):
                    x1, y1, x2, y2 = x - hx, y - hy, x + hx, y + hy
                    if not legal(pl.side, x1, y1, x2, y2):
                        continue
                    # soft: avoid sitting on other parts' bodies (hidden after assembly)
                    ov = crt[pl.side].overlap_area(x1, y1, x2, y2)
                    if where != "inside":
                        ov -= max(0.0, min(x2, cx2) - max(x1, cx1)) * max(0.0, min(y2, cy2) - max(y1, cy1))
                    cost = i * 0.02 + max(0.0, ov) * 3 + (REF_SIZES[0] - size) * 6 + (0.4 if ang != (90 if tall else 0) else 0)
                    if best is None or cost < best[0]:
                        best = (cost, round(x, 3), round(y, 3), ang, size)
            if best and best[0] < 0.5:
                break
        if best:
            _, x, y, ang, size = best
            hx, hy = text_box(ref, size, ang)
            texts[pl.side].add(x - hx, y - hy, x + hx, y + hy)
            refs_out[ref] = (x, y, ang, size)
        else:
            refs_out[ref] = None

    # board title on the bottom: lower edge if free, else anywhere free along the bottom half
    title = None
    if d.board.title:
        txt = f"{d.board.title} rev {d.board.rev}"
        hx, hy = text_box(txt, 0.8, 0)
        for y in [H - 1.4 - 0.5 * k for k in range(0, 40)]:
            for x in sorted(np.arange(hx + EDGE_CLR, W - hx - EDGE_CLR, 0.5), key=lambda v: abs(v - W / 2)):
                if legal("bottom", x - hx, y - hy, x + hx, y + hy) and not crt["bottom"].hits(x - hx, y - hy, x + hx, y + hy):
                    title = (round(float(x), 2), round(y, 2), 0.8)
                    break
            if title:
                break
        if title:
            texts["bottom"].add(title[0] - hx, title[1] - hy, title[0] + hx, title[1] + hy)
    shown = sum(1 for v in refs_out.values() if v)
    return {"refs": refs_out, "texts": out_texts, "title": title,
            "summary": {"refs_shown": shown, "refs_on_fab": len(refs_out) - shown,
                        "labels_moved": sum(1 for t in out_texts if t is not None),
                        "labels_unplaced": sum(1 for t in out_texts if t is None)}}
