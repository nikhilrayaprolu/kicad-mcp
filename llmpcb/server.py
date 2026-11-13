"""llm-pcb MCP server.

Design principle: the LLM states *intent* (parts, connectivity, reference blocks,
floorplan & constraints); deterministic engines produce geometry; every tool returns
compact engineering state (counts, violations, constraint misses) rather than raw
dumps.  ~16 coarse tools instead of hundreds of micro-operations.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import time
import traceback
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Image, MCPServer

from . import blocks, board, checks, export, libs, parts, placer, render, router, schematic
from .ir import BoardSpec, Constraint, NetClass, Placement, Project, Zone

INSTRUCTIONS = """kicad-mcp (llm-pcb): LLM-orchestrated PCB design. YOU decide intent (parts, nets, reference blocks,
netclasses, floorplan, constraints); deterministic engines produce all geometry; KiCad 10 (kicad-cli) verifies.
Never invent coordinates for tracks; never skip verification gates. Full playbook: AGENTS.md in the repo.
Proven flow:
 1 project_open(create) with requirements -> part_search/part_info/pin_query (resolve pins by datasheet function name)
 2 block_add (buck, decoupling, crystal, usb_c_sink, led, button, ...) + ir_edit (atomic batches) for the rest
 3 schematic_build -> must be ERC 0 errors AND netlist_parity ok before any layout
 4 board_setup -> ir_edit netclasses (check track+clearance can escape the finest pin pitch; design_review flags it),
   diffpair constraints, plane constraints (GND on In1), edge/region/near/side constraints
 5 place(seeds=64) -> render_view -> fix floorplan conflicts from the 'outside'/'near_violations' report, re-place
 6 route(engine="hybrid", candidates=16, profiles=["human","fewvias"], critical_nets=[crystal, *_SW, power])
 7 zones(gnd_pour_outer) -> copper(restitch) -> drc ; close leftovers with copper(repair) / route(mode="complete")
   / connect_pads ; copper(min_width) for neck-down artefacts ; copper(dangling_vias)
 8 tune_lengths -> drc -> design_review -> export_fab ; done only at DRC 0 errors / 0 unconnected / 0 parity.
