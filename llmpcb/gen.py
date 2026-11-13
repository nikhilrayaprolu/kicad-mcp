"""Parametric generators for symbols & footprints missing from the KiCad libraries.

Output goes to llmpcb/library/{symbols/llmpcb.kicad_sym, footprints/llmpcb.pretty}
so that libs.get_symbol("llmpcb:X") / get_footprint("llmpcb:Y") find them.
"""
from __future__ import annotations

import math
import uuid
from pathlib import Path

from . import libs
from . import sexpr as sx
from .sexpr import S, Sym

LIB = libs.LOCAL_DIR
SYM_FILE = LIB / "symbols" / "llmpcb.kicad_sym"
FP_DIR = LIB / "footprints" / "llmpcb.pretty"
G = 2.54


def _font(size=1.27):
    return S("effects", S("font", S("size", size, size)))


def _prop(k, v, x=0.0, y=0.0, hide=False):
    eff = _font()
    if hide:
        eff.append(S("hide", Sym("yes")))
    return S("property", k, v, S("at", x, y, 0), eff)


# ------------------------------------------------------------------ symbols
def make_box_symbol(name: str, pins: list[dict], ref_prefix: str = "U", footprint: str = "",
                    description: str = "", datasheet: str = "") -> list:
    """pins: [{number, name, type, side?}]  side in left/right/top/bottom (auto if absent).

    Auto side: power_in (non-GND) -> top, GND -> bottom, inputs -> left, others -> right.
    Pins sharing a name on top/bottom are stacked (hidden duplicates are NOT used: every
    physical pin is visible so ERC and humans see it).
    """
    sides: dict[str, list[dict]] = {"left": [], "right": [], "top": [], "bottom": []}
    for p in pins:
        s = p.get("side")
        if not s:
            t, n = p.get("type", "bidirectional"), p["name"].upper()
            if t == "power_in" and ("GND" in n or n.startswith("VSS")):
                s = "bottom"
            elif t in ("power_in", "power_out"):
                s = "top"
            elif t in ("input",):
                s = "left"
            else:
                s = "right"
        sides[s].append(p)
    # keep long sides balanced: move overflow from right->left etc.
    nl, nr = len(sides["left"]), len(sides["right"])
    while nr - nl > 1:
        sides["left"].append(sides["right"].pop()); nl += 1; nr -= 1
    while nl - nr > 1:
        sides["right"].append(sides["left"].pop()); nl -= 1; nr += 1
    for s in ("top", "bottom"):  # very wide power rows: wrap extra onto sides
        while len(sides[s]) > max(12, len(sides["left"])):
            (sides["left"] if len(sides["left"]) <= len(sides["right"]) else sides["right"]).append(sides[s].pop())
    CW = 1.0  # approx glyph width (mm) at 1.27 mm font
    tw = {k: (max([len(p["name"]) for p in v] + [0]) * CW + 1.0) if v else 0.0 for k, v in sides.items()}
    nv = max(len(sides["left"]), len(sides["right"]), 1)
    nh = max(len(sides["top"]), len(sides["bottom"]), 1)
    gu = lambda v: math.ceil(v / G) * G
    # horizontal room: top/bottom pins must clear left/right pin-name text, and names facing each other must fit
    half_w = gu(max(((nh - 1) * G + tw["left"] + tw["right"] + 2 * G) / 2, (tw["left"] + tw["right"]) / 2 + G, 2 * G))
    half_h = gu(max(((nv - 1) * G + tw["top"] + tw["bottom"] + 2 * G) / 2, (tw["top"] + tw["bottom"]) / 2 + G, 2 * G))
    L = G  # pin length
    pin_nodes = []

    def add(p, x, y, ang):
        pin_nodes.append(S("pin", Sym(p.get("type", "bidirectional")), Sym("line"), S("at", x, y, ang),
                           S("length", L), S("name", p["name"], _font()), S("number", str(p["number"]), _font())))

    y0 = gu(half_h - tw["top"] - G) if sides["top"] else half_h - G
    for i, p in enumerate(sides["left"]):
        add(p, -half_w - L, y0 - G * i, 0)
    for i, p in enumerate(sides["right"]):
        add(p, half_w + L, y0 - G * i, 180)
    x0 = -gu(half_w - tw["left"] - G) if sides["left"] else -half_w + G
    for i, p in enumerate(sides["top"]):
        add(p, x0 + G * i, half_h + L, 270)
    for i, p in enumerate(sides["bottom"]):
        add(p, x0 + G * i, -half_h - L, 90)
    body = S("symbol", f"{name}_0_1",
             S("rectangle", S("start", -half_w, half_h), S("end", half_w, -half_h),
               S("stroke", S("width", 0.254), S("type", Sym("default"))), S("fill", S("type", Sym("background")))))
    pins_unit = S("symbol", f"{name}_1_1", *pin_nodes)
    return S("symbol", name, S("exclude_from_sim", Sym("no")), S("in_bom", Sym("yes")), S("on_board", Sym("yes")),
             _prop("Reference", ref_prefix, 0, half_h + L + 1.5),
             _prop("Value", name, 0, -half_h - L - 1.5),
             _prop("Footprint", footprint, 0, 0, True),
             _prop("Datasheet", datasheet, 0, 0, True),
             _prop("Description", description, 0, 0, True),
             body, pins_unit)


