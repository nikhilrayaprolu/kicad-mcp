"""Routing engine: Specctra DSN writer -> Freerouting (headless) -> SES importer.

Our own DSN writer (no pcbnew): every component gets a *pre-rotated* image placed at
rotation 0, so the only coordinate convention that matters is ours (verified against
KiCad's IPC-D-356 export).  Plane constraints become Specctra power planes so the
router drops vias into GND / power pours instead of routing them as tracks.
"""
from __future__ import annotations

import fnmatch
import math
import os
import re
import shutil
import subprocess
import time
from collections import defaultdict
from pathlib import Path

from . import board as brd
from . import libs
from . import sexpr as sx
from .ir import Project, Track, Via

FR_JAR = Path(__file__).resolve().parents[1] / "tools/freerouting/freerouting-2.5.0.jar"
UM = 1000.0  # mm -> um


def _q(s: str) -> str:
    return '"' + s.replace('"', "") + '"'


def _c(v: float) -> str:
    return f"{v * UM:.1f}".rstrip("0").rstrip(".")


_PS_IDS: dict[str, str] = {}


def _padstack_name(p: brd.AbsPad) -> str:
    """Opaque padstack id.  NOTE: Freerouting 2.5 mis-resolves descriptive names that differ only in
    their dimensions (e.g. '..0.700x0.200..' vs '..0.200x0.700..'), silently using the wrong shape
    (found while routing the RV1106's 0.35 mm QFN: 208 phantom pin-to-pin violations)."""
    ls = "A" if len(p.layers) > 1 else ("T" if p.layers[0] == "F.Cu" else "B")
    a = round(p.angle) % 360
    key = f"{p.shape}|{ls}|{p.w:.4f}|{p.h:.4f}|{a}|{p.drill:.4f}|{','.join(p.layers)}"
    if key not in _PS_IDS:
        _PS_IDS[key] = f"PS{len(_PS_IDS):04d}"
    return _PS_IDS[key]


def _pad_shapes(p: brd.AbsPad) -> list[str]:
    out = []
    w, h = p.w, p.h
    a = round(p.angle) % 180
    for layer in p.layers:
        if p.shape == "circle":
            out.append(f"(shape (circle {layer} {_c(w)}))")
        elif p.shape == "oval" and abs(w - h) > 1e-6 and a in (0, 90):
            if a == 90:
                w, h = h, w
            if w > h:
                dx = (w - h) / 2
                out.append(f"(shape (path {layer} {_c(h)} {_c(-dx)} 0 {_c(dx)} 0))")
            else:
                dy = (h - w) / 2
                out.append(f"(shape (path {layer} {_c(w)} 0 {_c(-dy)} 0 {_c(dy)}))")
            w, h = p.w, p.h
        elif a in (0, 90):
            hx, hy = (w / 2, h / 2) if a == 0 else (h / 2, w / 2)
            out.append(f"(shape (rect {layer} {_c(-hx)} {_c(-hy)} {_c(hx)} {_c(hy)}))")
        else:  # arbitrary rotation -> polygon (Y flipped for DSN)
            pts = []
            for x, y in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
                rx, ry = libs.rot(x, y, p.angle)
                pts += [_c(rx), _c(-ry)]
            out.append(f"(shape (polygon {layer} 0 {' '.join(pts)}))")
    return out


def via_name(d_mm: float, drill: float, ncu: int) -> str:
    return f"Via[0-{ncu-1}]_{int(d_mm*1000)}:{int(drill*1000)}_um"