Pins: number, exact name, a '/'-segment of the name, or a glob ('DVDD*'). Nets are created by connecting pins."""

mcp = MCPServer("kicad-mcp", instructions=INSTRUCTIONS)
STATE: dict[str, Any] = {"prj": None}
CALL_LOG = Path(os.environ.get("LLMPCB_CALL_LOG", "")) if os.environ.get("LLMPCB_CALL_LOG") else None


def _prj() -> Project:
    if STATE["prj"] is None:
        raise RuntimeError("no project open: call project_open first")
    return STATE["prj"]


def _log(tool: str, args: dict, result: Any, t0: float):
    prj = STATE.get("prj")
    rec = {"t": round(time.time(), 2), "tool": tool, "s": round(time.time() - t0, 2),
           "args_bytes": len(json.dumps(args, default=str)), "result_bytes": len(json.dumps(result, default=str))}
    if prj is not None:
        with open(prj.root / "mcp_calls.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")


def tool(fn):
    """Wrap: catch errors into compact messages, log size/latency, persist project."""
    import functools

    @functools.wraps(fn)
    def wrapped(*a, **kw):
        t0 = time.time()
        try:
            res = fn(*a, **kw)
        except Exception as e:  # noqa: BLE001
            res = {"error": f"{type(e).__name__}: {e}", "where": traceback.format_exc().splitlines()[-3][:200]}
        _log(fn.__name__, kw, res if not isinstance(res, (list, Image)) else "image", t0)
        if isinstance(res, dict):   # compact JSON: token-efficient responses
            return json.dumps(res, separators=(",", ":"), default=str)
        return res
    wrapped.__annotations__ = {k: v for k, v in fn.__annotations__.items() if k != "return"}
    return mcp.tool(structured_output=False)(wrapped)


# ======================================================================= project
@tool
def project_open(path: str, create: bool = False, name: str = "", description: str = "",
                 requirements: list[str] | None = None) -> dict:
    """Open (or create) a design project directory. Returns status."""
    p = Path(path).expanduser().resolve()
    if create:
        if (p / "design.json").exists():
            raise ValueError("project exists; use create=false")
        prj = Project.create(p, name or p.name, description=description, requirements=requirements or [])
    else:
        prj = Project(p)
    STATE["prj"] = prj
    return _status()


@tool
def project_status() -> dict:
    """Compact summary of the design state and suggested next step."""
    return _status()


def _status() -> dict:
    prj = _prj()
    d, L = prj.design, prj.layout
    nets = d.nets()
    blocks_ = {}
    for c in d.components.values():
        blocks_[c.block] = blocks_.get(c.block, 0) + 1
    unplaced = [r for r in d.components if r not in L.placement]
    last = {}
    for f in ("erc.json", "drc.json"):
        pth = prj.root / "out" / f
        if pth.exists():
            last[f] = time.strftime("%H:%M:%S", time.localtime(pth.stat().st_mtime))
    nxt = ("add parts/blocks" if not d.components else
           "schematic_build" if not prj.path(".kicad_sch").exists() else
           "place" if unplaced else "route" if not L.tracks else "drc / design_review / export_fab")
    return {"project": str(prj.root), "name": d.name, "components": len(d.components), "nets": len(nets),
            "single_pin_nets": [n for n, v in nets.items() if len(v) == 1][:15],
            "blocks": blocks_, "board": d.board.model_dump(), "netclasses": list(d.netclasses),
            "constraints": len(d.constraints), "placed": len(L.placement), "unplaced": len(unplaced),
            "tracks": len(L.tracks), "vias": len(L.vias), "zones": [f"{z.net}@{z.layer}" for z in L.zones],
            "last_checks": last, "next": nxt}


# ======================================================================= parts
@tool
def part_search(query: str, limit: int = 12) -> dict:
    """Search curated parts (with design facts), KiCad stock symbols and footprints (all words must match)."""
    return parts.search(query, limit)


@tool
def part_info(part: str) -> dict:
    """Details for a curated part id (or a KiCad symbol lib_id): symbol, footprint, pins, design facts."""
    if ":" in part:
        s = libs.get_symbol(part)
        return {"symbol": part, "default_footprint": s.props.get("Footprint", ""), "desc": s.props.get("Description", ""),
                "pins": [f"{p.number}:{p.name}:{p.etype}" for p in s.pins][:200]}
    return parts.part_info(part)


@tool
def pin_query(target: str, pattern: str = "*") -> dict:
    """List pins of a placed component (ref, e.g. 'U1') or a part id, filtered by glob on number/name
    (for RV1106 also matches every datasheet mux function, e.g. 'UART2*', '*SDMMC0*'). Shows current net."""
    import fnmatch
    prj = STATE.get("prj")
    if prj and target in prj.design.components:
        c = prj.design.components[target]
        sym = libs.get_symbol(c.symbol)
        cur = c.pins
    else:
        info = parts.PARTS.get(target) or {}
        sid = info.get("symbol") or (parts.GENERATED[target]["ensure"]() if target in parts.GENERATED else target)
        sym = libs.get_symbol(sid)
        cur = {}
    pat = pattern.upper()
    full = {}
    if sym.lib_id.endswith("RV1106G3"):
        full = {str(k): v["name"] for k, v in parts.rv1106_data()["pins"].items()}
    rows = []
    for p in sym.pins:
        names = full.get(p.number, p.name)
        if fnmatch.fnmatchcase(p.number, pat) or fnmatch.fnmatchcase(names.upper(), pat) or \
                any(fnmatch.fnmatchcase(s.upper(), pat) for s in names.split("/")):
            rows.append(f"{p.number}:{names}:{p.etype}" + (f" -> {cur[p.number]}" if p.number in cur else ""))
    return {"symbol": sym.lib_id, "matches": rows[:150], "n": len(rows)}


# ======================================================================= circuit edits
@tool
def block_add(block: str, params: dict) -> dict:
    """Instantiate a reference circuit: buck_tlv62569, decoupling, crystal, usb_c_sink, led, button,
    resistor, capacitor, ferrite. params = the block's keyword args (see blocks_list).
    Adds parts, nets and placement constraints; returns refs + design calculations."""
    prj = _prj()
    if block not in blocks.BLOCKS:
        raise KeyError(f"unknown block; available: {list(blocks.BLOCKS)}")
    res = blocks.BLOCKS[block](prj.design, **params)
    prj.save()
    prj.log("block_add", block=block, params=params)
    return res


@tool
def blocks_list() -> dict:
    """Signatures/docs of available reference-circuit blocks."""
    return blocks.describe()


@tool
def ir_edit(ops: list[dict]) -> dict:
    """Batch edit the design IR. Each op is {"op": ..., ...}:
    add_part {part|symbol+footprint, ref?, block, value?, conn{pin:net}, nc[], mpn?, lcsc?, note?}
    connect {ref, conn{pin_or_name_or_glob: net|"NC"}}
    remove {ref} | set {ref, field, value} | rename_net {old, new}
    netclass {name, track, clearance, via_d?, via_drill?, nets[globs], current_a?, diff_width?, diff_gap?}
    constraint {kind, ...}  (kinds: fixed, edge, near, region, keepout, diffpair, length_match, plane)
    clear_constraints {kind?, ref?, block?}
    board {width?, height?, layers?, corner_r?, mounting_holes?, title?, rev?, fab?}
    note {text} | requirement {text}
    The batch is atomic: if any op fails nothing is applied and the failing op index + reason are returned."""
    prj = _prj()
    snapshot = prj.design.model_copy(deep=True)
    d = prj.design
    done = []
    for i, op in enumerate(ops):
        o = dict(op)
        kind = o.pop("op")
        try:
            if kind == "add_part":
                c = parts.add(d, o.pop("part", None), **o)
                done.append(f"+{c.ref}")
            elif kind == "connect":
                n = parts.connect(d, o["ref"], o["conn"])
                done.append(f"{o['ref']}:{len(n)}pins")
            elif kind == "remove":
                d.components.pop(o["ref"])
                d.constraints = [c for c in d.constraints if c.ref != o["ref"]]
                prj.layout.placement.pop(o["ref"], None)
                done.append(f"-{o['ref']}")
            elif kind == "set":
                c = d.components[o["ref"]]
                if o["field"] in ("value", "mpn", "lcsc", "block", "footprint", "note", "dnp"):
                    if o["field"] == "footprint":
                        libs.get_footprint(o["value"])
                    setattr(c, o["field"], o["value"])
                else:
                    c.fields[o["field"]] = str(o["value"])
                done.append(f"{o['ref']}.{o['field']}")
            elif kind == "rename_net":
                for c in d.components.values():
                    for k, v in c.pins.items():
                        if v == o["old"]:
                            c.pins[k] = o["new"]
                done.append(f"{o['old']}->{o['new']}")
            elif kind == "netclass":
                d.netclasses[o["name"]] = NetClass(**o)
                done.append(f"nc:{o['name']}")
            elif kind == "constraint":
                d.constraints.append(Constraint(**o))
                done.append(f"c:{o['kind']}")
            elif kind == "clear_constraints":
                before = len(d.constraints)
                d.constraints = [c for c in d.constraints if not (
                    (o.get("kind") is None or c.kind == o["kind"]) and (o.get("ref") is None or c.ref == o["ref"]) and
                    (o.get("block") is None or c.block == o["block"]))]
                done.append(f"cleared {before - len(d.constraints)}")
            elif kind == "board":
                d.board = BoardSpec(**{**d.board.model_dump(), **o})
                done.append("board")
            elif kind == "note":
                d.notes.append(o["text"]); done.append("note")
            elif kind == "requirement":
                d.requirements.append(o["text"]); done.append("req")
            else:
                raise ValueError(f"unknown op {kind}")
        except Exception as e:  # noqa: BLE001
            prj.design = snapshot
            return {"applied": 0, "rolled_back": True, "failed_op": i, "error": f"{type(e).__name__}: {e}"}
    prj.save()
    prj.log("ir_edit", n=len(ops))
    return {"applied": len(done), "summary": done[:40], "components": len(d.components), "nets": len(d.nets())}


@tool
def netlist_view(block: str = "", net_glob: str = "*", max_nets: int = 60) -> dict:
    """Readable connectivity: nets (optionally of one block / glob) with their ref.pin(name) members."""
    import fnmatch
    d = _prj().design
    out = {}
    for n, nodes in d.nets().items():
        if not fnmatch.fnmatchcase(n, net_glob):
            continue
        if block and not any(d.components[r].block == block for r, _ in nodes):
            continue
        out[n] = [f"{r}.{p}" for r, p in nodes][:24]
        if len(out) >= max_nets:
            break
    return {"nets": out}


# ======================================================================= schematic
@tool
def schematic_build() -> dict:
    """Generate hierarchical schematic (one sheet per block), run ERC and verify netlist parity with the IR."""
    prj = _prj()
    info = schematic.build(prj)
    erc = schematic.run_erc(prj)
    parity = schematic.check_netlist(prj)
    errs = [v for v in erc["violations"] if v["severity"] == "error"]
    warns = [v for v in erc["violations"] if v["severity"] != "error"]
    from collections import Counter
    return {"sheets": info["sheets"], "pwr_flags": len(info["pwr_flags"]), "ir_issues": info["issues"][:20],
            "erc_errors": len(errs), "erc_warnings": len(warns),
            "erc_by_type": dict(Counter(v["type"] for v in erc["violations"])),
            "erc_examples": [f"{v['type']}: {v['desc']} {v['items'][:2]}" for v in (errs + warns)[:12]],
            "netlist_parity": parity}


# ======================================================================= board
@tool
def board_setup(width: float, height: float, layers: int = 4, corner_r: float = 1.0,
                mounting_holes: list[list[float]] | None = None, title: str = "", rev: str = "A",
                fab: str = "jlcpcb", default_track: float = 0.15, default_clearance: float = 0.15) -> dict:
    """Board outline, layer count, fab rule preset, mounting holes [[x,y,drill]...] and Default netclass."""
    prj = _prj()
    d = prj.design
    d.board = BoardSpec(width=width, height=height, layers=layers, corner_r=corner_r,
                        mounting_holes=mounting_holes or [], title=title or d.name, rev=rev, fab=fab)
    dc = d.netclasses.get("Default") or NetClass(name="Default")
    dc.track, dc.clearance = default_track, default_clearance
    d.netclasses["Default"] = dc
    prj.save()
    schematic.write_project_file(prj)
    return {"board": d.board.model_dump(), "fab_rules": schematic.FAB_RULES.get(fab)}


def _write_board(prj) -> dict:
    schematic.write_project_file(prj)
    return board.build(prj)


@tool
def place(effort: float = 1.0, seed: int = 1, unlock_all: bool = False, image: bool = True, seeds: int = 32,
          macros: bool = True, fanout_halo_mm: float = 1.0) -> list | dict:
    """Run constraint-driven placement (regions, edge, fixed, near, keepouts) and write the PCB.
    seeds>1 runs that many independent annealing chains in parallel (one per core) and keeps the best.
    macros: parts with tight `near` constraints form rigid clusters around their hub (buck IC, SoC, connector).
    fanout_halo_mm: keep this ring free around fine-pitch ICs (>=32 pads) for escape vias.
    Locked parts (fixed/edge or locked placement) stay put unless unlock_all. Returns stats (+ 2-D image)."""
    prj = _prj()
    if unlock_all:
        for p in prj.layout.placement.values():
            p.locked = False
    # parts placed earlier but not constrained are re-placed freely
    fixedrefs = {c.ref for c in prj.design.constraints if c.kind in ("fixed", "edge")}
    for r, p in list(prj.layout.placement.items()):
        if r not in prj.design.components:
            prj.layout.placement.pop(r)
        elif p.locked and r not in fixedrefs and unlock_all:
            p.locked = False
    prj.layout.tracks, prj.layout.vias = [], []   # placement invalidates all copper (incl. fanout)
    kw = dict(macros=macros, fanout_halo_mm=fanout_halo_mm)
    res = placer.place_parallel(prj, seeds=seeds, effort=effort, **kw) if seeds > 1 else \
        placer.place(prj, effort=effort, seed=seed, **kw)
    prj.save()
    _write_board(prj)
    if image:
        png = render.render_2d(prj)
        return [json.dumps(res), Image(path=str(png))]
    return res


@tool
def place_part(ref: str, near: str, max_radius_mm: float = 12.0, rot: float | None = None) -> dict:
    """Place ONE (new) part at the nearest legal spot to `near` ("REF", "REF.pin" or "x,y") without touching
    any other part or copper; checks courtyards, board edge, keepouts and existing tracks/vias on F.Cu."""
    import math as _m
    import numpy as _np
    prj = _prj()
    d, L = prj.design, prj.layout
    if "," in near:
        tx, ty = map(float, near.split(","))
    else:
        r_, _, pin = near.partition(".")
        pads = [p for p in board.abs_pads(prj) if p.ref == r_ and (not pin or p.number == pin or
                any(q.name == pin and q.number == p.number for q in libs.get_symbol(d.components[r_].symbol).pins))]
        if not pads:
            raise ValueError(f"cannot resolve {near}")
        tx, ty = sum(p.x for p in pads) / len(pads), sum(p.y for p in pads) / len(pads)
    rects = []
    for r2, pl in L.placement.items():
        if r2 == ref or r2 not in d.components:
            continue
        bb = libs.bbox_rot(libs.get_footprint_side(d.components[r2].footprint, pl.side).courtyard, pl.rot)
        rects.append((pl.x + bb[0], pl.y + bb[1], pl.x + bb[2], pl.y + bb[3]))
    for mh in d.board.mounting_holes:
        rr = (mh[2] if len(mh) > 2 else d.board.mh_drill) / 2 + 1.6
        rects.append((mh[0] - rr, mh[1] - rr, mh[0] + rr, mh[1] + rr))
    R = _np.array(rects)
    obs = router._Obstacles(prj)
    fp = libs.get_footprint(d.components[ref].footprint)
    W, H = d.board.width, d.board.height
    for rad in _np.arange(0.0, max_radius_mm, 0.1):
        n = max(8, int(2 * _m.pi * max(rad, 0.1) / 0.1))
        for k in range(n):
            a = 2 * _m.pi * k / n
            x, y = round(tx + rad * _m.cos(a), 2), round(ty + rad * _m.sin(a), 2)
            for rt in ([rot] if rot is not None else [0.0, 90.0, 180.0, 270.0]):
                b = libs.bbox_rot(fp.courtyard, rt)
                q = (x + b[0] - 0.1, y + b[1] - 0.1, x + b[2] + 0.1, y + b[3] + 0.1)
                if q[0] < 0.5 or q[1] < 0.5 or q[2] > W - 0.5 or q[3] > H - 0.5:
                    continue
                if len(R) and ((_np.minimum(R[:, 2], q[2]) - _np.maximum(R[:, 0], q[0]) > 0) &
                               (_np.minimum(R[:, 3], q[3]) - _np.maximum(R[:, 1], q[1]) > 0)).any():
                    continue
                ok = True
                for p in fp.pads:     # pads must clear existing copper of other nets
                    px, py = libs.pad_abs(p, x, y, rt)
                    hx, hy = libs.pad_extent(p, rt)
                    net = d.components[ref].pins.get(p.number, "")
                    if not obs.clear(px, py, max(hx, hy), net, 0.15):
                        ok = False
                        break
                if ok:
                    L.placement[ref] = Placement(x=x, y=y, rot=rt, locked=True)
                    prj.save()
                    _write_board(prj)
                    return {"ref": ref, "x": x, "y": y, "rot": rt, "dist_mm": round(float(rad), 2)}
    return {"error": f"no legal spot for {ref} within {max_radius_mm} mm of {near}"}


@tool
def connect_pads(a: str, b: str, layer: str = "B.Cu", width: float = 0.0) -> dict:
    """Draw a straight track between two pads of the SAME net ("REF.pin"), only if the straight path is clear of
    other-net copper on that layer (checked with netclass clearance). For last-mile fixes the router missed."""
    from .ir import Track
    prj = _prj()
    pads = {f"{p.ref}.{p.number}": p for p in board.abs_pads(prj)}
    pa, pb = pads.get(a), pads.get(b)
    if not pa or not pb:
        raise ValueError("unknown pad")
    if pa.net != pb.net or not pa.net:
        raise ValueError(f"nets differ: {pa.net} vs {pb.net}")
    if layer not in pa.layers or layer not in pb.layers:
        raise ValueError(f"both pads must exist on {layer} (THT or same-side SMD)")
    nc = prj.design.netclass_of(pa.net)
    w = width or nc.track
    # layer-specific obstacle check: pads on that layer + tracks on that layer + all vias
    import math as _m
    blockers = []
    for p in pads.values():
        if p.net != pa.net and layer in p.layers:
            blockers.append(("pad", p))
    def seg_d(px, py, ax, ay, bx, by):
        return router._seg_dist(px, py, ax, ay, bx, by)
    n = max(4, int(_m.hypot(pb.x - pa.x, pb.y - pa.y) / 0.05))
    pts = [(pa.x + (pb.x - pa.x) * k / n, pa.y + (pb.y - pa.y) * k / n) for k in range(n + 1)]
    clr = nc.clearance
    for x, y in pts:
        for _, p in blockers:
            dx = max(p.x - p.hx - x, 0, x - p.x - p.hx); dy = max(p.y - p.hy - y, 0, y - p.y - p.hy)
            if _m.hypot(dx, dy) < w / 2 + clr:
                return {"error": f"blocked by pad {p.ref}.{p.number} [{p.net}]"}
        for t in prj.layout.tracks:
            if t.layer != layer or t.net == pa.net:
                continue
            for q1, q2 in zip(t.pts, t.pts[1:]):
                if seg_d(x, y, q1[0], q1[1], q2[0], q2[1]) < w / 2 + t.width / 2 + clr:
                    return {"error": f"blocked by track [{t.net}] on {layer}"}
        for v in prj.layout.vias:
            if v.net != pa.net and _m.hypot(v.x - x, v.y - y) < w / 2 + v.d / 2 + clr:
                return {"error": f"blocked by via [{v.net}] at ({v.x},{v.y})"}
    prj.layout.tracks.append(Track(net=pa.net, layer=layer, pts=[[pa.x, pa.y], [pb.x, pb.y]], width=w, locked=True))
    prj.save()
    _write_board(prj)
    return {"added": f"{a}->{b} on {layer}", "len_mm": round(_m.hypot(pb.x - pa.x, pb.y - pa.y), 2), "width": w}


@tool
def move(moves: list[dict]) -> dict:
    """Manually set/lock part positions: [{ref, x?, y?, rot?, dx?, dy?, lock?}]. Use for intent the placer can't infer."""
    prj = _prj()
    for m in moves:
        p = prj.layout.placement.get(m["ref"]) or Placement(x=0, y=0)
        p.x = m.get("x", p.x + m.get("dx", 0))
        p.y = m.get("y", p.y + m.get("dy", 0))
        p.rot = m.get("rot", p.rot)
        p.locked = m.get("lock", True)
        prj.layout.placement[m["ref"]] = p
    prj.save()
    _write_board(prj)
    return {"moved": len(moves)}