def save_symbols(symbols: list[list]) -> None:
    """Merge symbols (by name) into the generated library."""
    SYM_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if SYM_FILE.exists():
        tree = sx.load(SYM_FILE)
        existing = {str(c[1]): c for c in sx.find_all(tree, "symbol")}
    for s in symbols:
        existing[str(s[1])] = s
    tree = S("kicad_symbol_lib", S("version", 20241209), S("generator", "llmpcb"),
             S("generator_version", "10.0"), *existing.values())
    sx.dump(tree, SYM_FILE)
    libs._sym_lib.cache_clear()
    libs.get_symbol.cache_clear()
    libs.symbol_index.cache_clear()


# ------------------------------------------------------------------ footprints
def _line(x1, y1, x2, y2, layer, w=0.12):
    return S("fp_line", S("start", x1, y1), S("end", x2, y2), S("stroke", S("width", w), S("type", Sym("solid"))),
             S("layer", layer))


def _rect(x1, y1, x2, y2, layer, w=0.05):
    return S("fp_rect", S("start", x1, y1), S("end", x2, y2), S("stroke", S("width", w), S("type", Sym("solid"))),
             S("fill", Sym("no")), S("layer", layer))


def _smd(num, x, y, w, h, rr=0.25, layers=("F.Cu", "F.Mask", "F.Paste"), extra=()):
    return S("pad", str(num), Sym("smd"), Sym("roundrect"), S("at", x, y), S("size", w, h),
             S("layers", *layers), S("roundrect_rratio", rr), *extra)


def _fp(name, descr, items, cy, attr="smd"):
    x1, y1, x2, y2 = cy
    return S("footprint", name, S("version", 20241229), S("generator", "llmpcb"), S("layer", "F.Cu"),
             S("descr", descr), S("tags", "llmpcb generated"),
             S("property", "Reference", "REF**", S("at", 0, y1 - 1.0, 0), S("layer", "F.SilkS"),
               S("effects", S("font", S("size", 1, 1), S("thickness", 0.15)))),
             S("property", "Value", name, S("at", 0, y2 + 1.0, 0), S("layer", "F.Fab"),
               S("effects", S("font", S("size", 1, 1), S("thickness", 0.15)))),
             S("attr", Sym(attr)), *items, _rect(x1, y1, x2, y2, "F.CrtYd"))