def write_dsn(prj: Project, path: Path, planes: dict[str, str] | None = None, exclude_nets: set[str] | None = None,
              protect_all: bool = False, regional_planes: bool = True) -> dict:
    d, L = prj.design, prj.layout
    cul = brd.copper_layers(d.board.layers)
    planes = planes or {}                 # layer -> net (power layer)
    exclude_nets = exclude_nets or set()
    pads = brd.abs_pads(prj)
    by_ref: dict[str, list[brd.AbsPad]] = defaultdict(list)
    for p in pads:
        by_ref[p.ref].append(p)
    lines = [f'(pcb {_q(d.name)}',
             '  (parser (string_quote ") (space_in_quoted_tokens on) (host_cad "KiCad\'s Pcbnew") (host_version "10.0"))',
             '  (resolution um 10)', '  (unit um)', '  (structure']
    for i, l in enumerate(cul):
        typ = "power" if l in planes else "signal"
        lines.append(f'    (layer {l} (type {typ}) (property (index {i})))')
    # Freerouting keeps only its class clearance from the boundary; the fab's copper-to-edge rule is larger
    from .schematic import FAB_RULES
    edge = FAB_RULES.get(d.board.fab, FAB_RULES["jlcpcb"])["min_copper_edge_clearance"]
    min_clr = min(nc.clearance for nc in d.netclasses.values())
    poly = brd.outline_poly(d, inset=max(0.0, edge - min_clr) + 0.02)
    lines.append('    (boundary (path pcb 0 ' + " ".join(f"{_c(x)} {_c(-y)}" for x, y in poly + [poly[0]]) + '))')
    for layer, net in planes.items():
        W, H = d.board.width, d.board.height
        lines.append(f'    (plane {_q(net)} (polygon {layer} 0 ' + " ".join(
            f"{_c(x)} {_c(-y)}" for x, y in [(0.3, 0.3), (W - 0.3, 0.3), (W - 0.3, H - 0.3), (0.3, H - 0.3), (0.3, 0.3)]) + '))')
    for c in d.constraints:      # regional (split) planes: conduction areas on signal layers
        if regional_planes and c.kind == "plane" and c.rect and c.layer in cul:
            x1, y1, x2, y2 = c.rect
            lines.append(f'    (plane {_q(c.net)} (polygon {c.layer} 0 ' + " ".join(
                f"{_c(x)} {_c(-y)}" for x, y in [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]) + '))')
    # keepouts
    for c in d.constraints:
        if c.kind == "keepout" and c.rect:
            x1, y1, x2, y2 = c.rect
            lays = c.layers or cul
            for l in lays:
                if l in cul:
                    lines.append(f'    (keepout "" (rect {l} {_c(x1)} {_c(-y2)} {_c(x2)} {_c(-y1)}))')
    # keepout zones embedded in footprints (e.g. microSD contact area)
    for ref, comp in d.components.items():
        pl = L.placement.get(ref)
        if pl is None:
            continue
        fpn = libs.get_footprint_side(comp.footprint, pl.side).node
        for z in sx.find_all(fpn, "zone"):
            ko = sx.find(z, "keepout")
            if ko is None:
                continue
            bad = {str(c[0]) for c in ko[1:] if isinstance(c, list) and len(c) > 1 and c[1] == "not_allowed"}
            if not ({"tracks", "vias"} & bad):
                continue
            lays = sx.find(z, "layers") or sx.find(z, "layer")
            lnames = [str(x) for x in lays[1:]] if lays else ["F.Cu"]
            lnames = cul if any(x in ("*.Cu", "F&B.Cu") for x in lnames) else [x for x in lnames if x in cul]
            poly = sx.find(sx.find(z, "polygon") or [], "pts")
            if not poly:
                continue
            pts = []
            for xy in sx.find_all(poly, "xy"):
                ax, ay = libs.rot(float(xy[1]), float(xy[2]), pl.rot)
                pts += [_c(pl.x + ax), _c(-(pl.y + ay))]
            kind = "keepout" if "tracks" in bad else "via_keepout"
            for l in lnames:
                lines.append(f'    ({kind} "" (polygon {l} 0 {" ".join(pts)} {pts[0]} {pts[1]}))')
    # every via size that can appear: netclass vias AND existing (e.g. fan-out / locked) vias on the board
    vias = sorted({(nc.via_d, nc.via_drill) for nc in d.netclasses.values()} | {(v.d, v.drill) for v in L.vias})
    lines.append('    (via ' + " ".join(_q(via_name(a, b, len(cul))) for a, b in vias) + ')')
    dflt = d.netclasses.get("Default")
    lines.append(f'    (rule (width {_c(dflt.track)}) (clearance {_c(dflt.clearance)}) '
                 f'(clearance {_c(dflt.clearance)} (type default_smd)) (clearance {_c(min(dflt.clearance, 0.1))} (type smd_smd)))')
    lines.append('  )')
    # placement: one image per component, pre-rotated
    lines.append('  (placement')
    for ref in sorted(by_ref):
        lines.append(f'    (component {_q("IMG_" + ref)} (place {_q(ref)} 0 0 front 0))')
    lines.append('  )')
    lines.append('  (library')
    padstacks: dict[str, brd.AbsPad] = {}
    pin_ids: dict[tuple[str, str], list[str]] = defaultdict(list)
    for ref in sorted(by_ref):
        seen: dict[str, int] = defaultdict(int)
        img = [f'    (image {_q("IMG_" + ref)}']
        for p in by_ref[ref]:
            ps = _padstack_name(p)
            padstacks[ps] = p
            num = p.number if p.number else "NP"
            k = seen[num]
            seen[num] += 1
            pid = num if k == 0 else f"{num}@{k}"
            pin_ids[(ref, p.number)].append(f"{ref}-{pid}")
            img.append(f'      (pin {_q(ps)} {_q(pid)} {_c(p.x)} {_c(-p.y)})')
        img.append('    )')
        lines += img
    for ps, p in padstacks.items():
        lines.append(f'    (padstack {_q(ps)} ' + " ".join(_pad_shapes(p)) + ' (attach off))')
    for a, b in vias:
        lines.append(f'    (padstack {_q(via_name(a, b, len(cul)))} ' +
                     " ".join(f"(shape (circle {l} {_c(a)}))" for l in cul) + ' (attach off))')
    lines.append('  )')
    # network
    lines.append('  (network')
    nets = d.nets()
    for net, nodes in nets.items():
        ids = []
        for ref, pin in nodes:
            ids += pin_ids.get((ref, pin), [])
        if ids:
            lines.append(f'    (net {_q(net)} (pins ' + " ".join(ids) + '))')
    classes: dict[str, list[str]] = defaultdict(list)
    for net in nets:
        if net in exclude_nets:
            continue
        classes[d.netclass_of(net).name].append(net)
    for cname, cnets in classes.items():
        nc = d.netclasses.get(cname) or dflt
        lines.append(f'    (class {_q(cname)} ' + " ".join(_q(n) for n in cnets) +
                     f' (circuit (use_via {_q(via_name(nc.via_d, nc.via_drill, len(cul)))}))'
                     f' (rule (width {_c(nc.track)}) (clearance {_c(nc.clearance)})))')
    if exclude_nets:
        # nets we don't want routed (e.g. handled by planes/zones only) -> fixed class w/o layers
        lines.append('    (class "__noroute" ' + " ".join(_q(n) for n in sorted(exclude_nets) if n in nets) +
                     ' (circuit (use_layer )) (rule (width 100)))')
    lines.append('  )')
    # existing (locked) wiring
    lines.append('  (wiring')
    for t in L.tracks:
        if t.locked or protect_all:
            lines.append(f'    (wire (path {t.layer} {_c(t.width)} ' + " ".join(f"{_c(x)} {_c(-y)}" for x, y in t.pts) +
                         f') (net {_q(t.net)}) (type protect))')
    for v in L.vias:
        if v.locked or protect_all:
            lines.append(f'    (via {_q(via_name(v.d, v.drill, len(cul)))} {_c(v.x)} {_c(-v.y)} (net {_q(v.net)}) (type protect))')
    lines.append('  )')
    lines.append(')')
    path.write_text("\n".join(lines) + "\n")
    return {"dsn": str(path), "nets": len(nets), "padstacks": len(padstacks)}


