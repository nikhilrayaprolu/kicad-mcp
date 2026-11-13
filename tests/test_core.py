"""Core tests: file formats, generators, engines, and an end-to-end board that must pass DRC."""
import json
import math
import shutil
from pathlib import Path

import pytest

from llmpcb import blocks, board, checks, gen, libs, parts, placer, router, schematic
from llmpcb import sexpr as sx
from llmpcb.ir import Project

TMP = Path(__file__).resolve().parent / "_tmp"


def fresh(name):
    p = TMP / name
    shutil.rmtree(p, ignore_errors=True)
    return Project.create(p, name)


def test_sexpr_roundtrip():
    src = '(a (b "x y" 1 2.5) (c d) (e "q\\"uote"))'
    t = sx.loads(src)
    assert sx.loads(sx.dumps(t)) == t
    assert t[3][1] == 'q"uote'


def test_qfn_generator_geometry():
    fp = gen.qfn("T_QFN", 128, 0.35, 12.3, 12.3, 0.7, 0.2, ep=(7, 7), ep_number="129", toe=0.2)
    pads = [libs.parse_pad(p) for p in sx.find_all(fp, "pad") if p[1] and p[1] != "129"]
    assert len(pads) == 128
    left = sorted([p for p in pads if p.x < -5.9], key=lambda p: p.y)
    assert len(left) == 32
    pitches = {round(b.y - a.y, 4) for a, b in zip(left, left[1:])}
    assert pitches == {0.35}
    assert left[0].number == "1"          # pin 1 top-left, CCW numbering


def test_rv1106_pinmap_from_knowledge():
    sid = parts.ensure_rv1106()
    s = libs.get_symbol(sid)
    assert len({p.number for p in s.pins}) == 129
    assert parts.resolve_pins(sid, "UART2_TX_M1") == ["79"]
    assert parts.resolve_pins(sid, "OSC_XIN") == ["68"]
    assert sorted(parts.resolve_pins(sid, "DDR_VDDQ*"), key=int) == ["49", "50", "56"]


def test_divider_math():
    rt, rb, v = blocks.divider(1.8, 0.6, 100e3)
    assert abs(v - 1.8) < 0.01


def test_rotation_matches_kicad_convention():
    # KiCad: rotating a footprint by +90 moves a pad at local (0, +2.54) to (+2.54, 0)
    x, y = libs.rot(0, 2.54, 90)
    assert math.isclose(x, 2.54, abs_tol=1e-9) and math.isclose(y, 0, abs_tol=1e-9)


def _small_design(prj):
    d = prj.design
    blocks.buck_tlv62569(d, "power", vin="VIN", vout_net="3V3", vout=3.3)
    parts.add(d, "HDR_1x4", block="io", conn={"1": "VIN", "2": "GND", "3": "3V3", "4": "GND"})
    from llmpcb.ir import Constraint, NetClass
    d.netclasses["PWR"] = NetClass(name="PWR", track=0.4, clearance=0.2, nets=["VIN", "3V3", "*_SW"])
    d.constraints.append(Constraint(kind="edge", ref="J1", side="left", rot=0))
    d.constraints.append(Constraint(kind="plane", net="GND", layer="In1.Cu"))
    d.board.width, d.board.height = 30, 20
    prj.save()


def test_schematic_erc_and_parity():
    prj = fresh("sch")
    _small_design(prj)
    schematic.build(prj)
    erc = schematic.run_erc(prj)
    assert not [v for v in erc["violations"] if v["severity"] == "error"]
    assert schematic.check_netlist(prj)["ok"]


def test_placement_is_legal():
    prj = fresh("place")
    _small_design(prj)
    res = placer.place(prj, effort=0.5)
    assert res["overlaps"] == [] and res["outside"] == []


def test_end_to_end_drc_clean():
    prj = fresh("e2e")
    _small_design(prj)
    schematic.build(prj)
    placer.place(prj, effort=0.5)
    r = router.route(prj, passes=30, timeout_s=300)
    assert r["ok"]
    from llmpcb.ir import Zone
    prj.layout.zones += [Zone(net="GND", layer="F.Cu"), Zone(net="GND", layer="B.Cu")]   # documented flow: outer pours
    board.build(prj)
    drc = checks.run_drc(prj, parity=True)
    assert drc["errors"] == 0, drc
    assert drc["unconnected"] == 0, drc
    assert drc["parity"] == 0, drc


