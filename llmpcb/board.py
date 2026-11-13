"""IR + Layout -> .kicad_pcb (written directly as s-expressions; no pcbnew/SWIG).

We emit the KiCad 9 file format (version 20241229, explicit net table), which KiCad 10
loads natively; kicad-cli is used for zone fill, DRC and outputs.
"""
from __future__ import annotations

import copy
import math
import uuid
from dataclasses import dataclass

from . import libs
from . import sexpr as sx
from .ir import Project
from .schematic import _safe, sheet_path, sym_uuid
from .sexpr import S, Sym

PCB_VERSION = 20241229

CU = {2: ["F.Cu", "B.Cu"], 4: ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"], 6: ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"]}
CU_ID = {"F.Cu": 0, "B.Cu": 2, "In1.Cu": 4, "In2.Cu": 6, "In3.Cu": 8, "In4.Cu": 10}


def _uid(*parts) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "llmpcb-pcb/" + "/".join(map(str, parts))))


def copper_layers(n: int) -> list[str]:
    return CU[n]


# ------------------------------------------------------------------ geometry helpers
@dataclass
class AbsPad:
    ref: str
    number: str
    net: str
    x: float
    y: float
    hx: float          # half extents after rotation (axis aligned)
    hy: float
    layers: list[str]  # copper layers
    shape: str
    drill: float
    w: float
    h: float
    angle: float       # total angle


KEEP_ISLANDS = False   # analysis fills (fills.stitch_islands) keep floating islands


def abs_pads(prj: Project) -> list[AbsPad]:
    d, L = prj.design, prj.layout
    ncu = copper_layers(d.board.layers)
    out = []
    for ref, c in d.components.items():
        pl = L.placement.get(ref)
        if pl is None:
            continue
        fp = libs.get_footprint_side(c.footprint, pl.side)
        for p in fp.pads:
            cu = [l for l in p.layers if l.endswith(".Cu")]
            if "*.Cu" in p.layers or (p.kind in ("thru_hole", "np_thru_hole") and not cu):
                cu = list(ncu)
            if not cu:
                continue
            x, y = libs.pad_abs(p, pl.x, pl.y, pl.rot)
            hx, hy = libs.pad_extent(p, pl.rot)
            out.append(AbsPad(ref, p.number, c.pins.get(p.number, "") if p.kind != "np_thru_hole" else "",
                              x, y, hx, hy, cu, p.shape, p.drill, p.w, p.h, (p.angle + pl.rot) % 360))
    return out


def outline_poly(d, inset: float = 0.0) -> list[tuple[float, float]]:
    """Board outline (rounded rectangle); `inset` shrinks it uniformly (used for router keep-in)."""
    b = d.board
    r = max(0.0, b.corner_r - inset)
    x0, y0, W, H = inset, inset, b.width - inset, b.height - inset
    if r <= 0:
        return [(x0, y0), (W, y0), (W, H), (x0, H)]
    pts = []
    for cx, cy, a0 in ((W - r, y0 + r, -90), (W - r, H - r, 0), (x0 + r, H - r, 90), (x0 + r, y0 + r, 180)):
        for k in range(7):
            a = math.radians(a0 + k * 15)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


# ------------------------------------------------------------------ writer
def _layers_node(n: int) -> list:
    cu = [S(CU_ID[l], l, Sym("signal")) for l in CU[n]]
    tech = [S(9, "F.Adhes", Sym("user"), "F.Adhesive"), S(11, "B.Adhes", Sym("user"), "B.Adhesive"),
            S(13, "F.Paste", Sym("user")), S(15, "B.Paste", Sym("user")),
            S(5, "F.SilkS", Sym("user"), "F.Silkscreen"), S(7, "B.SilkS", Sym("user"), "B.Silkscreen"),
            S(1, "F.Mask", Sym("user")), S(3, "B.Mask", Sym("user")),
            S(17, "Dwgs.User", Sym("user"), "User.Drawings"), S(19, "Cmts.User", Sym("user"), "User.Comments"),
            S(21, "Eco1.User", Sym("user"), "User.Eco1"), S(23, "Eco2.User", Sym("user"), "User.Eco2"),
            S(25, "Edge.Cuts", Sym("user")), S(27, "Margin", Sym("user")),
            S(31, "F.CrtYd", Sym("user"), "F.Courtyard"), S(29, "B.CrtYd", Sym("user"), "B.Courtyard"),
            S(35, "F.Fab", Sym("user")), S(33, "B.Fab", Sym("user"))]
    return S("layers", *cu, *tech)