@tool
def route(passes: int = 100, timeout_s: int = 900, image: bool = True, candidates: int = 12,
          fanout_planes: bool = True, mode: str = "full", engine: str = "freerouting",
          profiles: list[str] | None = None, finish_with_freerouting: bool = True,
          critical_nets: list[str] | None = None) -> list | dict:
    """Autoroute with Freerouting (planes from 'plane' constraints become pours), then refill zones + DRC.
    candidates>1 routes the top-N placements from the last parallel `place` concurrently (one core-group each)
    and keeps the best routed board (fewest unrouted, then violations, vias, length). fanout_planes drops a via
    next to every SMD pad of plane nets first, so the router only handles signals.
    mode="complete": keep all existing copper (protected) and route only the missing connections.
    engine="hroute": llm-pcb's own human-like router (45-degree, few bends/vias, layer directions, neck-down at
    fine pins, coupled differential pairs). engine="hybrid": critical_nets (globs) + diff pairs by hroute, locked,
    then Freerouting routes the rest. Routes candidates x profiles (human|fewvias|tight|orthogonal) in
    parallel; finish_with_freerouting lets Freerouting complete any leftovers around the protected copper."""
    import json as _json
    prj = _prj()
    if mode == "complete" and engine == "hroute":   # our router, only the listed nets, around all copper
        from . import hroute as _hr
        res = _hr.complete_nets(prj, critical_nets or [], (profiles or ["human"])[0], timeout_s=timeout_s)
        prj.save()
        _write_board(prj)
        drc_ = checks.run_drc(prj)
        res["drc"] = {k: drc_.get(k) for k in ("errors", "unconnected", "by_type", "unconnected_by_net")}
        return [json.dumps(res), Image(path=str(render.render_2d(prj)))] if image else res
    if mode == "complete":     # keep all copper, route only what's missing
        res = router.complete(prj, passes=passes, timeout_s=timeout_s)
        prj.save()
        _write_board(prj)
        drc_ = checks.run_drc(prj)
        res["drc"] = {k: drc_.get(k) for k in ("errors", "unconnected", "by_type", "unconnected_by_net")}
        return [json.dumps(res), Image(path=str(render.render_2d(prj)))] if image else res
    cdir = prj.root / "out" / "placements"
    cands = [_json.loads(f.read_text()) for f in sorted(cdir.glob("rank*.json"))][:candidates] if cdir.exists() else []
    if engine == "hybrid":      # critical nets + diff pairs by hroute (locked), the rest by Freerouting
        from . import hroute as _hr
        crit = critical_nets or ["*_XIN", "*_XOUT*", "*_SW", "VSYS"]
        if candidates > 1 and cands:
            res = _hr.hybrid_portfolio(prj, cands[:candidates], profiles or ["human", "fewvias"], crit,
                                       passes=passes, timeout_s=timeout_s)
        else:
            res = _hr.route_critical_then_complete(prj, crit, (profiles or ["human"])[0], passes=passes,
                                                   timeout_s=timeout_s)
        prj.save()
        _write_board(prj)
        drc_ = checks.run_drc(prj)
        res["drc"] = {k: drc_.get(k) for k in ("errors", "unconnected", "by_type", "unconnected_by_net")}
        return [json.dumps(res), Image(path=str(render.render_2d(prj)))] if image else res
    if engine == "hroute":
        from . import hroute as _hr
        res = _hr.route_portfolio(prj, cands or [None], profiles or ["human", "fewvias"], fanout_planes=fanout_planes,
                                  timeout_s=timeout_s)
        prj.save()
        if res.get("ok") and finish_with_freerouting and res.get("failed"):
            fin = router.complete(prj, passes=passes, timeout_s=min(timeout_s, 900))
            res["freerouting_finish"] = {k: fin.get(k) for k in ("new_tracks", "new_vias", "fr_unrouted")}
            prj.save()
        _write_board(prj)
        drc_ = checks.run_drc(prj)
        res["drc"] = {k: drc_.get(k) for k in ("errors", "unconnected", "by_type", "unconnected_by_net")}
        return [json.dumps(res), Image(path=str(render.render_2d(prj)))] if image else res
    if candidates > 1 and cands:
        res = router.route_placements(prj, cands, passes=passes, timeout_s=timeout_s, fanout_planes=fanout_planes)
    else:
        if fanout_planes:
            prj.layout.tracks, prj.layout.vias = [t for t in prj.layout.tracks if t.locked], [v for v in prj.layout.vias if v.locked]
            planes = sorted({c.net for c in prj.design.constraints if c.kind == "plane"})
            if planes:
                router.plane_fanout(prj, planes)
        res = router.route(prj, passes=passes, timeout_s=timeout_s)
    prj.save()
    if not res.get("ok"):
        return res
    _write_board(prj)
    drc = checks.run_drc(prj)
    res["drc"] = {k: drc.get(k) for k in ("errors", "warnings", "unconnected", "by_type", "unconnected_by_net")}
    if image:
        png = render.render_2d(prj)
        return [json.dumps(res), Image(path=str(png))]
    return res


