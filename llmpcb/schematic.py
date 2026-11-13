"""IR -> hierarchical KiCad schematic (one sheet per functional block).

Connectivity is expressed with labels attached directly at pin endpoints (local
labels for block-internal nets, global labels for nets crossing blocks), so no
wire geometry needs to be solved.  Unconnected pins get no-connect flags, and
PWR_FLAGs are added to power nets without a power_out driver so ERC is meaningful.
After generation the netlist is exported by kicad-cli and compared with the IR.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import uuid
from collections import defaultdict
from pathlib import Path

from . import libs
from . import sexpr as sx
from .ir import Design, Project
from .sexpr import S, Sym

G = 2.54
SCH_VERSION = 20250114


def _u() -> str:
    return str(uuid.uuid4())


def _uid(*parts) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "llmpcb/" + "/".join(map(str, parts))))


def _eff(justify=None, hide=False, size=1.27):
    e = S("effects", S("font", S("size", size, size)))
    if justify:
        e.append(S("justify", *[Sym(j) for j in justify.split()]))
    if hide:
        e.append(S("hide", Sym("yes")))
    return e


def _snap(v: float, g: float = 1.27) -> float:
    return round(round(v / g) * g, 4)


def label_len(net: str) -> float:
    return len(net) * 1.0 + 4


class SheetBuilder:
    def __init__(self, name: str, uuid_: str):
        self.name = name
        self.uuid = uuid_
        self.items: list = []
        self.lib_ids: set[str] = set()


def _pin_label(net: str, x: float, y: float, pin_angle: float, is_global: bool, sheet_key: str):
    # pin_angle (lib) = direction from connection point into the body; label points away.
    ang = {0: 180, 180: 0, 90: 270, 270: 90}[int(pin_angle) % 360]
    just = "left bottom" if ang in (0, 90) else "right bottom"
    if is_global:
        just = "left" if ang in (0, 90) else "right"
        return S("global_label", net, S("shape", Sym("bidirectional")), S("at", x, y, ang),
                 S("fields_autoplaced", Sym("yes")), _eff(just), S("uuid", _uid(sheet_key, "gl", net, x, y)),
                 S("property", "Intersheetrefs", "${INTERSHEET_REFS}", S("at", x, y, 0), _eff(hide=True)))
    return S("label", net, S("at", x, y, ang), S("fields_autoplaced", Sym("yes")), _eff(just),
             S("uuid", _uid(sheet_key, "l", net, x, y)))


def build(prj: Project) -> dict:
    d = prj.design
    root_uuid = _uid(d.name, "root")
    nets = d.nets()
    net_blocks: dict[str, set[str]] = defaultdict(set)
    for c in d.components.values():
        for pin, net in c.pins.items():
            net_blocks[net].add(c.block)
    # power-ish nets: those with power pins -> treat as global
    blocks: dict[str, list] = defaultdict(list)
    for c in d.components.values():
        blocks[c.block].append(c)

    # which nets have a power_out / power_in pin
    has_pout: set[str] = set()
    has_pin: set[str] = set()
    pin_types: dict[tuple[str, str], str] = {}
    for c in d.components.values():
        sym = libs.get_symbol(c.symbol)
        for p in sym.pins:
            pin_types[(c.ref, p.number)] = p.etype
            net = c.pins.get(p.number)
            if not net:
                continue
            if p.etype == "power_out":
                has_pout.add(net)
            if p.etype == "power_in":
                has_pin.add(net)
    flag_nets = sorted(has_pin - has_pout)

    sheets: dict[str, SheetBuilder] = {}
    issues: list[str] = []
    for bname in sorted(blocks):
        sb = SheetBuilder(bname, _uid(d.name, "sheet", bname))
        sheets[bname] = sb
        comps = sorted(blocks[bname], key=lambda c: (-len(libs.get_symbol(c.symbol).pins), c.ref))
        # simple shelf packing on an A3-ish canvas (grows as needed)
        x0, y0 = 25.4, 30.48
        cur_x, cur_y, shelf_h = x0, y0, 0.0
        max_w = 380.0
        for c in comps:
            sym = libs.get_symbol(c.symbol)
            sb.lib_ids.add(c.symbol)
            for unit in range(1, sym.units + 1):
                upins = [p for p in sym.pins if p.unit in (0, unit)]
                if not upins and unit > 1:
                    continue
                bx1, by1, bx2, by2 = sym.bbox(unit)
                ll = max([label_len(c.pins.get(p.number, "")) for p in upins] + [4])
                left_ext = ll if any(p.angle == 0 for p in upins) else 2
                right_ext = ll if any(p.angle == 180 for p in upins) else 2
                top_ext = ll if any(p.angle == 270 for p in upins) else 2
                bot_ext = ll if any(p.angle == 90 for p in upins) else 2
                w = (bx2 - bx1) + left_ext + right_ext + 10
                h = (by2 - by1) + top_ext + bot_ext + 8
                if cur_x + w > x0 + max_w and cur_x > x0:
                    cur_x = x0
                    cur_y += shelf_h
                    shelf_h = 0
                X = _snap(cur_x + left_ext - bx1 + 5, G)
                Y = _snap(cur_y + top_ext + by2 + 4, G)
                cur_x += w
                shelf_h = max(shelf_h, h)
                sb.items.append(_symbol_instance(d, c, sym, unit, X, Y, by1, by2, root_uuid, sb, bname, X + bx2 + right_ext))
                for p in upins:
                    px, py = X + p.x, Y - p.y
                    net = c.pins.get(p.number)
                    if net:
                        is_global = True  # plain net names everywhere (PCB parity, readability)
                        sb.items.append(_pin_label(net, round(px, 4), round(py, 4), p.angle, is_global, bname))
                    elif p.etype != "no_connect":
                        if p.number not in c.nc and p.etype in ("power_in",):
                            issues.append(f"{c.ref}.{p.number}({p.name}) power pin left unconnected")
                        sb.items.append(S("no_connect", S("at", round(px, 4), round(py, 4)),
                                          S("uuid", _uid(bname, c.ref, p.number, "nc"))))
        sb.height = cur_y + shelf_h  # type: ignore[attr-defined]

    # PWR_FLAG sheet content goes to a dedicated "power_flags" area on the root sheet
    root_items = []
    lib_all: set[str] = set()
    for sb in sheets.values():
        lib_all |= sb.lib_ids
    if flag_nets:
        lib_all.add("power:PWR_FLAG")
        fsym = libs.get_symbol("power:PWR_FLAG")
        for i, net in enumerate(flag_nets):
            X = _snap(30 + (i % 8) * 45, G)
            Y = _snap(200 + (i // 8) * 20, G)
            ref = f"#FLG{i+1:02d}"
            root_items.append(S("symbol", S("lib_id", "power:PWR_FLAG"), S("at", X, Y, 0), S("unit", 1),
                                S("exclude_from_sim", Sym("no")), S("in_bom", Sym("yes")), S("on_board", Sym("yes")),
                                S("dnp", Sym("no")), S("uuid", _uid(d.name, "flag", net)),
                                S("property", "Reference", ref, S("at", X, Y - 5, 0), _eff(hide=True)),
                                S("property", "Value", "PWR_FLAG", S("at", X, Y - 3.8, 0), _eff()),
                                S("property", "Footprint", "", S("at", X, Y, 0), _eff(hide=True)),
                                S("property", "Datasheet", "", S("at", X, Y, 0), _eff(hide=True)),
                                S("pin", "1", S("uuid", _uid(d.name, "flagpin", net))),
                                S("instances", S("project", d.name, S("path", f"/{root_uuid}",
                                                                      S("reference", ref), S("unit", 1))))))
            p = fsym.pins[0]
            root_items.append(_pin_label(net, X + p.x, Y - p.y, p.angle if p.angle else 90, True, "root"))
            root_items.append(S("text", f"rail {net}", S("at", X + 3, Y + 4, 0), _eff("left"),
                                S("uuid", _uid("root", "flagtxt", net))))

    files = []
    # sub-sheets
    for i, (bname, sb) in enumerate(sorted(sheets.items())):
        fname = f"{_safe(bname)}.kicad_sch"
        lib_syms = [_lib_symbol(lid) for lid in sorted(sb.lib_ids)]
        title = S("title_block", S("title", f"{d.board.title or d.name} - {bname}"), S("rev", d.board.rev))
        tree = S("kicad_sch", S("version", SCH_VERSION), S("generator", "llmpcb"), S("generator_version", "10.0"),
                 S("uuid", sb.uuid), S("paper", "A3" if sb.height < 280 else "A2" if sb.height < 400 else "A1"),
                 title, S("lib_symbols", *lib_syms), *sb.items, S("embedded_fonts", Sym("no")))
        sx.dump(tree, prj.root / fname)
        files.append(fname)
    # root sheet with sheet symbols
    sheet_nodes = []
    for i, (bname, sb) in enumerate(sorted(sheets.items())):
        x, y = 25.4 + (i % 6) * 60.96, 30.48 + (i // 6) * 45.72
        sheet_nodes.append(S("sheet", S("at", x, y), S("size", 50.8, 30.48), S("exclude_from_sim", Sym("no")),
                             S("in_bom", Sym("yes")), S("on_board", Sym("yes")), S("dnp", Sym("no")),
                             S("fields_autoplaced", Sym("yes")),
                             S("stroke", S("width", 0.1524), S("type", Sym("solid"))),
                             S("fill", S("color", 0, 0, 0, 0.0)), S("uuid", sb.uuid),
                             S("property", "Sheetname", bname, S("at", x, y - 0.7, 0), _eff("left bottom")),
                             S("property", "Sheetfile", f"{_safe(bname)}.kicad_sch", S("at", x, y + 31.1, 0),
                               _eff("left top")),
                             S("instances", S("project", d.name, S("path", f"/{root_uuid}", S("page", str(i + 2)))))))
    notes = [S("text", "\n".join([d.description] + [f"- {r}" for r in d.requirements])[:2000],
               S("at", 25.4, 170, 0), _eff("left"), S("uuid", _uid("root", "notes")))] if d.description else []
    root = S("kicad_sch", S("version", SCH_VERSION), S("generator", "llmpcb"), S("generator_version", "10.0"),
             S("uuid", root_uuid), S("paper", "A3"),
             S("title_block", S("title", d.board.title or d.name), S("rev", d.board.rev)),
             S("lib_symbols", *[_lib_symbol(l) for l in sorted({"power:PWR_FLAG"} & lib_all)]),
             *sheet_nodes, *root_items, *notes,
             S("sheet_instances", S("path", "/", S("page", "1"))), S("embedded_fonts", Sym("no")))
    sx.dump(root, prj.path(".kicad_sch"))
    write_project_file(prj)
    return {"root": str(prj.path(".kicad_sch")), "sheets": files, "pwr_flags": flag_nets, "issues": issues}


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s)


def _lib_symbol(lib_id: str) -> list:
    return libs.get_symbol(lib_id).node


def _symbol_instance(d: Design, c, sym, unit, X, Y, by1, by2, root_uuid, sb, bname, tx):
    props = [
        S("property", "Reference", c.ref, S("at", _snap(tx), _snap(Y - by2), 0), _eff("left")),
        S("property", "Value", c.value or sym.props.get("Value", ""), S("at", _snap(tx), _snap(Y - by2) + 2.54, 0),
          _eff("left")),
        S("property", "Footprint", c.footprint, S("at", X, Y, 0), _eff(hide=True)),
        S("property", "Datasheet", str(sym.props.get("Datasheet", "")), S("at", X, Y, 0), _eff(hide=True)),
    ]
    extra = dict(c.fields)
    if c.mpn:
        extra["MPN"] = c.mpn
    if c.lcsc:
        extra["LCSC"] = c.lcsc
    for k, v in extra.items():
        props.append(S("property", k, v, S("at", X, Y, 0), _eff(hide=True)))
    pins = [S("pin", p.number, S("uuid", _uid(bname, c.ref, unit, "pin", p.number)))
            for p in sym.pins if p.unit in (0, unit)]
    return S("symbol", S("lib_id", c.symbol), S("at", X, Y, 0), S("unit", unit),
             S("exclude_from_sim", Sym("no")), S("in_bom", Sym("no") if c.dnp else Sym("yes")),
             S("on_board", Sym("yes")), S("dnp", Sym("yes") if c.dnp else Sym("no")),
             S("uuid", sym_uuid(d, c.ref, unit)),
             *props, *pins,
             S("instances", S("project", d.name, S("path", f"/{root_uuid}/{sb.uuid}",
                                                   S("reference", c.ref), S("unit", unit)))))


def sym_uuid(d: Design, ref: str, unit: int = 1) -> str:
    return _uid(d.name, "sym", ref, unit)


def sheet_path(d: Design, block: str) -> str:
    return f"/{_uid(d.name, 'sheet', block)}"


# ------------------------------------------------------------------ project file
FAB_RULES = {
    "jlcpcb": dict(min_clearance=0.1, min_track_width=0.09, min_via_diameter=0.4, min_through_hole_diameter=0.2,
                   min_via_annular_width=0.1, min_hole_clearance=0.2, min_hole_to_hole=0.25,
                   min_copper_edge_clearance=0.3, min_silk_clearance=0.0, min_text_height=0.6,
                   min_text_thickness=0.08, min_connection=0.0, solder_mask_to_copper_clearance=0.0,
                   min_microvia_diameter=0.2, min_microvia_drill=0.1, min_resolved_spokes=1,
                   use_height_for_length_calcs=True),
}


def write_project_file(prj: Project) -> Path:
    d = prj.design
    classes = []
    patterns = []
    for nc in d.netclasses.values():
        classes.append({
            "name": nc.name, "clearance": nc.clearance, "track_width": nc.track,
            "via_diameter": nc.via_d, "via_drill": nc.via_drill,
            "diff_pair_width": nc.diff_width or nc.track, "diff_pair_gap": nc.diff_gap or nc.clearance,
            "diff_pair_via_gap": 0.25, "microvia_diameter": 0.3, "microvia_drill": 0.1,
            "bus_width": 12, "wire_width": 6, "line_style": 0,
            "pcb_color": "rgba(0, 0, 0, 0.000)", "schematic_color": "rgba(0, 0, 0, 0.000)",
            "priority": 2147483647 if nc.name == "Default" else len(classes),
        })
        if nc.name != "Default":
            patterns += [{"netclass": nc.name, "pattern": p} for p in nc.nets]
    severities = {"lib_footprint_issues": "ignore", "lib_footprint_mismatch": "ignore",
                  "silk_over_copper": "warning", "silk_overlap": "warning", "text_height": "warning",
                  "text_thickness": "warning", "silk_edge_clearance": "warning",
                  "footprint_symbol_mismatch": "warning", "nonmirrored_text_on_back_layer": "ignore",
                  "missing_courtyard": "ignore", "solder_mask_bridge": "warning", "copper_sliver": "warning",
                  "isolated_copper": "warning", "starved_thermal": "warning"}
    erc_sev = {"lib_symbol_issues": "ignore", "lib_symbol_mismatch": "ignore", "footprint_link_issues": "ignore",
               "simulation_model_issue": "ignore", "single_global_label": "ignore",
               "unconnected_wire_endpoint": "warning"}
    pro = {
        "board": {"design_settings": {
            "defaults": {"copper_line_width": 0.2, "silk_line_width": 0.12, "silk_text_size_h": 0.8,
                         "silk_text_size_v": 0.8, "silk_text_thickness": 0.12},
            "diff_pair_dimensions": [], "drc_exclusions": [],
            "rules": FAB_RULES.get(d.board.fab, FAB_RULES["jlcpcb"]),
            "rule_severities": severities,
            "track_widths": sorted({0.0} | {nc.track for nc in d.netclasses.values()}),
            "via_dimensions": [{"diameter": 0.0, "drill": 0.0}] +
                              [{"diameter": nc.via_d, "drill": nc.via_drill} for nc in d.netclasses.values()],
        }},
        "erc": {"rule_severities": erc_sev, "erc_exclusions": [], "meta": {"version": 0},
                "pin_map": None},
        "boards": [], "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
        "meta": {"filename": f"{d.name}.kicad_pro", "version": 3},
        "net_settings": {"classes": classes, "meta": {"version": 4}, "net_colors": None,
                         "netclass_assignments": None, "netclass_patterns": patterns},
        "pcbnew": {"last_paths": {}, "page_layout_descr_file": ""},
        "schematic": {"legacy_lib_dir": "", "legacy_lib_list": []},
        "sheets": [[_uid(d.name, "root"), "Root"]] +
                  [[_uid(d.name, "sheet", b), b] for b in sorted({c.block for c in d.components.values()})],
        "text_variables": {},
    }
    if pro["erc"]["pin_map"] is None:
        del pro["erc"]["pin_map"]
    p = prj.path(".kicad_pro")
    p.write_text(json.dumps(pro, indent=2))
    # library tables so KiCad GUI can resolve llmpcb: ids
    import os as _os
    try:   # project-relative so the KiCad project stays portable when the repo moves
        lib = "${KIPRJMOD}/" + _os.path.relpath(libs.LOCAL_DIR, prj.root.resolve())
    except ValueError:
        lib = str(libs.LOCAL_DIR)
    (prj.root / "sym-lib-table").write_text(
        f'(sym_lib_table\n  (version 7)\n  (lib (name "llmpcb")(type "KiCad")(uri "{lib}/symbols/llmpcb.kicad_sym")(options "")(descr "llm-pcb generated"))\n)\n')
    (prj.root / "fp-lib-table").write_text(
        f'(fp_lib_table\n  (version 7)\n  (lib (name "llmpcb")(type "KiCad")(uri "{lib}/footprints/llmpcb.pretty")(options "")(descr "llm-pcb generated"))\n)\n')
    return p


# ------------------------------------------------------------------ ERC + netlist check
def run_erc(prj: Project) -> dict:
    out = prj.root / "out" / "erc.json"
    out.parent.mkdir(exist_ok=True)
    subprocess.run(["kicad-cli", "sch", "erc", "--format", "json", "--severity-all", "-o", str(out),
                    str(prj.path(".kicad_sch"))], capture_output=True, text=True, timeout=300)
    data = json.loads(out.read_text())
    viol = []
    for sh in data.get("sheets", []):
        for v in sh.get("violations", []):
            viol.append({"sheet": sh.get("path", ""), "type": v.get("type"), "severity": v.get("severity"),
                         "desc": v.get("description"),
                         "items": [i.get("description") for i in v.get("items", [])][:3]})
    return {"violations": viol, "report": str(out)}


def check_netlist(prj: Project) -> dict:
    """Export the netlist with kicad-cli and diff it against the IR connectivity."""
    out = prj.root / "out" / "netlist.net"
    out.parent.mkdir(exist_ok=True)
    r = subprocess.run(["kicad-cli", "sch", "export", "netlist", "--format", "kicadsexpr", "-o", str(out),
                        str(prj.path(".kicad_sch"))], capture_output=True, text=True, timeout=300)
    if not out.exists():
        return {"ok": False, "error": r.stderr[-500:]}
    tree = sx.load(out)
    got: dict[tuple[str, str], str] = {}
    for net in sx.find_all(sx.find(tree, "nets"), "net"):
        name = str(sx.find(net, "name")[1]).lstrip("/")
        for node in sx.find_all(net, "node"):
            got[(str(sx.find(node, "ref")[1]), str(sx.find(node, "pin")[1]))] = name
    mism = []
    for c in prj.design.components.values():
        for pin, net in c.pins.items():
            g = got.get((c.ref, pin))
            if g is None or g.split("/")[-1] != net:
                mism.append(f"{c.ref}.{pin}: ir={net} sch={g}")
    return {"ok": not mism, "mismatches": mism[:30], "n_mismatch": len(mism)}