def read_ses(prj: Project, path: Path) -> tuple[list[Track], list[Via]]:
    tree = sx.load(path)
    routes = sx.find(tree, "routes")
    res = sx.find(routes, "resolution")
    scale = float(res[2]) if res else 10.0     # units per um
    f = 1.0 / (scale * UM)
    vias_dims = {}
    lo = sx.find(routes, "library_out")
    if lo:
        for ps in sx.find_all(lo, "padstack"):
            m = re.search(r"_(\d+):(\d+)_um", str(ps[1]))
            if m:
                vias_dims[str(ps[1])] = (int(m.group(1)) / 1000, int(m.group(2)) / 1000)
    tracks, vias = [], []
    no = sx.find(routes, "network_out")
    for net in sx.find_all(no or [], "net"):
        name = str(net[1])
        for w in sx.find_all(net, "wire"):
            pth = sx.find(w, "path")
            layer, width = str(pth[1]), float(pth[2]) * f
            coords = [float(v) for v in pth[3:] if isinstance(v, (int, float))]
            pts = [[round(coords[i] * f, 4), round(-coords[i + 1] * f, 4)] for i in range(0, len(coords) - 1, 2)]
            tracks.append(Track(net=name, layer=layer, pts=pts, width=round(width, 4)))
        for v in sx.find_all(net, "via"):
            dims = vias_dims.get(str(v[1]))
            if dims is None:
                m = re.search(r"_(\d+):(\d+)_um", str(v[1]))
                dims = (int(m.group(1)) / 1000, int(m.group(2)) / 1000) if m else (0.45, 0.2)
            vias.append(Via(net=name, x=round(float(v[2]) * f, 4), y=round(-float(v[3]) * f, 4), d=dims[0], drill=dims[1]))
    return tracks, vias


def _hms(sec: int) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def run_freerouting(dsn: Path, ses: Path, passes: int = 100, timeout_s: int = 600, threads: int = 0, log: Path | None = None,
                    extra: list[str] | None = None) -> dict:
    java = shutil.which("java", path=str(Path(__file__).resolve().parents[1] / ".pixi/envs/default/bin") + os.pathsep + os.environ.get("PATH", ""))
    cmd = [java, "-Djava.awt.headless=true", "-jar", str(FR_JAR), "-de", str(dsn), "-do", str(ses),
           f"--router.autorouter.max_passes={passes}", "--gui.enabled=false",
           # Freerouting's own budget so it always writes a session (we hard-kill only as a backstop)
           f"--router.job_timeout={_hms(timeout_s)}", f"--router.optimizer.timeout={_hms(max(30, timeout_s // 4))}",
           "--router.automatic_neckdown=true", "--router.neck_width_um=90"]   # never neck below the fab minimum   # needed to escape 0.35 mm-pitch QFN rows; fab min is 0.09 mm
    if threads:
        cmd += ["-mt", str(threads)]
    cmd += extra or []
    t0 = time.time()
    if ses.exists():
        ses.unlink()
    log = log or (ses.parent / "freerouting.log")
    with open(log, "w") as lf:
        proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, text=True)
        try:
            proc.wait(timeout=timeout_s + 180)
            timed_out = False
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            timed_out = True
    out = log.read_text(errors="ignore")
    unrouted = violations = None
    for m in re.finditer(r"\((\d+) unrouted and (\d+) violation", out):
        unrouted, violations = int(m.group(1)), int(m.group(2))
    passes_done = len(re.findall(r"Auto-routing pass", out)) or None
    return {"ok": ses.exists(), "seconds": round(time.time() - t0, 1), "timed_out": timed_out,
            "fr_unrouted": unrouted, "fr_violations": violations,
            "log_tail": "\n".join(l for l in out.splitlines() if "INFO" in l or "WARN" in l or "ERROR" in l)[-1500:]}


