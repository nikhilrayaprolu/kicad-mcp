"""Access to KiCad stock symbol / footprint libraries + geometry extraction.

Everything here is read-only and cached.  Footprints are returned as parsed
s-expressions plus a lightweight geometric summary (pads, courtyard bbox) used by
placer, router (DSN writer) and checks.
"""
from __future__ import annotations

import copy
import functools
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import sexpr as sx
from .sexpr import Sym

ROOT = Path(os.environ.get("LLMPCB_KICAD_ROOT", Path(__file__).resolve().parents[1] / "tools/kicad-root"))
SYM_DIR = Path(os.environ.get("KICAD10_SYMBOL_DIR", ROOT / "usr/share/kicad/symbols"))
FP_DIR = Path(os.environ.get("KICAD10_FOOTPRINT_DIR", ROOT / "usr/share/kicad/footprints"))
LOCAL_DIR = Path(__file__).resolve().parent / "library"   # generated / curated symbols & footprints


# ======================================================================= symbols
@dataclass
class SymPin:
    number: str
    name: str
    etype: str          # input, output, bidirectional, power_in, power_out, passive, no_connect, ...
    x: float            # library coords (Y up)
    y: float
    angle: float        # 0 = pin extends to the right of its connection point
    length: float
    unit: int = 1
    hidden: bool = False


@dataclass
class SymbolDef:
    lib_id: str
    node: list                      # flattened symbol s-expr named lib_id
    pins: list[SymPin]
    units: int
    props: dict = field(default_factory=dict)

    def pins_by_number(self) -> dict[str, list[SymPin]]:
        d: dict[str, list[SymPin]] = {}
        for p in self.pins:
            d.setdefault(p.number, []).append(p)
        return d

    def bbox(self, unit: int = 1) -> tuple[float, float, float, float]:
        xs, ys = [], []
        for p in self.pins:
            if p.unit in (0, unit):
                xs.append(p.x)
                ys.append(p.y)
        for n in sx.walk(self.node):
            h = sx.head(n)
            if h in ("rectangle",):
                for k in ("start", "end"):
                    c = sx.find(n, k)
                    xs.append(c[1]); ys.append(c[2])
            elif h == "polyline":
                pts = sx.find(n, "pts")
                for xy in sx.find_all(pts, "xy"):
                    xs.append(xy[1]); ys.append(xy[2])
        if not xs:
            return (-2.54, -2.54, 2.54, 2.54)
        return (min(xs), min(ys), max(xs), max(ys))


@functools.lru_cache(maxsize=None)
def _sym_lib(path: str) -> dict[str, list]:
    tree = sx.load(path)
    return {str(c[1]): c for c in sx.find_all(tree, "symbol")}


def _lib_path(lib: str) -> Path:
    for d in (LOCAL_DIR / "symbols", SYM_DIR):
        p = d / f"{lib}.kicad_sym"
        if p.exists():
            return p
    raise KeyError(f"symbol library {lib!r} not found")


def _rename_units(node: list, old: str, new: str) -> None:
    for c in node:
        if isinstance(c, list) and c and c[0] == "symbol" and isinstance(c[1], str):
            if c[1].startswith(old + "_"):
                c[1] = new + c[1][len(old):]


GENERATORS: dict = {}   # lib_id -> callable that (re)generates it (registered by parts.py)


def get_symbol(lib_id: str) -> SymbolDef:
    try:
        return _get_symbol(lib_id)
    except KeyError:
        if lib_id in GENERATORS:
            GENERATORS[lib_id]()
            return _get_symbol(lib_id)
        raise


get_symbol.cache_clear = lambda: _get_symbol.cache_clear()  # type: ignore[attr-defined]


@functools.lru_cache(maxsize=None)
def _get_symbol(lib_id: str) -> SymbolDef:
    lib, name = lib_id.split(":", 1)
    syms = _sym_lib(str(_lib_path(lib)))
    if name not in syms:
        raise KeyError(f"symbol {lib_id} not found")
    node = copy.deepcopy(syms[name])
    ext = sx.find(node, "extends")
    if ext is not None:
        parent = copy.deepcopy(syms[str(ext[1])])
        pname = str(ext[1])
        # child properties override parent's
        child_props = {c[1]: c for c in sx.find_all(node, "property")}
        newnode = [Sym("symbol"), name]
        for c in parent[2:]:
            if isinstance(c, list) and c and c[0] == "property" and c[1] in child_props:
                newnode.append(child_props.pop(c[1]))
            else:
                newnode.append(c)
        newnode.extend(child_props.values())
        _rename_units(newnode, pname, name)
        node = newnode
    node = copy.deepcopy(node)
    node[1] = lib_id
    pins: list[SymPin] = []
    units = 1
    for sub in sx.find_all(node, "symbol"):
        m = re.search(r"_(\d+)_(\d+)$", str(sub[1]))
        unit = int(m.group(1)) if m else 1
        units = max(units, unit)
        for p in sx.find_all(sub, "pin"):
            at = sx.find(p, "at")
            nm = sx.find(p, "name")
            nu = sx.find(p, "number")
            ln = sx.find(p, "length")
            hidden = any(isinstance(c, Sym) and c == "hide" for c in p) or \
                (sx.find(p, "hide") is not None and sx.find(p, "hide")[1] == "yes")
            pins.append(SymPin(str(nu[1]), str(nm[1]), str(p[1]), float(at[1]), float(at[2]),
                               float(at[3]) if len(at) > 3 else 0.0, float(ln[1]) if ln else 2.54, unit, hidden))
    props = {str(c[1]): c[2] for c in sx.find_all(node, "property")}
    return SymbolDef(lib_id, node, pins, units, props)


