"""Fabrication outputs via kicad-cli (Gerber X2, Excellon, CPL, BOM, PDFs, STEP)."""
from __future__ import annotations

import csv
import subprocess
import zipfile
from collections import defaultdict
from pathlib import Path

from . import board as brd
from .ir import Project


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return r.returncode, (r.stdout + r.stderr)[-400:]


def export_fab(prj: Project, step: bool = False) -> dict:
    d = prj.design
    pcb = str(prj.path(".kicad_pcb"))
    fab = prj.root / "out" / "fab"
    fab.mkdir(parents=True, exist_ok=True)
    cu = brd.copper_layers(d.board.layers)
    layers = cu + ["F.Paste", "B.Paste", "F.SilkS", "B.SilkS", "F.Mask", "B.Mask", "Edge.Cuts"]
    res = {}
    res["gerbers"] = _run(["kicad-cli", "pcb", "export", "gerbers", "--check-zones", "--subtract-soldermask",
                           "-l", ",".join(layers), "-o", str(fab) + "/", pcb])
    res["drill"] = _run(["kicad-cli", "pcb", "export", "drill", "--format", "excellon", "--excellon-separate-th",
                         "--generate-map", "--map-format", "pdf", "-o", str(fab) + "/", pcb])
    res["pos"] = _run(["kicad-cli", "pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both",
                       "--exclude-dnp", "-o", str(fab / f"{d.name}-cpl.csv"), pcb])
    res["ipc356"] = _run(["kicad-cli", "pcb", "export", "ipcd356", "-o", str(fab / f"{d.name}.d356"), pcb])
    res["pdf"] = _run(["kicad-cli", "pcb", "export", "pdf", "--mode-single", "-l", ",".join(cu + ["Edge.Cuts"]),
                       "-o", str(prj.root / "out" / f"{d.name}-layers.pdf"), pcb])
    res["sch_pdf"] = _run(["kicad-cli", "sch", "export", "pdf", "-o", str(prj.root / "out" / f"{d.name}-schematic.pdf"),
                           str(prj.path(".kicad_sch"))])
    if step:
        res["step"] = _run(["kicad-cli", "pcb", "export", "step", "--subst-models", "-f", "-o",
                            str(prj.root / "out" / f"{d.name}.step"), pcb])
    # BOM grouped by value+footprint+mpn (JLC style)
    groups = defaultdict(list)
    for c in d.components.values():
        if c.dnp:
            continue
        groups[(c.value, c.footprint.split(":")[-1], c.mpn, c.lcsc)].append(c.ref)
    bom = fab / f"{d.name}-bom.csv"
    with open(bom, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Comment", "Designator", "Footprint", "Quantity", "MPN", "LCSC Part #"])
        for (val, fp, mpn, lcsc), refs in sorted(groups.items(), key=lambda kv: kv[1][0]):
            w.writerow([val, ",".join(sorted(refs)), fp, len(refs), mpn, lcsc])
    gz = prj.root / "out" / f"{d.name}-gerbers.zip"
    with zipfile.ZipFile(gz, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(fab.iterdir()):
            if p.suffix.lower() in (".gbr", ".gtl", ".gbl", ".gto", ".gbo", ".gts", ".gbs", ".gtp", ".gbp", ".gm1",
                                    ".g1", ".g2", ".drl", ".gbrjob") or p.name.endswith(".gbr"):
                z.write(p, p.name)
    files = sorted(p.name for p in fab.iterdir())
    failed = {k: v[1] for k, v in res.items() if v[0] != 0}
    return {"dir": str(fab), "zip": str(gz), "files": len(files), "bom_lines": len(groups), "failed": failed}