def route(prj: Project, passes: int = 100, timeout_s: int = 900, keep_existing: bool = False) -> dict:
    d, L = prj.design, prj.layout
    work = prj.root / "out" / "route"
    work.mkdir(parents=True, exist_ok=True)
    planes = {c.layer: c.net for c in d.constraints if c.kind == "plane" and not c.rect and c.layer in ("In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu")}
    if not keep_existing:
        L.tracks = [t for t in L.tracks if t.locked]
        L.vias = [v for v in L.vias if v.locked]
    dsn, ses = work / f"{d.name}.dsn", work / f"{d.name}.ses"
    info = write_dsn(prj, dsn, planes=planes)
    fr = run_freerouting(dsn, ses, passes=passes, timeout_s=timeout_s, log=work / "freerouting.log")
    if not fr["ok"]:
        return {"ok": False, **fr}
    tracks, vias = read_ses(prj, ses)
    locked_t = [t for t in L.tracks if t.locked]
    locked_v = [v for v in L.vias if v.locked]
    key = lambda t: (t.net, t.layer, tuple(map(tuple, t.pts)))
    lk = {key(t) for t in locked_t}
    L.tracks = locked_t + [t for t in tracks if key(t) not in lk]
    vk = {(v.net, round(v.x, 3), round(v.y, 3)) for v in locked_v}
    L.vias = locked_v + [v for v in vias if (v.net, round(v.x, 3), round(v.y, 3)) not in vk]
    # ensure zones exist for plane nets
    for layer, net in planes.items():
        if not any(z.layer == layer and z.net == net for z in L.zones):
            from .ir import Zone
            L.zones.append(Zone(net=net, layer=layer, connect="solid" if net.upper().startswith("GND") else "thermal"))
    _regional_zones(prj)
    L.history.append({"op": "route", "passes": passes, "tracks": len(L.tracks), "vias": len(L.vias), "s": fr["seconds"]})
    return {"ok": True, "tracks": len(L.tracks), "vias": len(L.vias), "seconds": fr["seconds"],
            "fr_unrouted": fr.get("fr_unrouted"), "fr_violations": fr.get("fr_violations"),
            "timed_out": fr["timed_out"], "planes": planes, "dsn": info}


# ------------------------------------------------------------------ deterministic copper helpers
def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