@functools.lru_cache(maxsize=1)
def symbol_index() -> list[tuple[str, str, str]]:
    """(lib_id, description, keywords) for every stock symbol (cached on disk)."""
    import json
    cache = Path.home() / ".cache" / "llmpcb" / "symidx.json"
    stamp = sum(f.stat().st_mtime for d in (LOCAL_DIR / "symbols", SYM_DIR) if d.exists() for f in d.glob("*.kicad_sym"))
    if cache.exists():
        try:
            data = json.loads(cache.read_text())
            if abs(data["stamp"] - stamp) < 1e-3:
                return [tuple(x) for x in data["idx"]]
        except Exception:  # noqa: BLE001
            pass
    out = []
    for d in (LOCAL_DIR / "symbols", SYM_DIR):
        if not d.exists():
            continue
        for f in sorted(d.glob("*.kicad_sym")):
            text = f.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r'\n\t\(symbol "([^"]+)"(.*?)(?=\n\t\(symbol "|\Z)', text, re.S):
                body = m.group(2)
                desc = re.search(r'\(property "Description" "((?:[^"\\]|\\.)*)"', body)
                kw = re.search(r'\(property "ki_keywords" "((?:[^"\\]|\\.)*)"', body)
                out.append((f"{f.stem}:{m.group(1)}", desc.group(1) if desc else "", kw.group(1) if kw else ""))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"stamp": stamp, "idx": out}))
    return out


# ===================================================================== footprints
@dataclass
class Pad:
    number: str
    kind: str            # smd | thru_hole | np_thru_hole | connect
    shape: str           # rect | roundrect | circle | oval | custom | trapezoid
    x: float             # footprint-local coords (mm, Y down)
    y: float
    w: float
    h: float
    angle: float         # local pad rotation (deg)
    layers: list[str]
    drill: float = 0.0
    rratio: float = 0.0