@tool
def zones(add: list[dict] | None = None, clear: bool = False, gnd_pour_outer: bool = False) -> dict:
    """Copper pours. add: [{net, layer, poly?[[x,y]..], priority?, clearance?, connect?}]. gnd_pour_outer adds
    GND pours on F.Cu and B.Cu. Zones are filled by kicad-cli during drc/export."""
    prj = _prj()
    L = prj.layout
    if clear:
        L.zones = []
    for z in add or []:
        L.zones.append(Zone(**z))
    if gnd_pour_outer:
        for lay in ("F.Cu", "B.Cu"):
            if not any(z.net == "GND" and z.layer == lay for z in L.zones):
                L.zones.append(Zone(net="GND", layer=lay, priority=0, clearance=0.2))
    prj.save()
    _write_board(prj)
    return {"zones": [f"{z.net}@{z.layer} p{z.priority}" for z in L.zones]}


@tool
def copper(action: str, nets: list[str] | None = None, net: str = "GND", pitch: float = 3.0,
           clear_unlocked_routes: bool = False) -> dict:
    """Deterministic copper helpers. action=plane_fanout: drop a via + stub next to every SMD pad of the given
    plane nets (run BEFORE route so the router only has signals left). action=stitch: grid of `net` vias in free
    space (after routing, ties pours/planes). action=clear: remove all tracks/vias (keep locked unless
    clear_unlocked_routes=False is overridden)."""
    prj = _prj()
    if action == "plane_fanout":
        res = router.plane_fanout(prj, nets or ["GND"])
    elif action == "stitch":
        res = router.stitch_vias(prj, net=net, pitch=pitch)
    elif action == "restitch":
        removed = router.remove_free_vias(prj, net)
        res = {"removed": removed, **router.stitch_vias(prj, net=net, pitch=pitch)}
    elif action == "dangling_vias":     # remove one-layer vias, then trim the stubs they leave
        res = {"removed_vias": router.remove_dangling_vias(prj), "trimmed_points": router.prune_stubs(prj)}
    elif action == "repair":       # parallel rip-up/re-route variants scored by KiCad DRC
        res = router.parallel_repair(prj, variants=int(pitch) if pitch > 3 else 24)
    elif action == "ripup":        # remove all (incl. locked) copper of the given nets so they can be re-routed
        ns = set(nets or [])
        nt, nv = len(prj.layout.tracks), len(prj.layout.vias)
        prj.layout.tracks = [t for t in prj.layout.tracks if t.net not in ns]
        prj.layout.vias = [v for v in prj.layout.vias if v.net not in ns]
        res = {"removed_tracks": nt - len(prj.layout.tracks), "removed_vias": nv - len(prj.layout.vias)}
    elif action == "min_width":    # widen tracks below the fab minimum (router neck-down artefacts)
        mn = schematic.FAB_RULES.get(prj.design.board.fab, schematic.FAB_RULES["jlcpcb"])["min_track_width"]
        n = 0
        for t in prj.layout.tracks:
            if t.width < mn - 1e-6:
                t.width = mn
                n += 1
        res = {"widened": n, "to_mm": mn}
    elif action == "clear":
        prj.layout.tracks, prj.layout.vias = [], []
        res = {"cleared": True}
    else:
        raise ValueError("action must be plane_fanout|stitch|restitch|repair|ripup|min_width|clear")
    prj.save()
    _write_board(prj)
    return res