class _Obstacles:
    """Copper obstacles on all layers for through-via legality checks (grid-bucketed)."""

    def __init__(self, prj: Project, cell: float = 2.0):
        self.cell = cell
        self.items = []   # (kind, net, geom, halfwidth)
        self.grid: dict = defaultdict(list)
        for p in brd.abs_pads(prj):
            self._add(("pad", p.net, (p.x - p.hx, p.y - p.hy, p.x + p.hx, p.y + p.hy), 0.0))
            if p.drill:   # drilled holes block vias regardless of net (hole-to-hole rule)
                self._add(("via", "__hole__", (p.x, p.y), p.drill / 2 + 0.15))
        for t in prj.layout.tracks:
            for a, b in zip(t.pts, t.pts[1:]):
                self._add(("seg", t.net, (a[0], a[1], b[0], b[1]), t.width / 2))
        for v in prj.layout.vias:
            self._add(("via", v.net, (v.x, v.y), v.d / 2))
        d = prj.design
        for ref, comp in d.components.items():
            pl = prj.layout.placement.get(ref)
            if pl is None:
                continue
            for z in sx.find_all(libs.get_footprint_side(comp.footprint, pl.side).node, "zone"):
                if sx.find(z, "keepout") is None:
                    continue
                pts = [libs.rot(float(xy[1]), float(xy[2]), pl.rot) for xy in sx.find_all(sx.find(sx.find(z, "polygon") or [], "pts") or [], "xy")]
                if pts:
                    xs, ys = [pl.x + a for a, _ in pts], [pl.y + b for _, b in pts]
                    self._add(("pad", "__keepout__", (min(xs), min(ys), max(xs), max(ys)), 0.0))
        for mh in d.board.mounting_holes:
            r = (mh[2] if len(mh) > 2 else d.board.mh_drill) / 2 + 0.6
            self._add(("via", "", (mh[0], mh[1]), r))

    def _bbox(self, it):
        k, _, g, hw = it
        if k == "pad":
            return g
        if k == "seg":
            return (min(g[0], g[2]) - hw, min(g[1], g[3]) - hw, max(g[0], g[2]) + hw, max(g[1], g[3]) + hw)
        return (g[0] - hw, g[1] - hw, g[0] + hw, g[1] + hw)

    def _add(self, it):
        i = len(self.items)
        self.items.append(it)
        x1, y1, x2, y2 = self._bbox(it)
        for gx in range(int(x1 // self.cell), int(x2 // self.cell) + 1):
            for gy in range(int(y1 // self.cell), int(y2 // self.cell) + 1):
                self.grid[(gx, gy)].append(i)

    def clear(self, x, y, r, net, clearance) -> bool:
        seen = set()
        for gx in range(int((x - r - 1) // self.cell), int((x + r + 1) // self.cell) + 1):
            for gy in range(int((y - r - 1) // self.cell), int((y + r + 1) // self.cell) + 1):
                for i in self.grid.get((gx, gy), ()):
                    if i in seen:
                        continue
                    seen.add(i)
                    k, n, g, hw = self.items[i]
                    need = r + clearance
                    if k == "pad":
                        dx = max(g[0] - x, 0, x - g[2]); dy = max(g[1] - y, 0, y - g[3])
                        dist = math.hypot(dx, dy)
                        if n and n == net:
                            continue
                    elif k == "seg":
                        dist = _seg_dist(x, y, *g) - hw
                    else:
                        dist = math.hypot(x - g[0], y - g[1]) - hw
                    if n == net and k != "pad" and n:
                        need = r + 0.05
                    if dist < need:
                        return False
        return True

    def seg_clear(self, a, b, hw, net, clearance) -> bool:
        n = max(2, int(math.hypot(b[0] - a[0], b[1] - a[1]) / 0.05))
        return all(self.clear(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n, hw, net, clearance)
                   for k in range(1, n + 1))

    def add_via(self, v: Via):
        self._add(("via", v.net, (v.x, v.y), v.d / 2))


def stitch_vias(prj: Project, net: str = "GND", pitch: float = 5.0, edge: float = 1.0, d: float = 0.45,
                drill: float = 0.25, clearance: float = 0.2, exclude_rects: list | None = None,
                edge_pitch: float = 2.5, avoid_parts: bool = True) -> dict:
    """Expert-style ground stitching: a via fence along the board edge (edge_pitch) plus a sparse interior grid
    (pitch). No vias under any part's courtyard (either side) or inside keepout constraints."""
    D = prj.design
    obs = _Obstacles(prj)
    blocked = list(exclude_rects or [])
    for c in D.constraints:
        if c.kind == "keepout" and c.rect:
            blocked.append(tuple(c.rect))
    if avoid_parts:
        for ref, comp in D.components.items():
            pl = prj.layout.placement.get(ref)
            if pl is None:
                continue
            bb = libs.bbox_rot(libs.get_footprint_side(comp.footprint, pl.side).courtyard, pl.rot)
            blocked.append((pl.x + bb[0] - 0.1, pl.y + bb[1] - 0.1, pl.x + bb[2] + 0.1, pl.y + bb[3] + 0.1))
    W, H = D.board.width, D.board.height
    pts = []
    # edge fence (inset by `edge`), corners rounded off by the outline radius
    r = max(D.board.corner_r, edge)
    for x in [edge + r + k * edge_pitch for k in range(int((W - 2 * (edge + r)) / edge_pitch) + 1)]:
        pts += [(x, edge), (x, H - edge)]
    for y in [edge + r + k * edge_pitch for k in range(int((H - 2 * (edge + r)) / edge_pitch) + 1)]:
        pts += [(edge, y), (W - edge, y)]
    # sparse interior grid
    y = edge + pitch
    while y <= H - edge - pitch / 2:
        x = edge + pitch
        while x <= W - edge - pitch / 2:
            pts.append((x, y))
            x += pitch
        y += pitch
    added = 0
    for x, y in pts:
        if any(rr[0] <= x <= rr[2] and rr[1] <= y <= rr[3] for rr in blocked):
            continue
        if obs.clear(x, y, d / 2, net, clearance):
            v = Via(net=net, x=round(x, 3), y=round(y, 3), d=d, drill=drill, locked=True)
            prj.layout.vias.append(v)
            obs.add_via(v)
            added += 1
    return {"added": added}


def plane_fanout(prj: Project, nets: list[str], d: float = 0.45, drill: float = 0.25, clearance: float | None = None,
                 width: float = 0.25) -> dict:
    """For SMD pads on plane nets: drop a via right next to the pad (short stub) if none is near.
    Clearance = max of all netclass clearances (conservative, so pre-placed copper never violates)."""
    obs = _Obstacles(prj)
    clr = clearance if clearance is not None else max(nc.clearance for nc in prj.design.netclasses.values())
    pads = [p for p in brd.abs_pads(prj) if p.net in nets and len(p.layers) == 1]
    near_v = defaultdict(list)
    for v in prj.layout.vias:
        near_v[v.net].append((v.x, v.y))
    # same-footprint large pad of the same net (e.g. QFN exposed pad): tie inward with a pad-width stub
    allp = brd.abs_pads(prj)
    big = defaultdict(list)
    for q in allp:
        if q.net in nets and q.w * q.h > 4.0:
            big[(q.ref, q.net)].append(q)
    added, failed = 0, []
    for p in pads:
        ok = False
        for q in big.get((p.ref, p.net), []):
            if q is p or q.number == p.number and abs(q.x - p.x) < 1e-6:
                continue
            tx = min(max(p.x, q.x - q.hx), q.x + q.hx)
            ty = min(max(p.y, q.y - q.hy), q.y + q.hy)
            w = round(min(p.w, p.h) * 0.9, 3)
            if math.hypot(tx - p.x, ty - p.y) < 3.0 and obs.seg_clear((p.x, p.y), (tx, ty), w / 2, p.net, 0.1):
                prj.layout.tracks.append(Track(net=p.net, layer=p.layers[0], pts=[[round(p.x, 4), round(p.y, 4)],
                                                                                   [round(tx, 4), round(ty, 4)]], width=w, locked=True))
                obs._add(("seg", p.net, (p.x, p.y, tx, ty), w / 2))
                ok = True
                added += 1
                break
        if ok:
            continue
        # reuse a nearby same-net via if a clear short stub reaches it
        for vx, vy in sorted(near_v[p.net], key=lambda q: math.hypot(q[0] - p.x, q[1] - p.y))[:3]:
            if math.hypot(vx - p.x, vy - p.y) > max(p.hx, p.hy) + 1.2:
                break
            if obs.seg_clear((p.x, p.y), (vx, vy), width / 2, p.net, clr):
                prj.layout.tracks.append(Track(net=p.net, layer=p.layers[0], pts=[[round(p.x, 4), round(p.y, 4)], [vx, vy]],
                                               width=width, locked=True))
                obs._add(("seg", p.net, (p.x, p.y, vx, vy), width / 2))
                ok = True
                break
        if ok:
            continue
        for dist in (0.0, 0.25, 0.5, 0.8, 1.1):
            for ang in range(0, 360, 30):
                a = math.radians(ang)
                vx = p.x + math.cos(a) * (p.hx + d / 2 + clr + dist)
                vy = p.y + math.sin(a) * (p.hy + d / 2 + clr + dist)
                # stub leaves the pad from its edge point towards the via
                ex = p.x + max(-p.hx, min(p.hx, vx - p.x))
                ey = p.y + max(-p.hy, min(p.hy, vy - p.y))
                if obs.clear(vx, vy, d / 2, p.net, clr) and obs.seg_clear((ex, ey), (vx, vy), width / 2, p.net, clr):
                    v = Via(net=p.net, x=round(vx, 3), y=round(vy, 3), d=d, drill=drill, locked=True)
                    prj.layout.vias.append(v)
                    prj.layout.tracks.append(Track(net=p.net, layer=p.layers[0], pts=[[round(p.x, 4), round(p.y, 4)], [v.x, v.y]],
                                                   width=width, locked=True))
                    obs.add_via(v)
                    obs._add(("seg", p.net, (p.x, p.y, v.x, v.y), width / 2))
                    near_v[p.net].append((v.x, v.y))
                    added += 1
                    ok = True
                    break
            if ok:
                break
        if not ok:
            failed.append(f"{p.ref}.{p.number}")
    return {"added": added, "failed": failed[:20], "n_failed": len(failed)}



def _regional_zones(prj: Project) -> None:
    from .ir import Zone
    for c in prj.design.constraints:
        if c.kind == "plane" and c.rect:
            x1, y1, x2, y2 = c.rect
            poly = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
            if not any(z.net == c.net and z.layer == c.layer and z.poly == poly for z in prj.layout.zones):
                prj.layout.zones.append(Zone(net=c.net, layer=c.layer, poly=poly, priority=2, connect="solid"))


def route_placements(prj: Project, candidates: list[dict], passes: int = 100, timeout_s: int = 900,
                     fanout_planes: bool = True, cpus: int | None = None) -> dict:
    """Real diversity: route several *different placements* concurrently (Freerouting's autorouter is
    deterministic; its -us/-is strategy flags only affect the optimizer, so strategy portfolios on one
    placement return identical boards).  Keeps the best routed candidate."""
    import copy
    from concurrent.futures import ThreadPoolExecutor
    from .ir import Placement, Zone
    d = prj.design
    planes = {c.layer: c.net for c in d.constraints if c.kind == "plane" and not c.rect and c.layer in ("In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu")}
    plane_nets = sorted(set(planes.values()) | {c.net for c in d.constraints if c.kind == "plane" and c.rect})
    cpus = cpus or max(1, (os.cpu_count() or 2) - 2)
    thr = max(1, cpus // max(1, len(candidates)))

    def one(i_cand):
        i, cand = i_cand
        work = prj.root / "out" / "route" / f"cand{i:02d}"
        work.mkdir(parents=True, exist_ok=True)
        sub = copy.copy(prj)
        sub.layout = prj.layout.model_copy(deep=True)
        sub.layout.placement = {k: Placement(**v) for k, v in cand["placement"].items()}
        sub.layout.tracks, sub.layout.vias = [], []
        fo = plane_fanout(sub, plane_nets) if fanout_planes and plane_nets else {"added": 0}
        dsn, ses = work / f"{d.name}.dsn", work / f"{d.name}.ses"
        write_dsn(sub, dsn, planes=planes)
        r = run_freerouting(dsn, ses, passes=passes, timeout_s=timeout_s, threads=thr, log=work / "freerouting.log")
        r.update(cand=i, seed=cand.get("seed"), fanout=fo.get("added"), fanout_failed=fo.get("n_failed"))
        if r["ok"]:
            t, v = read_ses(sub, ses)
            locked_t = [x for x in sub.layout.tracks if x.locked]
            locked_v = [x for x in sub.layout.vias if x.locked]
            kk = lambda x: (x.net, x.layer, tuple(map(tuple, x.pts)))
            lk = {kk(x) for x in locked_t}
            vk = {(x.net, round(x.x, 3), round(x.y, 3)) for x in locked_v}
            sub.layout.tracks = locked_t + [x for x in t if kk(x) not in lk]
            sub.layout.vias = locked_v + [x for x in v if (x.net, round(x.x, 3), round(x.y, 3)) not in vk]
            r["n_vias"] = len(sub.layout.vias)
            r["len_mm"] = round(sum(math.hypot(a[0] - b[0], a[1] - b[1]) for tt in sub.layout.tracks
                                    for a, b in zip(tt.pts, tt.pts[1:])), 1)
            r["_layout"] = sub.layout
        return r

    with ThreadPoolExecutor(max_workers=len(candidates)) as ex:
        results = list(ex.map(one, enumerate(candidates)))
    table = [{k: r.get(k) for k in ("cand", "seed", "fanout", "fanout_failed", "fr_unrouted", "fr_violations", "n_vias",
                                     "len_mm", "seconds")} for r in results]
    ok = [r for r in results if r["ok"]]
    if not ok:
        return {"ok": False, "candidates": table}
    # combined: routing completeness AND placement quality (decap/SW-node/crystal proximity) - a fully routed
    # board with a 15 mm buck switch node is worse than one with a single unrouted net.
    for r in ok:
        r["placement_score"] = round(candidates[r["cand"]].get("score", 0.0), 1)
        r["cost"] = round(200.0 * (r["fr_unrouted"] if r["fr_unrouted"] is not None else 999) +
                          100.0 * (r["fr_violations"] or 0) + r["placement_score"] + 0.5 * r["n_vias"], 1)
    best = min(ok, key=lambda r: r["cost"])
    table = [{**t, "placement_score": next((r.get("placement_score") for r in ok if r["cand"] == t["cand"]), None),
              "cost": next((r.get("cost") for r in ok if r["cand"] == t["cand"]), None)} for t in table]
    prj.layout = best["_layout"]
    for layer, net in planes.items():
        if not any(z.layer == layer and z.net == net and not z.poly for z in prj.layout.zones):
            prj.layout.zones.append(Zone(net=net, layer=layer, connect="solid" if net.upper().startswith("GND") else "thermal"))
    _regional_zones(prj)
    prj.layout.history.append({"op": "route_placements", "best_cand": best["cand"], "n": len(candidates)})
    return {"ok": True, "best_cand": best["cand"], "best_seed": best.get("seed"), "fr_unrouted": best["fr_unrouted"],
            "fr_violations": best["fr_violations"], "tracks": len(prj.layout.tracks), "vias": len(prj.layout.vias),
            "candidates": table}



def complete(prj: Project, passes: int = 100, timeout_s: int = 600, workdir: Path | None = None, threads: int = 0) -> dict:
    """Incremental completion: all existing copper is protected; only missing connections are routed.
    Regional (split) planes are dropped from the DSN so their nets can be stitched with real tracks."""
    d, L = prj.design, prj.layout
    work = workdir or (prj.root / "out" / "route" / "complete")
    work.mkdir(parents=True, exist_ok=True)
    planes = {c.layer: c.net for c in d.constraints if c.kind == "plane" and not c.rect and c.layer in ("In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu")}
    dsn, ses = work / f"{d.name}.dsn", work / f"{d.name}.ses"
    write_dsn(prj, dsn, planes=planes, protect_all=True, regional_planes=False)
    fr = run_freerouting(dsn, ses, passes=passes, timeout_s=timeout_s, log=work / "freerouting.log", threads=threads)
    if not fr["ok"]:
        return {"ok": False, **{k: fr[k] for k in ("seconds", "timed_out", "fr_unrouted")}}
    t, v = read_ses(prj, ses)
    key = lambda x: (x.net, x.layer, tuple(tuple(round(c, 3) for c in p) for p in x.pts))
    have = {key(x) for x in L.tracks}
    new_t = [x for x in t if key(x) not in have]
    vk = {(x.net, round(x.x, 3), round(x.y, 3)) for x in L.vias}
    new_v = [x for x in v if (x.net, round(x.x, 3), round(x.y, 3)) not in vk]
    L.tracks += new_t
    L.vias += new_v
    L.history.append({"op": "complete", "new_tracks": len(new_t), "new_vias": len(new_v)})
    return {"ok": True, "new_tracks": len(new_t), "new_vias": len(new_v), "fr_unrouted": fr["fr_unrouted"],
            "fr_violations": fr["fr_violations"], "seconds": fr["seconds"]}



def remove_free_vias(prj: Project, net: str = "GND") -> int:
    """Remove `net` vias that no track touches (i.e. stitching vias), so they can be re-placed."""
    ends = {(round(p[0], 3), round(p[1], 3)) for t in prj.layout.tracks if t.net == net for p in t.pts}
    keep, removed = [], 0
    for v in prj.layout.vias:
        if v.net == net and (round(v.x, 3), round(v.y, 3)) not in ends:
            removed += 1
            continue
        keep.append(v)
    prj.layout.vias = keep
    return removed


def parallel_repair(prj: Project, variants: int = 24, passes: int = 100, timeout_s: int = 600, seed: int = 0) -> dict:
    """Close the last unconnected nets: many concurrent (rip-up neighbourhood x Freerouting completion) variants,
    each scored by real KiCad DRC in its own sandbox; the best (fewest errors, then unconnected) is applied."""
    import json as _json
    import random
    import shutil
    from concurrent.futures import ThreadPoolExecutor
    from . import checks as _checks, schematic as _sch
    from .ir import Project as _P, Layout as _L
    drc0 = _checks.run_drc(prj, parity=False)
    data = _json.loads(Path(drc0["report"]).read_text())
    spots, bad_nets = [], set()
    for u in data.get("unconnected_items", []):
        for it in u.get("items", []):
            pos = it.get("pos", {})
            spots.append((pos.get("x", 0), pos.get("y", 0)))
            desc = it.get("description", "")
            if "[" in desc:
                bad_nets.add(desc.split("[")[-1].split("]")[0])
    for v in data.get("violations", []):
        if v.get("severity") == "error":
            for it in v.get("items", []):
                desc = it.get("description", "")
                if "[" in desc and "Track" in desc:
                    bad_nets.add(desc.split("[")[-1].split("]")[0])
    planes = {c.net for c in prj.design.constraints if c.kind == "plane"}
    bad_nets -= planes
    if not bad_nets:
        return {"ok": True, "note": "nothing to repair", "unconnected": drc0["unconnected"], "errors": drc0["errors"]}
    # nets with copper near the trouble spots, by distance
    near: dict[str, float] = {}
    for t in prj.layout.tracks:
        if t.net in planes:
            continue
        for p in t.pts:
            for sx_, sy_ in spots:
                dd = math.hypot(p[0] - sx_, p[1] - sy_)
                if dd < near.get(t.net, 1e9):
                    near[t.net] = dd
    rnd = random.Random(seed)
    cfgs = []
    for k in range(variants):
        radius = [1.0, 2.0, 3.0, 4.0, 6.0][k % 5]
        frac = [1.0, 0.7, 0.5][(k // 5) % 3]
        extra = [n for n, dd in near.items() if dd <= radius and n not in bad_nets and rnd.random() <= frac]
        cfgs.append((k, sorted(bad_nets | set(extra)), radius, frac))
    base = prj.root / "out" / "repair"
    if base.exists():
        shutil.rmtree(base)
    thr = max(1, ((os.cpu_count() or 2) - 2) // variants)

    def one(cfg):
        k, rip, radius, frac = cfg
        d = base / f"v{k:02d}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "design.json").write_text(prj.design.model_dump_json())
        lay = prj.layout.model_copy(deep=True)
        lay.tracks = [t for t in lay.tracks if t.net not in rip]
        lay.vias = [v for v in lay.vias if v.net not in rip]
        (d / "layout.json").write_text(lay.model_dump_json())
        sub = _P(d)
        fo = plane_fanout(sub, sorted(planes)) if planes else {}
        fin = complete(sub, passes=passes, timeout_s=timeout_s, workdir=d / "fr", threads=thr)
        mn = _sch.FAB_RULES.get(sub.design.board.fab, _sch.FAB_RULES["jlcpcb"])["min_track_width"]
        for t in sub.layout.tracks:          # widen router neck-down artefacts before scoring
            if t.width < mn - 1e-6:
                t.width = mn
        sub.save()
        _sch.write_project_file(sub)
        brd.build(sub)
        r = _checks.run_drc(sub, parity=False)
        return {"k": k, "ripped": len(rip), "radius": radius, "frac": frac, "errors": r.get("errors"),
                "unconnected": r.get("unconnected"), "fr_unrouted": fin.get("fr_unrouted"), "_layout": sub.layout}

    with ThreadPoolExecutor(max_workers=variants) as ex:
        res = list(ex.map(one, cfgs))
    ok = [r for r in res if r.get("errors") is not None]
    best = min(ok, key=lambda r: (r["errors"] + r["unconnected"], r["errors"]))
    before = drc0["errors"] + drc0["unconnected"]
    applied = best["errors"] + best["unconnected"] < before
    if applied:
        prj.layout = best["_layout"]
    return {"ok": True, "before": {"errors": drc0["errors"], "unconnected": drc0["unconnected"]},
            "best": {k: best[k] for k in ("k", "ripped", "radius", "frac", "errors", "unconnected")}, "applied": applied,
            "variants": sorted([{k: r[k] for k in ("k", "ripped", "errors", "unconnected")} for r in ok],
                               key=lambda r: r["errors"] + r["unconnected"])[:10]}



def remove_dangling_vias(prj: Project) -> int:
    """Remove vias that connect copper on fewer than two layers (router leftovers). Vias of nets that have a pour
    (plane / zone) are kept: the pour supplies the second layer."""
    def in_zone(v):
        for z in prj.layout.zones:
            if z.net != v.net:
                continue
            if not z.poly:
                return True
            xs = [q[0] for q in z.poly]; ys = [q[1] for q in z.poly]
            if min(xs) <= v.x <= max(xs) and min(ys) <= v.y <= max(ys):
                return True
        return False
    ends = defaultdict(set)
    for t in prj.layout.tracks:
        for p in t.pts:
            ends[(t.net, round(p[0], 3), round(p[1], 3))].add(t.layer)
    pads = brd.abs_pads(prj)
    keep, removed = [], 0
    for v in prj.layout.vias:
        if in_zone(v):
            keep.append(v)
            continue
        layers = set(ends.get((v.net, round(v.x, 3), round(v.y, 3)), set()))
        for p in pads:
            if p.net == v.net and abs(p.x - v.x) <= p.hx and abs(p.y - v.y) <= p.hy:
                layers |= set(p.layers)
        if len(layers) >= 2:
            keep.append(v)
        else:
            removed += 1
    prj.layout.vias = keep
    return removed


def prune_stubs(prj: Project) -> int:
    """Iteratively trim track ends that touch nothing (no pad, via, zone-free end or other track of the net)."""
    pads = brd.abs_pads(prj)
    removed = 0
    for _ in range(50):
        pts_count = defaultdict(int)
        for t in prj.layout.tracks:
            for p in (t.pts[0], t.pts[-1]):
                pts_count[(t.net, t.layer, round(p[0], 3), round(p[1], 3))] += 1
            for p in t.pts[1:-1]:
                pts_count[(t.net, t.layer, round(p[0], 3), round(p[1], 3))] += 2
        vias = {(v.net, round(v.x, 3), round(v.y, 3)) for v in prj.layout.vias}

        segs_by = defaultdict(list)
        for t2 in prj.layout.tracks:
            for a, b in zip(t2.pts, t2.pts[1:]):
                segs_by[(t2.net, t2.layer)].append((a, b, t2.width, id(t2)))

        def anchored(t, p):
            key = (t.net, t.layer, round(p[0], 3), round(p[1], 3))
            if pts_count[key] > 1 or (t.net, round(p[0], 3), round(p[1], 3)) in vias:
                return True
            for a, b, w, tid in segs_by[(t.net, t.layer)]:      # T-junction onto another track of the net
                if tid == id(t):
                    continue
                if _seg_dist(p[0], p[1], a[0], a[1], b[0], b[1]) <= w / 2 + 1e-3:
                    return True
            for v in prj.layout.vias:
                if v.net == t.net and math.hypot(v.x - p[0], v.y - p[1]) <= v.d / 2:
                    return True
            for q in pads:
                if q.net == t.net and t.layer in q.layers and abs(q.x - p[0]) <= q.hx + 1e-3 and abs(q.y - p[1]) <= q.hy + 1e-3:
                    return True
            return False
        changed = False
        keep = []
        for t in prj.layout.tracks:
            pts = list(t.pts)
            while len(pts) >= 2 and not anchored(t, pts[-1]):
                pts.pop(); changed = True; removed += 1
            while len(pts) >= 2 and not anchored(t, pts[0]):
                pts.pop(0); changed = True; removed += 1
            if len(pts) >= 2:
                t.pts = pts
                keep.append(t)
            else:
                changed = True
        prj.layout.tracks = keep
        if not changed:
            break
    return removed