def _stackup(n: int, thickness: float) -> list:
    cu = CU[n]
    ndiel = len(cu) - 1
    cu_t = 0.035
    if n == 4:   # JLC 7628 4-layer: 0.2104 prepreg / 1.065 core
        diel = [(0.2104, "prepreg"), (thickness - 2 * 0.2104 - 4 * cu_t - 0.02, "core"), (0.2104, "prepreg")]
    else:
        each = (thickness - len(cu) * cu_t - 0.02) / ndiel
        diel = [(each, "core" if i % 2 else "prepreg") for i in range(ndiel)]
    items = [S("layer", "F.SilkS", S("type", "Top Silk Screen")), S("layer", "F.Paste", S("type", "Top Solder Paste")),
             S("layer", "F.Mask", S("type", "Top Solder Mask"), S("thickness", 0.01))]
    for i, l in enumerate(cu):
        items.append(S("layer", l, S("type", "copper"), S("thickness", cu_t)))
        if i < ndiel:
            t, kind = diel[i]
            items.append(S("layer", f"dielectric {i+1}", S("type", kind), S("thickness", round(t, 4)),
                           S("material", "FR4"), S("epsilon_r", 4.4), S("loss_tangent", 0.02)))
    items += [S("layer", "B.Mask", S("type", "Bottom Solder Mask"), S("thickness", 0.01)),
              S("layer", "B.Paste", S("type", "Bottom Solder Paste")), S("layer", "B.SilkS", S("type", "Bottom Silk Screen")),
              S("copper_finish", "ENIG"), S("dielectric_constraints", Sym("no"))]
    return S("stackup", *items)


def _xform_footprint(node: list, ref: str, value: str, x: float, y: float, rot: float, netmap: dict[str, tuple[int, str]],
                     path: str, sheetname: str, sheetfile: str, extra_props: dict, board_only=False, dnp=False,
                     side: str = "top", hide_ref: bool = False, ref_at: tuple | None = None) -> list:
    fp = copy.deepcopy(node)
    for k in ("version", "generator", "generator_version"):
        sx.remove(fp, k)
    body = fp[2:]
    out = [Sym("footprint"), fp[1], S("layer", "B.Cu" if side == "bottom" else "F.Cu"), S("uuid", _uid("fp", ref)),
           S("at", x, y, rot) if rot else S("at", x, y)]
    for c in body:
        if not isinstance(c, list):
            continue
        h = sx.head(c)
        if h == "layer":
            continue
        if h == "property":
            if c[1] == "Reference":
                c[2] = ref
                if hide_ref:      # small passive: keep the designator on the fab layer only
                    lay = sx.find(c, "layer")
                    if lay is not None:
                        lay[1] = "B.Fab" if str(lay[1]).startswith("B.") else "F.Fab"
                elif ref_at:      # silkscreen legaliser: absolute position, reading angle, uniform size
                    X, Y, ang, size = ref_at
                    lx, ly = libs.rot(X - x, Y - y, -rot)
                    at = sx.find(c, "at")
                    at[:] = [Sym("at"), round(lx, 4), round(ly, 4), ang]
                    font = sx.find(sx.find(c, "effects") or [], "font")
                    if font is not None:
                        sx.remove(font, "size"); sx.remove(font, "thickness")
                        font += [S("size", size, size), S("thickness", 0.12)]
                    out.append(c)
                    continue
            elif c[1] == "Value":
                c[2] = value
            elif c[1] in extra_props:
                c[2] = extra_props.pop(c[1])
            at = sx.find(c, "at")
            if at is not None and rot:
                ang = (float(at[3]) if len(at) > 3 else 0.0) + rot
                at[:] = [Sym("at"), at[1], at[2], ang % 360]
        if h == "fp_text":
            at = sx.find(c, "at")
            if at is not None and rot:
                ang = (float(at[3]) if len(at) > 3 else 0.0) + rot
                at[:] = [Sym("at"), at[1], at[2], ang % 360]
        if h == "pad":
            at = sx.find(c, "at")
            if rot:
                ang = (float(at[3]) if len(at) > 3 else 0.0) + rot
                at[:] = [Sym("at"), at[1], at[2], ang % 360]
            num = str(c[1])
            if num in netmap and str(c[2]) != "np_thru_hole":
                nid, nname = netmap[num]
                sx.remove(c, "net")
                # insert net after layers
                c.append(S("net", nid, nname))
            sx.remove(c, "uuid")
            c.append(S("uuid", _uid("pad", ref, num, at[1], at[2])))
        if h == "zone":   # footprint zones are stored in ABSOLUTE board coordinates in .kicad_pcb
            for poly in sx.find_all(c, "polygon"):
                pts = sx.find(poly, "pts")
                for xy in sx.find_all(pts, "xy"):
                    ax, ay = libs.rot(float(xy[1]), float(xy[2]), rot)
                    xy[1], xy[2] = round(x + ax, 4), round(y + ay, 4)
            sx.remove(c, "uuid")
            c.append(S("uuid", _uid("fpzone", ref, len(out))))
        if h == "attr" and (board_only or dnp):
            extra = [a for a in c[1:]]
            if board_only and "board_only" not in extra:
                extra += [Sym("board_only"), Sym("exclude_from_pos_files"), Sym("exclude_from_bom")]
            if dnp and "dnp" not in extra:
                extra.append(Sym("dnp"))
            c = S("attr", *extra)
        out.append(c)
    for k, v in extra_props.items():
        out.append(S("property", k, v, S("at", 0, 0, 0), S("layer", "B.Fab" if side == "bottom" else "F.Fab"), S("hide", Sym("yes")),
                     S("effects", S("font", S("size", 1, 1), S("thickness", 0.15)))))
    if path:
        out += [S("path", path), S("sheetname", sheetname), S("sheetfile", sheetfile)]
    return out