@tool
def tune_lengths(pairs: list[str] | None = None, default_tol: float = 0.5) -> dict:
    """Length-match differential pairs: serpentine meanders on the shorter leg, sized to the skew and checked
    against copper on that layer with netclass clearance. pairs = optional list of net names to limit to."""
    from . import tune
    prj = _prj()
    res = tune.tune_all(prj, default_tol, only=pairs)
    prj.save()
    _write_board(prj)
    return res


@tool
def drc(parity: bool = True) -> dict:
    """Refill zones, run KiCad DRC (incl. schematic parity), return grouped violations with examples."""
    prj = _prj()
    _write_board(prj)
    return checks.run_drc(prj, parity=parity)


@tool
def design_review() -> dict:
    """Engineering review beyond DRC: near-constraint misses (decoupling/crystal), diff-pair skew, current vs width,
    unconnected power pins, single-pin nets."""
    return checks.design_review(_prj())


@tool
def render_view(kind: str = "2d", side: str = "top", page: int = 0, layers: list[str] | None = None,
                highlight: list[str] | None = None) -> list:
    """Look at the design: kind=2d (fast placement/routing view), 3d (kicad-cli render), schematic (page N)."""
    prj = _prj()
    if kind == "3d":
        p = render.render_3d(prj, side=side)
    elif kind == "schematic":
        ps = render.render_schematic(prj, page=page or 1)
        p = ps[0]
    else:
        p = render.render_2d(prj, layers=layers, highlight=highlight)
    return [f"{kind} view: {p}", Image(path=str(p))]


