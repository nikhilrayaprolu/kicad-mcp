"""Verification: DRC via kicad-cli, condensed diagnostics, and knowledge-based design review."""
from __future__ import annotations

import fnmatch
import json
import math
import subprocess
from collections import Counter, defaultdict

from . import board as brd
from .ir import Project


def run_drc(prj: Project, refill: bool = True, save: bool = True, parity: bool = False) -> dict:
    out = prj.root / "out" / "drc.json"
    out.parent.mkdir(exist_ok=True)
    cmd = ["kicad-cli", "pcb", "drc", "--format", "json", "--severity-all", "-o", str(out)]
    if refill:
        cmd.append("--refill-zones")
        if save:
            cmd.append("--save-board")
    if parity:
        cmd.append("--schematic-parity")
    cmd.append(str(prj.path(".kicad_pcb")))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if not out.exists():
        return {"error": (r.stdout + r.stderr)[-800:]}
    data = json.loads(out.read_text())
    return summarize_drc(data, str(out))


def summarize_drc(data: dict, path: str) -> dict:
    viol = data.get("violations", [])
    unc = data.get("unconnected_items", [])
    par = data.get("schematic_parity", [])
    by_type: Counter = Counter()
    sev: Counter = Counter()
    examples: dict[str, list] = defaultdict(list)
    for v in viol:
        t = v.get("type")
        by_type[t] += 1
        sev[v.get("severity")] += 1
        if len(examples[t]) < 3:
            it = v.get("items", [])
            pos = it[0].get("pos", {}) if it else {}
            examples[t].append({"desc": v.get("description"), "at": [round(pos.get("x", 0), 2), round(pos.get("y", 0), 2)],
                                "items": [i.get("description", "")[:90] for i in it[:2]]})
    unc_nets: Counter = Counter()
    for u in unc:
        for it in u.get("items", [])[:1]:
            desc = it.get("description", "")
            net = desc.split("[")[-1].split("]")[0] if "[" in desc else "?"
            unc_nets[net] += 1
    return {
        "errors": sev.get("error", 0), "warnings": sev.get("warning", 0),
        "unconnected": len(unc), "parity": len(par),
        "by_type": dict(by_type.most_common()),
        "examples": dict(examples),
        "unconnected_by_net": dict(unc_nets.most_common(25)),
        "report": path,
    }


