"""Fast 2-D board renderer (PIL) so the LLM can *look* at placement/routing state,
plus a wrapper for kicad-cli's photoreal 3-D render and schematic PDF->PNG."""
from __future__ import annotations

import colorsys
import math
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import board as brd
from . import libs
from .ir import Project

LAYER_COL = {"F.Cu": (200, 60, 60, 200), "In1.Cu": (60, 160, 60, 150), "In2.Cu": (190, 150, 40, 150),
             "B.Cu": (60, 90, 210, 200), "In3.Cu": (150, 60, 160, 150), "In4.Cu": (60, 160, 160, 150)}


def _block_colors(blocks):
    out = {}
    for i, b in enumerate(sorted(blocks)):
        h = (i * 0.618) % 1.0
        r, g, bl = colorsys.hsv_to_rgb(h, 0.35, 0.95)
        out[b] = (int(r * 255), int(g * 255), int(bl * 255), 255)
    return out


def render_2d(prj: Project, out: Path | None = None, px_per_mm: float = 14, layers: list[str] | None = None,
              ratsnest: bool = True, labels: bool = True, highlight: list[str] | None = None) -> Path:
    d, L = prj.design, prj.layout
    W, H = d.board.width, d.board.height
    m = 3
    S = px_per_mm
    img = Image.new("RGBA", (int((W + 2 * m) * S), int((H + 2 * m) * S)), (20, 24, 28, 255))
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    dr = ImageDraw.Draw(ov)
    T = lambda x, y: ((x + m) * S, (y + m) * S)
    poly = brd.outline_poly(d)
    dr.polygon([T(*p) for p in poly], fill=(34, 60, 40, 255), outline=(230, 230, 120, 255))
    try:
        font = ImageFont.load_default(size=max(9, int(S * 0.75)))
        small = ImageFont.load_default(size=max(8, int(S * 0.5)))
    except TypeError:
        font = small = ImageFont.load_default()
    bcol = _block_colors({c.block for c in d.components.values()})
    # copper pours: KiCad's saved fills (faint, per layer), then zone outlines
    try:
        from . import fills as _fills
        for _net, lay, pts in _fills.read_fills(prj):
            if layers and lay not in layers:
                continue
            col = LAYER_COL.get(lay, (200, 200, 200, 80))
            dr.polygon([T(*p) for p in pts], fill=col[:3] + (110 if layers and len(layers) == 1 else 38,))
    except Exception:  # noqa: BLE001
        pass
    for z in L.zones:
        col = LAYER_COL.get(z.layer, (200, 200, 200, 80))
        pts = z.poly or brd.inset_rect(d, 0.0)
        dr.polygon([T(*p) for p in pts], outline=col[:3] + (120,))
    # courtyards
    for ref, c in d.components.items():
        pl = L.placement.get(ref)
        if not pl:
            continue
        fp = libs.get_footprint_side(c.footprint, pl.side)
        x1, y1, x2, y2 = libs.bbox_rot(fp.courtyard, pl.rot)
        col = bcol[c.block]
        hl = highlight and ref in highlight
        if pl.side == "bottom":   # bottom-side parts: hatched outline, no fill
            x_1, y_1 = T(pl.x + x1, pl.y + y1); x_2, y_2 = T(pl.x + x2, pl.y + y2)
            dr.rectangle([x_1, y_1, x_2, y_2], outline=(140, 170, 255, 255) if not hl else (255, 255, 0, 255), width=1)
            for k in range(int(x_1), int(x_2), 5):
                dr.line([(k, y_1), (min(k + (y_2 - y_1), x_2), min(y_2, y_1 + (x_2 - k)))], fill=(140, 170, 255, 90))
        else:
            dr.rectangle([T(pl.x + x1, pl.y + y1), T(pl.x + x2, pl.y + y2)], outline=(255, 255, 0, 255) if hl else col,
                         width=3 if hl else 1, fill=col[:3] + (40,))
    # tracks
    show = set(layers) if layers else None
    order = ["B.Cu", "In2.Cu", "In1.Cu", "F.Cu"]
    for lay in order:
        if show and lay not in show:
            continue
        col = LAYER_COL.get(lay, (200, 200, 200, 150))
        for t in L.tracks:
            if t.layer != lay:
                continue
            for a, b in zip(t.pts, t.pts[1:]):
                dr.line([T(*a), T(*b)], fill=col, width=max(1, int(t.width * S)))
    # pads
    for p in brd.abs_pads(prj):
        col = (210, 170, 60, 255) if "F.Cu" in p.layers else (110, 140, 230, 255)
        if p.drill:
            col = (200, 200, 200, 255)
        dr.rectangle([T(p.x - p.hx, p.y - p.hy), T(p.x + p.hx, p.y + p.hy)], fill=col)
    for v in L.vias:
        r = v.d / 2 * S
        cx, cy = T(v.x, v.y)
        dr.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(230, 230, 230, 255), outline=(40, 40, 40, 255))
    # ratsnest for nets with unrouted pads (MST over pads ignoring tracks; drawn only if no tracks on net)
    if ratsnest:
        routed = {t.net for t in L.tracks} | {z.net for z in L.zones}
        pads = brd.abs_pads(prj)
        bynet = {}
        for p in pads:
            if p.net:
                bynet.setdefault(p.net, []).append((p.x, p.y))
        for n, pts in bynet.items():
            if n in routed or len(pts) < 2 or len(pts) > 40:
                continue
            for a, b in _mst(pts):
                dr.line([T(*a), T(*b)], fill=(255, 255, 255, 110), width=1)
    if labels:
        for ref, pl in L.placement.items():
            if ref in d.components:
                dr.text(T(pl.x, pl.y), ref + ("'" if pl.side == "bottom" else ""),
                        fill=(255, 255, 255, 230) if pl.side == "top" else (170, 200, 255, 230), font=small, anchor="mm")
    for mh in d.board.mounting_holes:
        r = (mh[2] if len(mh) > 2 else d.board.mh_drill) / 2 * S
        cx, cy = T(mh[0], mh[1])
        dr.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(240, 240, 240, 255), width=2)
    # legend
    y = 4
    for b, col in bcol.items():
        dr.rectangle([4, y, 14, y + 10], fill=col)
        dr.text((18, y - 1), b, fill=(240, 240, 240, 255), font=small)
        y += 13
    img = Image.alpha_composite(img, ov)
    out = out or (prj.root / "out" / "board_2d.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out)
    return out


def _mst(pts):
    n = len(pts)
    inT = [False] * n
    dist = [math.inf] * n
    par = [-1] * n
    dist[0] = 0
    edges = []
    for _ in range(n):
        u = min((i for i in range(n) if not inT[i]), key=lambda i: dist[i])
        inT[u] = True
        if par[u] >= 0:
            edges.append((pts[par[u]], pts[u]))
        for v in range(n):
            if not inT[v]:
                dd = math.hypot(pts[u][0] - pts[v][0], pts[u][1] - pts[v][1])
                if dd < dist[v]:
                    dist[v], par[v] = dd, u
    return edges


def render_3d(prj: Project, side: str = "top", out: Path | None = None, w: int = 1600, h: int = 1000) -> Path:
    out = out or (prj.root / "out" / f"board_3d_{side}.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["kicad-cli", "pcb", "render", "--side", side, "-w", str(w), "-h", str(h), "--quality", "basic",
           "-o", str(out), str(prj.path(".kicad_pcb"))]
    subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return out


def render_schematic(prj: Project, page: int = 0, dpi: int = 70) -> list[Path]:
    import pymupdf
    pdf = prj.root / "out" / "schematic.pdf"
    pdf.parent.mkdir(exist_ok=True)
    subprocess.run(["kicad-cli", "sch", "export", "pdf", "-o", str(pdf), str(prj.path(".kicad_sch"))],
                   capture_output=True, text=True, timeout=300)
    doc = pymupdf.open(pdf)
    outs = []
    for i, pg in enumerate(doc):
        if page and i + 1 != page:
            continue
        p = prj.root / "out" / f"schematic_p{i+1}.png"
        pg.get_pixmap(dpi=dpi).save(p)
        outs.append(p)
    return outs