@tool
def export_fab(step: bool = False) -> dict:
    """Gerbers (zones refilled), Excellon drill, CPL, BOM (JLC format), IPC-D-356, PDFs; zips gerbers."""
    prj = _prj()
    _write_board(prj)
    return export.export_fab(prj, step=step)


@tool
def run_script(code: str) -> dict:
    """Escape hatch: run Python with `prj`, `d` (design), `L` (layout) and llmpcb modules in scope.
    Saves the project afterwards. Use sparingly; every use is logged as a capability gap."""
    prj = _prj()
    buf = io.StringIO()
    env = {"prj": prj, "d": prj.design, "L": prj.layout, "parts": parts, "blocks": blocks, "libs": libs,
           "board": board, "router": router, "placer": placer, "checks": checks}
    with contextlib.redirect_stdout(buf):
        exec(code, env)  # noqa: S102 - local, user-operated tool
    prj.save()
    prj.log("run_script", code=code[:2000])
    return {"stdout": buf.getvalue()[-3000:]}


@tool
def sync_from_kicad() -> dict:
    """Pull footprint positions/rotations and tracks/vias back from the .kicad_pcb (after manual edits in KiCad)."""
    from . import sexpr as sx
    from .ir import Track, Via
    prj = _prj()
    tree = sx.load(prj.path(".kicad_pcb"))
    netnames = {}
    for n in sx.find_all(tree, "net"):
        if len(n) > 2:
            netnames[n[1]] = str(n[2])
    moved = 0
    for fp in sx.find_all(tree, "footprint"):
        ref = sx.prop(fp, "Reference")
        at = sx.find(fp, "at")
        if ref in prj.design.components:
            pl = prj.layout.placement.get(ref)
            x, y, r = float(at[1]), float(at[2]), float(at[3]) if len(at) > 3 else 0.0
            if pl is None or abs(pl.x - x) > 1e-3 or abs(pl.y - y) > 1e-3 or pl.rot != r:
                moved += 1
                prj.layout.placement[ref] = Placement(x=x, y=y, rot=r, locked=True)

    def netof(node):
        n = sx.find(node, "net")
        if n is None:
            return ""
        v = n[1]
        return netnames.get(v, str(v)) if isinstance(v, int) else str(v)
    tracks = []
    for s in sx.find_all(tree, "segment"):
        a, b = sx.find(s, "start"), sx.find(s, "end")
        tracks.append(Track(net=netof(s), layer=str(sx.find(s, "layer")[1]), pts=[[a[1], a[2]], [b[1], b[2]]],
                            width=float(sx.find(s, "width")[1])))
    vias = []
    for v in sx.find_all(tree, "via"):
        at = sx.find(v, "at")
        vias.append(Via(net=netof(v), x=at[1], y=at[2], d=float(sx.find(v, "size")[1]), drill=float(sx.find(v, "drill")[1])))
    prj.layout.tracks, prj.layout.vias = tracks, vias
    prj.save()
    return {"moved": moved, "tracks": len(tracks), "vias": len(vias)}


@tool
def ipc_sync(direction: str = "push", copper: bool = True) -> dict:
    """LIVE MODE (optional): sync with a running KiCad 10 PCB editor over the IPC API. push = move footprints /
    replace copper in the open board from the IR; pull = read human edits back. Needs the KiCad GUI with the
    API server enabled (KiCad 10 IPC is GUI-only)."""
    from . import ipc
    prj = _prj()
    return ipc.push(prj, copper=copper) if direction == "push" else ipc.pull(prj)


def main():
    mcp.run("stdio")


if __name__ == "__main__":
    main()