# ------------------------------------------------------------------ design review
def design_review(prj: Project) -> dict:
    """Engineering checks that DRC can't do: constraint satisfaction, pair skew, power widths."""
    d, L = prj.design, prj.layout
    pads = brd.abs_pads(prj)
    padmap = {(p.ref, p.number): p for p in pads}
    findings: list[dict] = []

    def pin_pos(spec: str):
        if "." in spec:
            ref, pin = spec.split(".", 1)
            c = d.components.get(ref)
            if c is None:
                return None
            # allow pin name
            if (ref, pin) not in padmap:
                from . import libs
                sym = libs.get_symbol(c.symbol)
                nums = [p.number for p in sym.pins if p.name == pin]
                if not nums:
                    return None
                ps = [padmap[(ref, n)] for n in nums if (ref, n) in padmap]
                return ps
            return [padmap[(ref, pin)]]
        pl = L.placement.get(spec)
        return [type("P", (), {"x": pl.x, "y": pl.y})()] if pl else None

    # near constraints
    for c in d.constraints:
        if c.kind == "near" and c.ref in L.placement and c.target:
            tgt = pin_pos(c.target)
            if not tgt:
                continue
            comp = d.components[c.ref]
            # distance from the component's pad sharing a net with target (or its center)
            mine = [p for p in pads if p.ref == c.ref]
            tnets = {getattr(t, "net", None) for t in tgt}
            shared = [p for p in mine if p.net and p.net in tnets] or mine
            dist = min(math.hypot(a.x - b.x, a.y - b.y) for a in shared for b in tgt) if shared else 1e9
            if c.max_mm and dist > c.max_mm:
                findings.append({"sev": "warn", "rule": "near", "msg": f"{c.ref} is {dist:.1f} mm from {c.target} (max {c.max_mm})"})
    # routed lengths per net
    length: dict[str, float] = defaultdict(float)
    for t in L.tracks:
        for a, b in zip(t.pts, t.pts[1:]):
            length[t.net] += math.hypot(a[0] - b[0], a[1] - b[1])
    vias_per_net = Counter(v.net for v in L.vias)
    for c in d.constraints:
        if c.kind == "diffpair" and c.p and c.n:
            lp, ln = length.get(c.p, 0), length.get(c.n, 0)
            npads = Counter(p.net for p in pads if p.net in (c.p, c.n))
            if (npads[c.p] != 2 or npads[c.n] != 2) and lp and ln:
                findings.append({"sev": "info", "rule": "diffpair_multipin",
                                 "msg": f"{c.p}/{c.n}: multi-pin nets (stubs/branches); total copper differs "
                                        f"{abs(lp - ln):.2f} mm (not a path skew)"})
                continue
            if lp and ln:
                skew = abs(lp - ln)
                if skew > (c.tol_mm or 1.0):
                    findings.append({"sev": "warn", "rule": "diffpair_skew",
                                     "msg": f"{c.p}/{c.n} length {lp:.1f}/{ln:.1f} mm skew {skew:.2f} mm"})
                if vias_per_net[c.p] != vias_per_net[c.n]:
                    findings.append({"sev": "info", "rule": "diffpair_vias",
                                     "msg": f"{c.p}/{c.n} via count {vias_per_net[c.p]}/{vias_per_net[c.n]}"})
        if c.kind == "length_match" and c.nets:
            ls = {n: length.get(n, 0) for n in c.nets}
            if ls and max(ls.values()) - min(ls.values()) > (c.tol_mm or 2.0):
                findings.append({"sev": "warn", "rule": "length_match", "msg": f"{ls}"})
    # power current vs track width (IPC-2221 external, 10C rise, 1oz)
    for nc in d.netclasses.values():
        if nc.current_a > 0:
            need = ipc2221_width(nc.current_a)
            if nc.track + 1e-9 < need:
                findings.append({"sev": "warn", "rule": "current",
                                 "msg": f"netclass {nc.name}: {nc.current_a} A needs ~{need:.2f} mm, have {nc.track}"})
            thin = defaultdict(float)     # ignore short fan-out stubs / neck-downs (< 2 mm per net)
            for t in L.tracks:
                if any(fnmatch.fnmatchcase(t.net, p) for p in nc.nets) and t.width + 1e-9 < need * 0.7:
                    thin[t.net] += sum(math.hypot(a[0] - b[0], a[1] - b[1]) for a, b in zip(t.pts, t.pts[1:]))
            for net_, ln_ in thin.items():
                if ln_ > 2.0:
                    findings.append({"sev": "warn", "rule": "current_track",
                                     "msg": f"{net_}: {ln_:.1f} mm of track narrower than {need * 0.7:.2f} mm"})
    # unconnected power pins (IR level)
    from . import libs
    for c in d.components.values():
        sym = libs.get_symbol(c.symbol)
        for p in sym.pins:
            if p.etype == "power_in" and not c.pins.get(p.number) and p.number not in c.nc:
                findings.append({"sev": "error", "rule": "power_pin_nc", "msg": f"{c.ref}.{p.number} {p.name} unconnected"})
    # escape feasibility: a netclass must fit between adjacent pins of every fine-pitch part it touches
    from . import libs as _libs
    seen = set()
    for c in d.components.values():
        fp = _libs.get_footprint(c.footprint)
        smd = [q for q in fp.pads if q.kind == "smd" and q.number]
        if len(smd) < 8:
            continue
        # per-pad neighbour pitch within aligned rows (same orientation)
        pitch_of, padw_of = {}, {}
        for q in smd:
            best = None
            for r_ in smd:
                if r_ is q or (abs(r_.w - q.w) > 1e-6 or abs(r_.h - q.h) > 1e-6):
                    continue
                if abs(r_.x - q.x) < 1e-3 or abs(r_.y - q.y) < 1e-3:
                    dd = math.hypot(r_.x - q.x, r_.y - q.y)
                    if dd > 0.05 and (best is None or dd < best):
                        best = dd
            if best:
                pitch_of[q.number] = best
                padw_of[q.number] = min(q.w, q.h)
        if not pitch_of:
            continue
        for pn, net in c.pins.items():
            if pn not in pitch_of:
                continue
            nc = d.netclass_of(net)
            pitch, pw = pitch_of[pn], padw_of[pn]
            need = max(nc.track + nc.clearance, nc.track / 2 + pw / 2 + nc.clearance)
            tn = nc.track * 0.75          # what the router can neck down to at the pin
            need_neck = max(tn + nc.clearance, tn / 2 + pw / 2 + nc.clearance)
            if need > pitch - 0.005 and (c.ref, nc.name) not in seen:
                seen.add((c.ref, nc.name))
                findings.append({"sev": "error" if need_neck > pitch - 0.005 else "warn", "rule": "escape",
                                 "msg": f"netclass {nc.name} (track {nc.track}, clearance {nc.clearance}) cannot escape "
                                        f"{c.ref} pin pitch {pitch:.2f} mm (needs {need:.2f}); e.g. net {net}"})
    # single-pin nets
    for net, nodes in d.nets().items():
        if len(nodes) == 1:
            findings.append({"sev": "info", "rule": "single_pin_net", "msg": f"{net}: only {nodes[0]}"})
    total_len = sum(length.values())
    return {"findings": findings[:60], "n_findings": len(findings),
            "stats": {"track_mm": round(total_len, 1), "vias": len(L.vias), "tracks": len(L.tracks)}}


def ipc2221_width(i_a: float, rise_c: float = 10, oz: float = 1.0, external: bool = True) -> float:
    k = 0.048 if external else 0.024
    area_mil2 = (i_a / (k * rise_c ** 0.44)) ** (1 / 0.725)
    width_mil = area_mil2 / (1.378 * oz)
    return width_mil * 0.0254