def test_padstack_ids_are_opaque():
    """Freerouting mis-resolves descriptive padstack names that differ only in dimensions."""
    from llmpcb.board import AbsPad
    a = AbsPad("U", "1", "", 0, 0, .35, .1, ["F.Cu"], "roundrect", 0, .7, .2, 0)
    b = AbsPad("U", "2", "", 0, 0, .1, .35, ["F.Cu"], "roundrect", 0, .2, .7, 0)
    na, nb = router._padstack_name(a), router._padstack_name(b)
    assert na != nb and na.startswith("PS") and "x" not in na


def test_footprint_zone_absolute_coords():
    """Zones inside footprints are stored in absolute board coordinates."""
    prj = fresh("fpzone")
    d = prj.design
    parts.add(d, "MICROSD_DM3AT", ref="J1", block="sd", conn={"VSS": "GND", "VDD": "3V3"})
    from llmpcb.ir import Placement
    prj.layout.placement["J1"] = Placement(x=30, y=20, rot=90)
    d.board.width, d.board.height = 60, 40
    prj.save()
    board.build(prj)
    tree = sx.load(prj.path(".kicad_pcb"))
    fp = next(sx.find_all(tree, "footprint"))
    xs = [float(xy[1]) for z in sx.find_all(fp, "zone") for xy in sx.find_all(sx.find(sx.find(z, "polygon"), "pts"), "xy")]
    assert xs and min(xs) > 15 and max(xs) < 45     # around the part, not around the origin


def test_hroute_drc_clean():
    """Our own router: small board fully routed and KiCad-DRC clean (with outer GND pours)."""
    from llmpcb import hroute
    from llmpcb.ir import Zone
    prj = fresh("hroute")
    _small_design(prj)
    schematic.build(prj)
    placer.place(prj, effort=0.5)
    router.plane_fanout(prj, ["GND"])
    res = hroute.run(prj, "human", timeout_s=600)
    assert res["ok"] and not res["failed"], res.get("failed")
    hroute.apply(prj, res)
    prj.layout.zones += [Zone(net="GND", layer="In1.Cu"), Zone(net="GND", layer="F.Cu"), Zone(net="GND", layer="B.Cu")]
    board.build(prj)
    drc = checks.run_drc(prj, parity=True)
    assert drc["errors"] == 0, drc
    assert drc["unconnected"] == 0, drc


def test_bottom_side_mirroring_matches_kicad():
    """Bottom-side pads: our coordinates == KiCad's (IPC-D-356 export)."""
    import re
    import subprocess
    from llmpcb.ir import Placement
    prj = fresh("flip")
    d = prj.design
    d.board.width, d.board.height, d.board.layers = 30, 20, 2
    parts.add(d, "TLV62569DBV", ref="U1", block="a", conn={"VIN": "A", "GND": "G", "SW": "S", "FB": "F", "EN": "E"})
    prj.layout.placement = {"U1": Placement(x=10, y=8, rot=30, side="bottom")}
    prj.save()
    schematic.write_project_file(prj)
    board.build(prj)
    out = prj.root / "k.d356"
    subprocess.run(["kicad-cli", "pcb", "export", "ipcd356", "-o", str(out), str(prj.path(".kicad_pcb"))], capture_output=True)
    mine = {p.number: (p.x, p.y) for p in board.abs_pads(prj)}
    n = 0
    for line in out.read_text().splitlines():
        if line.startswith("327") and line[20:26].strip() == "U1":
            pin = line[27:31].strip().lstrip("-")
            m = re.search(r"X([+-]\d+)Y([+-]\d+)", line)
            x, y = int(m.group(1)) * 0.00254, -int(m.group(2)) * 0.00254
            assert abs(mine[pin][0] - x) < 0.01 and abs(mine[pin][1] - y) < 0.01, (pin, mine[pin], (x, y))
            n += 1
    assert n == 5