def _nc_netnames(prj: Project) -> dict:
    """(ref, pin) -> 'unconnected-(...)' net names exactly as KiCad's schematic netlist names them, so
    intentionally unconnected pads match the schematic (DRC schematic parity)."""
    path = prj.root / "out" / "netlist.net"
    out = {}
    if not path.exists():
        return out
    try:
        tree = sx.load(path)
        for net in sx.find_all(sx.find(tree, "nets"), "net"):
            name = str(sx.find(net, "name")[1])
            if name.startswith("unconnected-"):
                for node in sx.find_all(net, "node"):
                    out[(str(sx.find(node, "ref")[1]), str(sx.find(node, "pin")[1]))] = name
    except Exception:  # noqa: BLE001
        return {}
    return out


def build(prj: Project) -> dict:
    d, L = prj.design, prj.layout
    ncmap = _nc_netnames(prj)
    nets = sorted(set(d.nets()) | set(ncmap.values()))
    netid = {n: i + 1 for i, n in enumerate(nets)}
    items: list = []
    ncu = d.board.layers
    root_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, "llmpcb/" + "/".join([d.name, "root"])))
    missing = []
    from . import silk as _silk
    try:
        sp = _silk.plan(prj)
    except Exception:  # noqa: BLE001  (never block board writing on cosmetics)
        sp = None
    for ref, c in sorted(d.components.items()):
        pl = L.placement.get(ref)
        if pl is None:
            missing.append(ref)
            continue
        fp = libs.get_footprint_side(c.footprint, pl.side)
        netmap = {p.number: (netid[c.pins[p.number]], c.pins[p.number]) for p in fp.pads if c.pins.get(p.number)}
        for p in fp.pads:
            nn = ncmap.get((ref, p.number))
            if nn and p.number not in netmap:
                netmap[p.number] = (netid[nn], nn)
        cx1, cy1, cx2, cy2 = fp.courtyard
        small = (cx2 - cx1) * (cy2 - cy1) < 4.5 and getattr(d.board, "silk_refs", "all") == "large"
        path = f"{sheet_path(d, c.block)}/{sym_uuid(d, ref, 1)}"
        props = {"Datasheet": str(libs.get_symbol(c.symbol).props.get("Datasheet", ""))}
        props.update(c.fields)
        if c.mpn:
            props["MPN"] = c.mpn
        if c.lcsc:
            props["LCSC"] = c.lcsc
        items.append(_xform_footprint(fp.node, ref, c.value, round(pl.x, 4), round(pl.y, 4), pl.rot % 360, netmap,
                                      path, f"/{c.block}/", f"{_safe(c.block)}.kicad_sch", props, dnp=c.dnp,
                                      side=pl.side,
                                      hide_ref=(sp["refs"].get(ref) is None) if sp else small,
                                      ref_at=sp["refs"].get(ref) if sp else None))
    # mounting holes (board-only)
    for i, mh in enumerate(d.board.mounting_holes):
        fpid = "MountingHole:MountingHole_3.2mm_M3" if (len(mh) < 3 or mh[2] >= 3.0) else "MountingHole:MountingHole_2.2mm_M2"
        fp = libs.get_footprint(fpid)
        items.append(_xform_footprint(fp.node, f"H{i+1}", "MountingHole", mh[0], mh[1], 0, {}, "", "", "", {},
                                      board_only=True, hide_ref=True))
    # global fiducials (board-only)
    for i, fd in enumerate(d.board.fiducials):
        side = fd[2] if len(fd) > 2 else "top"
        fp = libs.get_footprint_side("Fiducial:Fiducial_1mm_Mask2mm", side)
        items.append(_xform_footprint(fp.node, f"FID{i+1}", "Fiducial", fd[0], fd[1], 0, {}, "", "", "", {},
                                      board_only=True, side=side, hide_ref=True))
    # silkscreen labels (connector names, pin-outs, button functions, ...)
    for i, st in enumerate(d.board.silk_texts):
        st = dict(st)
        if sp and i < len(sp["texts"]) and sp["texts"][i]:
            st["x"], st["y"], st["size"] = sp["texts"][i]
            st.pop("ref", None)
            st.setdefault("side", L.placement[d.board.silk_texts[i]["ref"]].side
                          if d.board.silk_texts[i].get("ref") in L.placement else "top")
        if st.get("ref"):     # anchored to a part: follows it through re-placement
            pl_ = L.placement.get(st["ref"])
            if pl_ is None:
                continue
            st["x"] = round(pl_.x + st.get("dx", 0.0), 3)
            st["y"] = round(pl_.y + st.get("dy", 0.0), 3)
            st.setdefault("side", pl_.side)
        bottom = st.get("side", "top") == "bottom"
        eff = S("effects", S("font", S("size", st.get("size", 0.8), st.get("size", 0.8)), S("thickness", 0.12)))
        if bottom:
            eff.append(S("justify", Sym("mirror")))
        items.append(S("gr_text", st["text"], S("at", st["x"], st["y"], st.get("rot", 0)),
                       S("layer", "B.SilkS" if bottom else "F.SilkS"), S("uuid", _uid("silk", i)), eff))
    # outline
    poly = outline_poly(d)
    for i in range(len(poly)):
        a, b = poly[i], poly[(i + 1) % len(poly)]
        items.append(S("gr_line", S("start", round(a[0], 4), round(a[1], 4)), S("end", round(b[0], 4), round(b[1], 4)),
                       S("stroke", S("width", 0.1), S("type", Sym("solid"))), S("layer", "Edge.Cuts"),
                       S("uuid", _uid("edge", i))))
    if d.board.title:
        # board title on the bottom silkscreen (mirrored, readable from below), centred near the lower edge
        tx, ty = (sp["title"][0], sp["title"][1]) if sp and sp.get("title") else (d.board.width / 2, d.board.height - 1.4)
        items.append(S("gr_text", f"{d.board.title} rev {d.board.rev}", S("at", tx, ty, 0),
                       S("layer", "B.SilkS"), S("uuid", _uid("title")),
                       S("effects", S("font", S("size", 0.8, 0.8), S("thickness", 0.12)), S("justify", Sym("mirror")))))
    # tracks / vias
    for ti, t in enumerate(L.tracks):
        n = netid.get(t.net, 0)
        for k in range(len(t.pts) - 1):
            a, b = t.pts[k], t.pts[k + 1]
            if abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) < 1e-6:
                continue
            seg = S("segment", S("start", round(a[0], 4), round(a[1], 4)), S("end", round(b[0], 4), round(b[1], 4)),
                    S("width", t.width), S("layer", t.layer), S("net", n), S("uuid", _uid("seg", ti, k)))
            if t.locked:
                seg.insert(1, S("locked", Sym("yes")))
            items.append(seg)
    cul = copper_layers(ncu)
    for vi, v in enumerate(L.vias):
        items.append(S("via", S("at", round(v.x, 4), round(v.y, 4)), S("size", v.d), S("drill", v.drill),
                       S("layers", cul[0], cul[-1]), S("net", netid.get(v.net, 0)), S("uuid", _uid("via", vi))))
    # zones
    for zi, z in enumerate(L.zones):
        pts = z.poly or [list(p) for p in inset_rect(d, 0.0)]
        cp = S("connect_pads", S("clearance", z.clearance))
        if z.connect == "solid":
            cp = S("connect_pads", Sym("yes"), S("clearance", z.clearance))
        elif z.connect == "thru_hole_only":
            cp = S("connect_pads", Sym("thru_hole_only"), S("clearance", z.clearance))
        items.append(S("zone", S("net", netid.get(z.net, 0)), S("net_name", z.net), S("layer", z.layer),
                       S("uuid", _uid("zone", zi)), S("hatch", Sym("edge"), 0.5), S("priority", z.priority), cp,
                       S("min_thickness", z.min_width), S("filled_areas_thickness", Sym("no")),
                       S("fill", Sym("yes"), S("thermal_gap", z.thermal_gap), S("thermal_bridge_width", z.thermal_spoke),
                         S("island_removal_mode", 1 if KEEP_ISLANDS else 0), S("island_area_min", 0.5)),
                       S("polygon", S("pts", *[S("xy", round(p[0], 4), round(p[1], 4)) for p in pts]))))
    # rule areas: keepout constraints marked "no_pour" also forbid copper pours (e.g. under an RJ45 body)
    for ki, c in enumerate(d.constraints):
        if c.kind == "keepout" and c.rect and "no_pour" in (c.note or ""):
            x1, y1, x2, y2 = c.rect
            lays = [l for l in (c.layers or cul) if l in cul] or cul
            items.append(S("zone", S("net", 0), S("net_name", ""), S("layers", *lays), S("uuid", _uid("rulearea", ki)),
                           S("name", "keepout"), S("hatch", Sym("edge"), 0.5), S("connect_pads", S("clearance", 0)),
                           S("min_thickness", 0.25), S("filled_areas_thickness", Sym("no")),
                           S("keepout", S("tracks", Sym("not_allowed")), S("vias", Sym("not_allowed")),
                             S("pads", Sym("allowed")), S("copperpour", Sym("not_allowed")), S("footprints", Sym("allowed"))),
                           S("fill", S("thermal_gap", 0.5), S("thermal_bridge_width", 0.5)),
                           S("polygon", S("pts", *[S("xy", px, py) for px, py in ((x1, y1), (x2, y1), (x2, y2), (x1, y2))]))))
    net_nodes = [S("net", 0, "")] + [S("net", netid[n], n) for n in nets]
    tree = S("kicad_pcb", S("version", PCB_VERSION), S("generator", "llmpcb"), S("generator_version", "10.0"),
             S("general", S("thickness", d.board.thickness), S("legacy_teardrops", Sym("no"))),
             S("paper", "A4"),
             S("title_block", S("title", d.board.title or d.name), S("rev", d.board.rev)),
             _layers_node(ncu),
             S("setup", _stackup(ncu, d.board.thickness), S("pad_to_mask_clearance", 0),
               S("allow_soldermask_bridges_in_footprints", Sym("no")),
               S("tenting", Sym("front"), Sym("back"))),
             *net_nodes, *items, S("embedded_fonts", Sym("no")))
    sx.dump(tree, prj.path(".kicad_pcb"))
    return {"pcb": str(prj.path(".kicad_pcb")), "nets": len(nets), "footprints": len(d.components) - len(missing),
            "unplaced": missing}


def inset_rect(d, inset: float) -> list[tuple[float, float]]:
    W, H = d.board.width, d.board.height
    return [(inset, inset), (W - inset, inset), (W - inset, H - inset), (inset, H - inset)]