@dataclass
class FootprintDef:
    lib_id: str
    node: list
    pads: list[Pad]
    courtyard: tuple[float, float, float, float]   # local bbox (x1,y1,x2,y2)
    attrs: list[str]

    @property
    def size(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.courtyard
        return (x2 - x1, y2 - y1)


def _fp_path(lib_id: str) -> Path:
    lib, name = lib_id.split(":", 1)
    for d in (LOCAL_DIR / "footprints", FP_DIR):
        p = d / f"{lib}.pretty" / f"{name}.kicad_mod"
        if p.exists():
            return p
    raise KeyError(f"footprint {lib_id} not found")


def parse_pad(p: list) -> Pad:
    at = sx.find(p, "at")
    size = sx.find(p, "size")
    layers = sx.find(p, "layers")
    drill = sx.find(p, "drill")
    rr = sx.find(p, "roundrect_rratio")
    dval = 0.0
    if drill is not None:
        nums = [v for v in drill[1:] if isinstance(v, (int, float))]
        dval = float(nums[0]) if nums else 0.0
        if any(v == "oval" for v in drill[1:]) and len(nums) > 1:
            dval = float(min(nums[0], nums[1]))
    return Pad(str(p[1]), str(p[2]), str(p[3]), float(at[1]), float(at[2]), float(size[1]), float(size[2]),
               float(at[3]) if len(at) > 3 else 0.0, [str(l) for l in layers[1:]] if layers else [], dval,
               float(rr[1]) if rr else 0.0)


def _geom_points(n: list) -> list[tuple[float, float]]:
    h = sx.head(n)
    pts = []
    if h in ("fp_line", "fp_rect"):
        for k in ("start", "end"):
            c = sx.find(n, k)
            if c:
                pts.append((float(c[1]), float(c[2])))
    elif h == "fp_circle":
        c, e = sx.find(n, "center"), sx.find(n, "end")
        r = math.hypot(e[1] - c[1], e[2] - c[2])
        pts += [(c[1] - r, c[2] - r), (c[1] + r, c[2] + r)]
    elif h == "fp_arc":
        for k in ("start", "mid", "end"):
            c = sx.find(n, k)
            if c:
                pts.append((float(c[1]), float(c[2])))
    elif h == "fp_poly":
        for xy in sx.find_all(sx.find(n, "pts") or [], "xy"):
            pts.append((float(xy[1]), float(xy[2])))
    return pts


def get_footprint(lib_id: str) -> FootprintDef:
    try:
        return _get_footprint(lib_id)
    except KeyError:
        if lib_id in GENERATORS:
            GENERATORS[lib_id]()
            return _get_footprint(lib_id)
        raise


get_footprint.cache_clear = lambda: _get_footprint.cache_clear()  # type: ignore[attr-defined]


@functools.lru_cache(maxsize=None)
def _get_footprint(lib_id: str) -> FootprintDef:
    node = sx.load(_fp_path(lib_id))
    node[1] = lib_id
    pads = [parse_pad(p) for p in sx.find_all(node, "pad")]
    cy = []
    for c in node:
        if isinstance(c, list) and sx.head(c) in ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly"):
            lay = sx.find(c, "layer")
            if lay and str(lay[1]) in ("F.CrtYd", "B.CrtYd"):
                cy += _geom_points(c)
    if not cy:
        for p in pads:
            r = max(p.w, p.h) / 2 + 0.25
            cy += [(p.x - r, p.y - r), (p.x + r, p.y + r)]
    if not cy:
        cy = [(-0.5, -0.5), (0.5, 0.5)]
    xs, ys = [p[0] for p in cy], [p[1] for p in cy]
    attr = sx.find(node, "attr")
    return FootprintDef(lib_id, node, pads, (min(xs), min(ys), max(xs), max(ys)),
                        [str(a) for a in attr[1:]] if attr else [])


@functools.lru_cache(maxsize=1)
def footprint_index() -> list[str]:
    out = []
    for d in (LOCAL_DIR / "footprints", FP_DIR):
        if d.exists():
            for lib in sorted(d.glob("*.pretty")):
                out += [f"{lib.stem}:{f.stem}" for f in sorted(lib.glob("*.kicad_mod"))]
    return out


# ======================================================================= geometry
def rot(x: float, y: float, deg: float) -> tuple[float, float]:
    """KiCad rotation in board coords (Y down, positive angle = CCW on screen)."""
    if deg % 360 == 0:
        return x, y
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return (x * c + y * s, -x * s + y * c)


def pad_abs(pad: Pad, fx: float, fy: float, frot: float) -> tuple[float, float]:
    dx, dy = rot(pad.x, pad.y, frot)
    return fx + dx, fy + dy


def pad_extent(pad: Pad, frot: float) -> tuple[float, float]:
    """Axis-aligned half-extent (hx, hy) of a pad after total rotation."""
    a = (pad.angle + frot) % 180
    if abs(a) < 1e-6:
        return pad.w / 2, pad.h / 2
    if abs(a - 90) < 1e-6:
        return pad.h / 2, pad.w / 2
    r = math.radians(a)
    c, s = abs(math.cos(r)), abs(math.sin(r))
    return (pad.w * c + pad.h * s) / 2, (pad.w * s + pad.h * c) / 2


def bbox_rot(bb, deg: float):
    x1, y1, x2, y2 = bb
    pts = [rot(x, y, deg) for x, y in ((x1, y1), (x1, y2), (x2, y1), (x2, y2))]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))



# ======================================================================= bottom side
def _swap_layer(name: str) -> str:
    if name.startswith("F."):
        return "B." + name[2:]
    if name.startswith("B."):
        return "F." + name[2:]
    return name


def mirror_node(node: list) -> list:
    """Mirror a footprint s-expr for the bottom side (KiCad stores flipped footprints already mirrored:
    local x negated, F./B. layers swapped, text mirrored)."""
    n = copy.deepcopy(node)

    def rec(x):
        if not isinstance(x, list) or not x:
            return
        h = x[0]
        if h in ("at", "start", "end", "center", "mid", "xy", "offset") and len(x) >= 3 and isinstance(x[1], (int, float)):
            x[1] = -float(x[1])
            if h == "at" and len(x) >= 4 and isinstance(x[3], (int, float)):
                x[3] = (-float(x[3])) % 360
        elif h in ("layer", "layers"):
            for i in range(1, len(x)):
                if isinstance(x[i], str):
                    x[i] = type(x[i])(_swap_layer(str(x[i])))
        elif h == "effects":
            j = sx.find(x, "justify")
            if j is None:
                x.append([Sym("justify"), Sym("mirror")])
            elif "mirror" not in [str(v) for v in j[1:]]:
                j.append(Sym("mirror"))
            return
        for c in x[1:]:
            rec(c)
    for c in n[2:]:
        rec(c)
    return n


@functools.lru_cache(maxsize=None)
def get_footprint_side(lib_id: str, side: str = "top") -> FootprintDef:
    fp = get_footprint(lib_id)
    if side != "bottom":
        return fp
    node = mirror_node(fp.node)
    pads = [parse_pad(p) for p in sx.find_all(node, "pad")]
    x1, y1, x2, y2 = fp.courtyard
    return FootprintDef(lib_id, node, pads, (-x2, y1, -x1, y2), fp.attrs)
