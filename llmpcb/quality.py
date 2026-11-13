"""Expert quality gate and publishing.

`expert_review` is the bar a board must clear before it is called done. It is the standard the RV1106
reference board was brought to. It goes beyond "DRC clean": it also checks the details that make a layout
look and behave like an experienced engineer made it. Every failed check names the tool that fixes it.

`publish` renders the board (2-D layout, 3-D top/bottom, every schematic sheet) into designs/<name>/images,
writes a gallery README with the review metrics, and commits it. With push=True it also pushes.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

from . import board as brd
from . import checks, fills, libs, render
from .ir import Project


def netlist_hash(prj: Project) -> str:
    """Fingerprint of everything the schematic depends on (ERC/parity freshness, independent of file times)."""
    import hashlib
    d = prj.design
    key = sorted((c.ref, c.symbol, c.footprint, c.value, sorted(c.pins.items()), sorted(c.nc)) for c in d.components.values())
    return hashlib.sha1(json.dumps(key, default=str).encode()).hexdigest()[:16]


def _fill_coverage(prj: Project) -> dict:
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    from shapely.validation import make_valid
    area = Polygon(brd.outline_poly(prj.design)).area
    by_layer = defaultdict(list)
    for _net, lay, pts in fills.read_fills(prj):
        by_layer[lay].append(make_valid(Polygon(pts)))
    return {lay: round(unary_union(ps).area / area, 3) for lay, ps in by_layer.items()}


def _corner_stats(prj: Project) -> dict:
    right = acute = total = 0
    for t in prj.layout.tracks:
        for a, b, c in zip(t.pts, t.pts[1:], t.pts[2:]):
            v1 = (b[0] - a[0], b[1] - a[1]); v2 = (c[0] - b[0], c[1] - b[1])
            l1, l2 = math.hypot(*v1), math.hypot(*v2)
            if l1 < 1e-6 or l2 < 1e-6:
                continue
            cos = (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2)
            if cos > 0.999:
                continue
            total += 1
            if abs(cos) < 0.02:
                right += 1
            elif cos < -0.02:          # turning by more than 90 degrees: the track folds back (acid trap)
                acute += 1
    return {"corners": total, "right_angle": right, "acute": acute}


def expert_review(prj: Project, run_drc: bool = True) -> dict:
    d, L = prj.design, prj.layout
    fail, ok, notes = [], [], []

    def check(cond, name, detail, fix):
        (ok.append(name) if cond else fail.append({"check": name, "detail": detail, "fix": fix}))

    # --- verification gates
    erc_p = prj.root / "out" / "erc.json"
    st_p = prj.root / "out" / "erc_state.json"
    try:
        stale = json.loads(st_p.read_text()).get("netlist_hash") != netlist_hash(prj)
    except Exception:  # noqa: BLE001
        stale = True
    erc_err = None
    if erc_p.exists():
        try:
            ej = json.loads(erc_p.read_text())
            erc_err = sum(1 for s in ej.get("sheets", []) for v in s.get("violations", []) if v.get("severity") == "error")
        except Exception:  # noqa: BLE001
            pass
    check(not stale and erc_err == 0, "erc_clean", f"ERC errors {erc_err}, stale={stale}", "schematic_build")
    drc = checks.run_drc(prj, parity=True) if run_drc else {}
    check(drc.get("errors") == 0 and drc.get("unconnected") == 0 and drc.get("parity") == 0, "drc_clean",
          {k: drc.get(k) for k in ("errors", "unconnected", "parity")}, "copper repair / route mode=complete / connect_pads")
    check(drc.get("warnings") == 0, "drc_zero_warnings", drc.get("by_type"),
          "silk: board writer legalises it (check silk_texts positions); isolated_copper: copper stitch_islands")
    rv = checks.design_review(prj)
    errs = [f for f in rv["findings"] if f["sev"] == "error"]
    check(not errs, "design_review_no_errors", errs[:6], "fix each finding at IR/placement level")

    # --- differential pairs
    pv = [f["msg"] for f in rv["findings"] if f["rule"] == "diffpair_vias"]
    check(not pv, "diffpair_via_symmetry", pv,
          "copper ripup nets=[P,N] (+blockers) then route mode=complete engine=hroute critical_nets=[P,N]")
    sk = [f["msg"] for f in rv["findings"] if f["rule"] == "diffpair_skew"]
    check(not sk, "diffpair_skew_in_tolerance", sk, "tune_lengths(reset=true)")

    # --- copper pours on every layer
    cov = _fill_coverage(prj)
    cul = brd.copper_layers(d.board.layers)
    low = {l: cov.get(l, 0.0) for l in cul if cov.get(l, 0.0) < (0.35 if l in ("F.Cu", "B.Cu") else 0.6)}
    check(not low, "pours_all_layers", {"coverage": cov, "low": low},
          "zones(gnd_pour_outer=true) + In-layer planes; copper restitch; copper stitch_islands")
    gnd_vias = sum(1 for v in L.vias if v.net.upper().startswith("GND"))
    check(gnd_vias >= (d.board.width * d.board.height) / 40, "ground_stitching", f"{gnd_vias} GND vias",
          "copper restitch net=GND pitch=5 edge_pitch=2.5")

    # --- geometry style
    cs = _corner_stats(prj)
    check(cs["acute"] == 0, "no_acute_corners", cs, "copper smooth / ripup + re-route the net")
    check(cs["right_angle"] <= 0.15 * max(1, cs["corners"]), "mostly_45_degree_routing", cs, "copper smooth")

    # --- assembly / fab / bring-up hygiene
    fine = any(min((abs(a.x - b.x) + abs(a.y - b.y)) for a, b in zip(fp.pads, fp.pads[1:])) < 0.55
               for fp in (libs.get_footprint(c.footprint) for c in d.components.values()) if len(fp.pads) > 8)
    check(len(d.board.fiducials) >= 3 or not fine, "fiducials", f"{len(d.board.fiducials)} fiducials, fine pitch={fine}",
          'ir_edit {"op":"board","fiducials":[[x,y,"top"],...]} (3, asymmetric)')
    check(len(d.board.mounting_holes) >= 2, "mounting_holes", d.board.mounting_holes, "board_setup mounting_holes")
    tp_nets = {c.pins.get("1") for c in d.components.values() if c.footprint.startswith("TestPoint:")}
    pins_on = Counter(n for c in d.components.values() for n in c.pins.values() if n)
    rails = sorted(n for n, k in pins_on.items()           # main rails (and GND): power-class nets with >= 8 pins
                   if k >= 8 and (n.upper().startswith("GND") or d.netclass_of(n).name in ("PWR", "PWR_HI")))
    missing_tp = [r for r in rails if r not in tp_nets]
    check(not missing_tp, "test_points_on_rails",
          {"rails": rails, "without_tp": missing_tp}, "add TestPoint parts on each rail (+ GND), side bottom")
    labelled = {st.get("ref") for st in d.board.silk_texts}
    conns = sorted(r for r in d.components if r.startswith("J"))
    unl = [r for r in conns if r not in labelled]
    check(not unl, "connector_labels", unl, 'ir_edit {"op":"board","silk_texts":[{"text":..,"ref":"J1","dx":..,"dy":..}]}')
    check(bool(d.board.title), "board_title_rev", d.board.title, 'ir_edit {"op":"board","title":..,"rev":..}')

    # --- placement quality
    near = [f["msg"] for f in rv["findings"] if f["rule"] == "near"]
    if near:
        notes.append({"near_misses": near[:12], "judge": "decoupling/crystal/inductor distance; tighten regions or side:any"})
    other = [f["msg"] for f in rv["findings"] if f["sev"] == "warn" and f["rule"] not in ("near", "diffpair_vias", "diffpair_skew")]
    if other:
        notes.append({"other_warnings": other[:10]})
    grade = "expert" if not fail else "needs_work"
    return {"grade": grade, "passed": ok, "failed": fail, "notes": notes,
            "metrics": {"drc": {k: drc.get(k) for k in ("errors", "warnings", "unconnected", "parity")},
                        "pour_coverage": cov, "corners": cs, "vias": len(L.vias), "tracks": len(L.tracks),
                        "components": len(d.components)},
            "visual_checklist": [
                "3D top/bottom: every connector faces its board edge; nothing overhangs the outline",
                "silkscreen: one designator per visible part, readable, none on pads; connector/pin-1 labels present",
                "pours: copper visible on all layers, no slivers or necked islands; via fence along the edge",
                "routing: 45-degree, no needless detours or via pairs; pairs run together and change layer together",
                "repeated cells (bucks, channels) laid out identically; decoupling hugs its pins",
                "schematic: one functional cluster per IC, readable labels, sheet not oversized"]}


def _git(root: Path, *args, timeout=120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=timeout)


def publish(prj: Project, push: bool = False, message: str = "", allow_needs_work: bool = False,
            review: dict | None = None, commit: bool = True) -> dict:
    """Render images + gallery README into designs/<name>/, commit them with the design sources; optionally push."""
    review = review or expert_review(prj)
    if review["grade"] != "expert" and not allow_needs_work:
        return {"published": False, "reason": "expert_review failed: fix these first (or allow_needs_work=true)",
                "failed": review["failed"]}
    img = prj.root / "images"
    img.mkdir(exist_ok=True)
    outs = {"layout_2d.png": render.render_2d(prj, out=img / "layout_2d.png"),
            "pcb_3d_top.png": render.render_3d(prj, "top", out=img / "pcb_3d_top.png"),
            "pcb_3d_bottom.png": render.render_3d(prj, "bottom", out=img / "pcb_3d_bottom.png")}
    for lay in brd.copper_layers(prj.design.board.layers):
        nm = f"layer_{lay.replace('.', '_')}.png"
        outs[nm] = render.render_2d(prj, out=img / nm, layers=[lay], ratsnest=False)
    sch = []
    try:
        for i, p in enumerate(render.render_schematic(prj, page=0, dpi=110)):
            dst = img / f"schematic_p{i + 1}.png"
            shutil.copy(p, dst)
            sch.append(dst.name)
    except Exception as e:  # noqa: BLE001
        sch.append(f"(schematic render failed: {e})")
    m = review["metrics"]
    d = prj.design
    lines = [f"# {d.board.title or d.name} (rev {d.board.rev})", "",
             f"{d.description}" if getattr(d, "description", "") else "", "",
             f"{d.board.width} × {d.board.height} mm, {d.board.layers} layers, {m['components']} parts, "
             f"{m['vias']} vias. Generated with kicad-mcp.", "",
             "| Check | Result |", "|---|---|",
             f"| Expert review | **{review['grade']}** ({len(review['passed'])} passed, {len(review['failed'])} failed) |",
             f"| KiCad DRC | {m['drc']['errors']} errors, {m['drc']['warnings']} warnings, "
             f"{m['drc']['unconnected']} unconnected, {m['drc']['parity']} parity |",
             "| Pour coverage | " + ", ".join(f"{k} {v:.0%}" for k, v in sorted(m["pour_coverage"].items())) + " |", "",
             "| Top (3D) | Bottom (3D) |", "|---|---|",
             "| ![top](images/pcb_3d_top.png) | ![bottom](images/pcb_3d_bottom.png) |", "",
             "![layout](images/layout_2d.png)", "",
             "| " + " | ".join(n for n in outs if n.startswith("layer_")) + " |",
             "|" + "---|" * sum(1 for n in outs if n.startswith("layer_")),
             "| " + " | ".join(f"![{n}](images/{n})" for n in outs if n.startswith("layer_")) + " |", "",
             "## Schematic", ""] + [f"![{s}](images/{s})" for s in sch if s.endswith(".png")]
    if review["failed"]:
        lines += ["", "## Open review items", ""] + [f"- **{f['check']}**: {f['detail']}" for f in review["failed"]]
    (prj.root / "README.md").write_text("\n".join(lines) + "\n")
    res = {"published": True, "images": sorted(outs) + sch, "readme": str(prj.root / "README.md")}
    if not commit:
        return res
    top = _git(prj.root, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        res["git"] = "not a git repository: images written locally only"
        return res
    root = Path(top.stdout.strip())
    rel = prj.root.relative_to(root)
    files = ["images", "README.md", "design.json", "layout.json"] + \
        [p.name for p in prj.root.iterdir() if p.suffix in (".kicad_pcb", ".kicad_sch", ".kicad_pro")]
    gz = list((prj.root / "out").glob("*-gerbers.zip"))
    files += [str(Path("out") / g.name) for g in gz]
    paths = [str(rel / f) for f in files if (prj.root / f).exists()]
    _git(root, "add", "--", *paths)
    msg = message or f"{d.name}: board images and review ({review['grade']}, DRC {m['drc']['errors']}/{m['drc']['warnings']})"
    c = _git(root, "commit", "-m", msg, "--", *paths)      # only this board's files, never unrelated staged work
    res["commit"] = (c.stdout + c.stderr).strip().splitlines()[:2]
    if push:
        p = _git(root, "-c", "core.sshCommand=ssh -o ServerAliveInterval=10 -o ConnectTimeout=15", "push", timeout=300)
        res["push"] = (p.stdout + p.stderr).strip()[-400:] or "ok"
    return res