def qfn(name: str, pins: int, pitch: float, body_x: float, body_y: float, pad_l: float, pad_w: float,
        ep: tuple[float, float] | None = None, ep_vias: int = 0, toe: float = 0.05,
        rows: tuple[int, int] | None = None, ep_number: str | None = None, descr: str = "") -> list:
    """Standard QFN, pin 1 at top-left of the left row, counter-clockwise numbering."""
    nx, ny = rows if rows else (pins // 4, pins // 4)   # pins per bottom/top row, per left/right row
    items = []
    num = 1
    cx = body_x / 2 - pad_l / 2 + toe
    cy_ = body_y / 2 - pad_l / 2 + toe
    # left (top->bottom)
    for i in range(ny):
        items.append(_smd(num, -cx, (i - (ny - 1) / 2) * pitch, pad_l, pad_w)); num += 1
    for i in range(nx):  # bottom (left->right)
        items.append(_smd(num, (i - (nx - 1) / 2) * pitch, cy_, pad_w, pad_l)); num += 1
    for i in range(ny):  # right (bottom->top)
        items.append(_smd(num, cx, ((ny - 1) / 2 - i) * pitch, pad_l, pad_w)); num += 1
    for i in range(nx):  # top (right->left)
        items.append(_smd(num, ((nx - 1) / 2 - i) * pitch, -cy_, pad_w, pad_l)); num += 1
    if ep:
        epn = ep_number or str(pins + 1)
        items.append(S("pad", epn, Sym("smd"), Sym("rect"), S("at", 0, 0), S("size", ep[0], ep[1]),
                       S("layers", "F.Cu", "F.Mask"), S("zone_connect", 2)))
        # paste windows (~60% coverage) in a 3x3 grid
        k = 3
        sw, sh = ep[0] / k * 0.75, ep[1] / k * 0.75
        for i in range(k):
            for j in range(k):
                items.append(S("pad", "", Sym("smd"), Sym("roundrect"),
                               S("at", (i - 1) * ep[0] / k, (j - 1) * ep[1] / k), S("size", sw, sh),
                               S("layers", "F.Paste"), S("roundrect_rratio", 0.25)))
        if ep_vias:
            g = ep_vias
            for i in range(g):
                for j in range(g):
                    vx = (i - (g - 1) / 2) * ep[0] / g
                    vy = (j - (g - 1) / 2) * ep[1] / g
                    items.append(S("pad", epn, Sym("thru_hole"), Sym("circle"), S("at", vx, vy), S("size", 0.5, 0.5),
                                   S("drill", 0.25), S("layers", "*.Cu"), S("zone_connect", 2)))
    hx, hy = body_x / 2, body_y / 2
    # corner marks just outside the pad ring, never reaching a pad (silk-to-mask clearance 0.2 mm)
    ox = max(hx, cx + pad_l / 2) + 0.2
    oy = max(hy, cy_ + pad_l / 2) + 0.2
    end_x = (nx - 1) / 2 * pitch + pad_w / 2 + 0.25      # outermost pad edge along a horizontal row
    end_y = (ny - 1) / 2 * pitch + pad_w / 2 + 0.25
    tick = 0.6
    for sxn in (-1, 1):
        for syn in (-1, 1):
            items.append(_line(sxn * ox, syn * oy, sxn * max(ox - tick, end_x), syn * oy, "F.SilkS"))
            items.append(_line(sxn * ox, syn * oy, sxn * ox, syn * max(oy - tick, end_y), "F.SilkS"))
    items.append(S("fp_circle", S("center", -ox - 0.35, -oy - 0.35), S("end", -ox - 0.2, -oy - 0.35),
                   S("stroke", S("width", 0.12), S("type", Sym("solid"))), S("fill", Sym("yes")), S("layer", "F.SilkS")))
    items.append(_rect(-hx, -hy, hx, hy, "F.Fab", 0.1))
    ext = max(cx + pad_l / 2, cy_ + pad_l / 2, hx, hy) + 0.25
    return _fp(name, descr or f"QFN-{pins} {body_x}x{body_y} P{pitch}", items, (-ext, -ext, ext, ext))


def save_footprint(fp: list) -> str:
    FP_DIR.mkdir(parents=True, exist_ok=True)
    name = str(fp[1])
    sx.dump(fp, FP_DIR / f"{name}.kicad_mod")
    libs.get_footprint.cache_clear()
    libs.footprint_index.cache_clear()
    return f"llmpcb:{name}"


def new_uuid() -> str:
    return str(uuid.uuid4())
