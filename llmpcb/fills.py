"""Copper pours: keep the saved board filled, read fills back, and tie floating pour islands to ground.

A board file with zone *outlines* but no fills looks unpoured in KiCad, in renders and on GitHub. So every
board write ends with a KiCad refill (`kicad-cli pcb drc --refill-zones --save-board`), and the filled
polygons are stored in the .kicad_pcb.

Expert practice for floating pour islands is to stitch them to the ground plane with a via. Deleting them
leaves bare laminate and uneven copper. `stitch_islands` keeps every island during an analysis fill. It finds
islands that touch no copper of their net, drops a ground via into each one big enough (clear of all other
copper on every layer), and then refills with island removal on, so only unstitchable slivers disappear.
"""
from __future__ import annotations

import math
import subprocess
import tempfile
from pathlib import Path

from . import board as brd
from . import sexpr as sx
from .ir import Project, Via


def refill(prj: Project) -> bool:
    """Fill all zones with KiCad and save them into the board file."""
    pcb = prj.path(".kicad_pcb")
    if not pcb.exists() or not prj.layout.zones:
        return False
    with tempfile.TemporaryDirectory() as td:
        rep = Path(td) / "fill.json"
        subprocess.run(["kicad-cli", "pcb", "drc", "--refill-zones", "--save-board", "--format", "json",
                        "-o", str(rep), str(pcb)], capture_output=True, text=True, timeout=900)
    return True


def read_fills(prj: Project) -> list[tuple[str, str, list[list[tuple[float, float]]]]]:
    """[(net, layer, [polygon outline points ...])] from the saved board's filled_polygon nodes."""
    pcb = prj.path(".kicad_pcb")
    if not pcb.exists():
        return []
    tree = sx.load(pcb)
    out = []
    for z in sx.find_all(tree, "zone"):
        nn = sx.find(z, "net_name")
        net = str(nn[1]) if nn else ""
        for fp in sx.find_all(z, "filled_polygon"):
            lay = sx.find(fp, "layer")
            pts = [(float(xy[1]), float(xy[2])) for xy in sx.find_all(sx.find(fp, "pts") or [], "xy")]
            if lay and len(pts) >= 3:
                out.append((net, str(lay[1]), pts))
    return out


def stitch_islands(prj: Project, net: str = "GND", min_area: float = 0.8, d: float = 0.45, drill: float = 0.25,
                   clearance: float = 0.2, write=None) -> dict:
    """Give every floating `net` pour island a via to the plane. Needs a solid `net` plane on some layer.
    `write(prj)` rewrites the board (server._write_board without refill)."""
    from shapely.geometry import Point, Polygon
    from shapely.validation import make_valid

    from . import router

    brd.KEEP_ISLANDS = True
    try:
        write(prj)
        refill(prj)
        fills = read_fills(prj)
    finally:
        brd.KEEP_ISLANDS = False
    pads = [p for p in brd.abs_pads(prj) if p.net == net]
    vias = [(v.x, v.y) for v in prj.layout.vias if v.net == net]
    obs = router._Obstacles(prj)
    added, floating, small = 0, 0, 0
    for fnet, layer, pts in fills:
        if fnet != net:
            continue
        poly = make_valid(Polygon(pts))
        if poly.is_empty:
            continue
        grown = poly.buffer(0.05)
        anchored = any(grown.contains(Point(x, y)) for x, y in vias) or \
            any(layer in p.layers and grown.intersects(Point(p.x, p.y).buffer(max(p.hx, p.hy))) for p in pads) or \
            any(t.net == net and t.layer == layer and any(grown.contains(Point(q[0], q[1])) for q in t.pts)
                for t in prj.layout.tracks)
        if anchored:
            continue
        floating += 1
        if poly.area < min_area:
            small += 1
            continue
        core = poly.buffer(-(d / 2 + 0.03))
        if core.is_empty:
            small += 1
            continue
        x1, y1, x2, y2 = core.bounds
        cands = []
        step = 0.25
        y = y1
        while y <= y2:
            x = x1
            while x <= x2:
                if core.contains(Point(x, y)):
                    cands.append((x, y))
                x += step
            y += step
        rp = core.representative_point()
        cands.sort(key=lambda q: math.hypot(q[0] - rp.x, q[1] - rp.y))
        for x, y in cands[:400]:
            if obs.clear(x, y, d / 2, net, clearance):
                v = Via(net=net, x=round(x, 3), y=round(y, 3), d=d, drill=drill, locked=True)
                prj.layout.vias.append(v)
                obs._add(("via", net, (v.x, v.y), d / 2))
                vias.append((v.x, v.y))
                added += 1
                break
    return {"floating_islands": floating, "stitched": added, "unstitched_removed": floating - added}
