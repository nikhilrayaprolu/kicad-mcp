"""Bridge to the C++ 'human-like' router (router_cpp/hroute.cpp).

Builds the routing problem from the IR/layout (pads as terminals, keepouts, plane layers excluded,
regional planes reserved for their net, fixed fan-out copper, differential pairs), runs the router,
and imports tracks/vias.  Several cost profiles / placements can be routed concurrently.
"""
from __future__ import annotations

import fnmatch
import math
import os
import subprocess
import time
from collections import defaultdict
from pathlib import Path

from . import board as brd
from . import libs
from . import sexpr as sx
from .ir import Project, Track, Via

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "router_cpp" / "hroute"
SRC = ROOT / "router_cpp" / "hroute.cpp"

PROFILES = {
    # name: via_cost, turn45, turn90, wrongdir, prox, pair_via_cost, edge_clr, max_rip
    "human":    (30, 1.5, 5.0, 0.35, 0.15, 120, 0.3, 4),
    "fewvias":  (60, 1.5, 5.0, 0.25, 0.10, 200, 0.3, 4),
    "tight":    (20, 1.0, 3.0, 0.20, 0.05, 80, 0.3, 6),
    "orthogonal": (30, 3.0, 4.0, 0.60, 0.15, 120, 0.3, 4),
}


def ensure_built() -> Path:
    if not BIN.exists() or BIN.stat().st_mtime < SRC.stat().st_mtime:
        subprocess.run(["g++", "-O3", "-march=native", "-std=c++17", "-o", str(BIN), str(SRC)], check=True)
    return BIN


def _forbid_rects(W: float, H: float, windows: list, step: float = 0.25) -> list:
    """Rectangles covering the board minus `windows` (row scan, merged runs)."""
    rects = []
    y = 0.0
    while y < H:
        y2 = min(H, y + step)
        x, run = 0.0, None
        while x < W:
            x2 = min(W, x + step)
            free = any(wx1 < x2 and wx2 > x and wy1 < y2 and wy2 > y for wx1, wy1, wx2, wy2 in windows)
            if not free:
                run = [run[0], x2] if run else [x, x2]
            elif run:
                rects.append((run[0], y, run[1], y2)); run = None
            x = x2
        if run:
            rects.append((run[0], y, run[1], y2))
        y = y2
    return rects


def write_problem(prj: Project, path: Path, profile: str = "human", pitch: float = 0.05,
                  only_nets: list[str] | None = None, protect_all: bool = False,
                  forbid: dict | None = None) -> dict:
    """forbid = {layer: [window rects]}: that layer is closed to the routed nets except inside the windows
    (used to make both legs of a pair change layer at the same place)."""
    d, L = prj.design, prj.layout
    cul = brd.copper_layers(d.board.layers)
    full_planes = {c.layer: c.net for c in d.constraints if c.kind == "plane" and not c.rect and c.layer in cul}
    plane_nets = set(full_planes.values())
    nets = sorted(d.nets())
    nid = {n: i for i, n in enumerate(nets)}
    W, H = d.board.width, d.board.height
    nx, ny = int(math.ceil(W / pitch)) + 1, int(math.ceil(H / pitch)) + 1
    out = [f"GRID {pitch} 0 0 {nx} {ny} {len(cul)}"]
    # layer roles: plane layers excluded; outer layers free direction; inner signal horizontal; bottom vertical
    for i, l in enumerate(cul):
        if l in full_planes:
            out.append(f"LAYER {i} 0 999")
        elif l == "F.Cu":
            out.append(f"LAYER {i} 0 1.0")
        elif l == "B.Cu":
            out.append(f"LAYER {i} 2 1.15")
        else:
            out.append(f"LAYER {i} 1 1.1")
    out.append("COST " + " ".join(str(v) for v in PROFILES[profile]))
    pair_nets = {}
    for c in d.constraints:
        if c.kind == "diffpair" and c.p in nid and c.n in nid:
            pair_nets[c.p] = c.n
    pn_set = set(pair_nets) | set(pair_nets.values())
    prio = {}
    for n in nets:
        nc = d.netclass_of(n)
        prio[n] = 4 if nc.name == "PWR_HI" else 3 if nc.name == "PWR" else 2 if nc.diff_gap else 1
        if n.endswith(("_XIN", "_XOUT", "_XOUT_R")):
            prio[n] = 5   # crystal: short and first, like a human would
    pads = brd.abs_pads(prj)
    by_net = defaultdict(list)
    for p in pads:
        if p.net:
            by_net[p.net].append(p)
    for n in nets:
        nc = d.netclass_of(n)
        w = (nc.diff_width or nc.track) if n in pn_set else nc.track
        route = n not in plane_nets and len(by_net[n]) >= 2
        if only_nets is not None and not any(fnmatch.fnmatchcase(n, g) for g in only_nets):
            route = False          # critical-first mode: only these nets (pairs are always attempted)
        out.append(f"NET {nid[n]} {n.replace(' ', '_')} {w / 2:.4f} {nc.clearance:.4f} {nc.via_d / 2:.4f} "
                   f"{nc.via_drill:.4f} {prio[n]} {int(route)}")
    # pins (terminals) + pad obstacles
    lidx = {l: i for i, l in enumerate(cul)}
    for p in pads:
        mask = 0
        for l in p.layers:
            if l in lidx:
                mask |= 1 << lidx[l]
        x1, y1, x2, y2 = p.x - p.hx, p.y - p.hy, p.x + p.hx, p.y + p.hy
        net = nid.get(p.net, -2)
        if p.net:
            out.append(f"PIN {net} {mask} {x1:.4f} {y1:.4f} {x2:.4f} {y2:.4f}")
        if len(p.layers) >= len(cul):
            out.append(f"RECT -1 {x1:.4f} {y1:.4f} {x2:.4f} {y2:.4f} {net}")
        else:
            for l in p.layers:
                if l in lidx:
                    out.append(f"RECT {lidx[l]} {x1:.4f} {y1:.4f} {x2:.4f} {y2:.4f} {net}")
        if p.drill:
            out.append(f"CIRC -1 {p.x:.4f} {p.y:.4f} {p.drill / 2 + 0.1:.4f} {net}")
    for mh in d.board.mounting_holes:
        r = (mh[2] if len(mh) > 2 else d.board.mh_drill) / 2 + 0.6
        out.append(f"CIRC -1 {mh[0]} {mh[1]} {r} -2")
    for c in d.constraints:
        if c.kind == "keepout" and c.rect:
            for l in (c.layers or cul):
                if l in lidx:
                    out.append(f"RECT {lidx[l]} {c.rect[0]} {c.rect[1]} {c.rect[2]} {c.rect[3]} -2")
        if c.kind == "plane" and c.rect and c.layer in lidx and c.net in nid:   # regional plane reserved for its net
            out.append(f"RECT {lidx[c.layer]} {c.rect[0]} {c.rect[1]} {c.rect[2]} {c.rect[3]} {nid[c.net]}")
    # footprint keepout zones
    for ref, comp in d.components.items():
        pl = L.placement.get(ref)
        if pl is None:
            continue
        for z in sx.find_all(libs.get_footprint_side(comp.footprint, pl.side).node, "zone"):
            ko = sx.find(z, "keepout")
            if ko is None:
                continue
            pts = [libs.rot(float(xy[1]), float(xy[2]), pl.rot) for xy in
                   sx.find_all(sx.find(sx.find(z, "polygon") or [], "pts") or [], "xy")]
            if not pts:
                continue
            xs, ys = [pl.x + a for a, _ in pts], [pl.y + b for _, b in pts]
            lays = sx.find(z, "layers") or sx.find(z, "layer")
            for ln in ([str(x) for x in lays[1:]] if lays else ["F.Cu"]):
                for l in (cul if ln in ("*.Cu", "F&B.Cu") else [ln]):
                    if l in lidx:
                        out.append(f"RECT {lidx[l]} {min(xs)} {min(ys)} {max(xs)} {max(ys)} -2")
    # fixed copper (fan-out stubs/vias, locked tracks)
    for t in L.tracks:
        if (t.locked or protect_all) and t.layer in lidx:
            pts = " ".join(f"{x:.4f} {y:.4f}" for x, y in t.pts)
            out.append(f"FSEG {nid.get(t.net, -2)} {lidx[t.layer]} {t.width} {len(t.pts)} {pts}")
    for v in L.vias:
        if v.locked or protect_all:
            out.append(f"FVIA {nid.get(v.net, -2)} {v.x} {v.y} {v.d} {v.drill}")
    # pairs: orientation hints (component centres at each end) in the pin order the router sees
    pin_order = defaultdict(list)
    for p in pads:
        if p.net:
            pin_order[p.net].append(p)
    def _sel(n):
        return only_nets is None or any(fnmatch.fnmatchcase(n, g) for g in only_nets)
    has_cu = {t.net for t in L.tracks} | {v.net for v in L.vias}
    for pnet, nnet in pair_nets.items():
        # incremental mode: couple a pair only when both legs are being (re)routed and carry no copper yet
        if protect_all and not (_sel(pnet) and _sel(nnet) and pnet not in has_cu and nnet not in has_cu):
            continue
        pp = pin_order[pnet]
        if len(pp) != 2 or len(pin_order[nnet]) != 2:
            continue
        nc = d.netclass_of(pnet)
        gap = nc.diff_gap or nc.clearance
        c0, c1 = L.placement[pp[0].ref], L.placement[pp[1].ref]
        out.append(f"PAIR {nid[pnet]} {nid[nnet]} {gap} {c0.x} {c0.y} {c1.x} {c1.y}")
    for lay, wins in (forbid or {}).items():
        if lay in lidx:
            for x1, y1, x2, y2 in _forbid_rects(d.board.width, d.board.height, wins):
                out.append(f"RECT {lidx[lay]} {x1:.4f} {y1:.4f} {x2:.4f} {y2:.4f} -2")
    path.write_text("\n".join(out) + "\n")
    return {"nets": nets, "nid": nid, "layers": cul, "routed_nets": sum(1 for n in nets if n not in plane_nets and len(by_net[n]) >= 2)}


def read_result(path: Path, info: dict) -> tuple[list[Track], list[Via], list[str], list[str]]:
    nets, cul = info["nets"], info["layers"]
    tracks, vias, failed, pairs = [], [], [], []
    for line in path.read_text().splitlines():
        f = line.split()
        if not f:
            continue
        if f[0] == "SEG":
            net, layer, w, k = int(f[1]), int(f[2]), float(f[3]), int(f[4])
            pts = [[round(float(f[5 + 2 * i]), 4), round(float(f[6 + 2 * i]), 4)] for i in range(k)]
            tracks.append(Track(net=nets[net], layer=cul[layer], pts=pts, width=round(w, 4)))
        elif f[0] == "VIA":
            vias.append(Via(net=nets[int(f[1])], x=round(float(f[2]), 4), y=round(float(f[3]), 4),
                            d=round(float(f[4]), 4), drill=round(float(f[5]), 4)))
        elif f[0] == "FAIL":
            failed.append(nets[int(f[1])])
        elif f[0] == "PAIROK":
            pairs.append(nets[int(f[1])])
    return tracks, vias, failed, pairs


def run(prj: Project, profile: str = "human", workdir: Path | None = None, timeout_s: int = 1800,
        only_nets: list[str] | None = None, protect_all: bool = False, forbid: dict | None = None) -> dict:
    ensure_built()
    work = workdir or (prj.root / "out" / "hroute" / profile)
    work.mkdir(parents=True, exist_ok=True)
    pin, pout = work / "problem.txt", work / "result.txt"
    info = write_problem(prj, pin, profile, only_nets=only_nets, protect_all=protect_all, forbid=forbid)
    t0 = time.time()
    logf = work / "hroute.log"
    if pout.exists():
        pout.unlink()
    with open(logf, "w") as lf:          # stream progress to the log (visible while running)
        try:
            rc = subprocess.run([str(BIN), str(pin), str(pout)], stdout=lf, stderr=lf, timeout=timeout_s).returncode
        except subprocess.TimeoutExpired:
            rc = -9
    log = logf.read_text(errors="ignore")
    if rc != 0 or not pout.exists():
        return {"ok": False, "error": log[-800:]}
    tracks, vias, failed, pairs = read_result(pout, info)
    return {"ok": True, "tracks": tracks, "vias": vias, "failed": failed, "pairs_coupled": sorted(set(pairs)),
            "seconds": round(time.time() - t0, 1), "log": log[-1500:], "routed_nets": info["routed_nets"]}


def apply(prj: Project, res: dict) -> None:
    L = prj.layout
    L.tracks = [t for t in L.tracks if t.locked] + res["tracks"]
    L.vias = [v for v in L.vias if v.locked] + res["vias"]


def _pair_skew(tracks: list[Track], pairs: dict[str, str]) -> dict:
    ln = defaultdict(float)
    for t in tracks:
        for a, b in zip(t.pts, t.pts[1:]):
            ln[t.net] += math.hypot(a[0] - b[0], a[1] - b[1])
    return {f"{p}/{n}": round(abs(ln[p] - ln[n]), 2) for p, n in pairs.items() if ln[p] and ln[n]}


def route_portfolio(prj: Project, candidates: list[dict], profiles: list[str], fanout_planes: bool = True,
                    timeout_s: int = 1800) -> dict:
    """Route (placement x cost-profile) combinations concurrently with the C++ router; keep the best by
    (failed nets, placement score, vias, length)."""
    import copy
    from concurrent.futures import ThreadPoolExecutor
    from . import router as frouter
    from .ir import Placement
    ensure_built()
    d = prj.design
    planes = sorted({c.net for c in d.constraints if c.kind == "plane"})
    pairs = {c.p: c.n for c in d.constraints if c.kind == "diffpair" and c.p and c.n}
    jobs = [(ci, c, pf) for ci, c in enumerate(candidates) for pf in profiles]

    def one(job):
        ci, cand, pf = job
        sub = copy.copy(prj)
        sub.layout = prj.layout.model_copy(deep=True)
        if cand is not None:
            sub.layout.placement = {k: Placement(**v) for k, v in cand["placement"].items()}
        sub.layout.tracks = [t for t in sub.layout.tracks if False]
        sub.layout.vias = []
        fo = frouter.plane_fanout(sub, planes) if fanout_planes and planes else {"added": 0}
        res = run(sub, pf, workdir=prj.root / "out" / "hroute" / f"c{ci:02d}_{pf}", timeout_s=timeout_s)
        if not res.get("ok"):
            return {"cand": ci, "profile": pf, "ok": False, "error": res.get("error", "")[-300:]}
        apply(sub, res)
        nv = len(sub.layout.vias)
        ln = sum(math.hypot(a[0] - b[0], a[1] - b[1]) for t in sub.layout.tracks for a, b in zip(t.pts, t.pts[1:]))
        return {"cand": ci, "profile": pf, "ok": True, "failed": res["failed"], "n_failed": len(res["failed"]),
                "pairs_coupled": len(res["pairs_coupled"]) // 2, "vias": nv, "len_mm": round(ln, 1),
                "seconds": res["seconds"], "placement_score": (cand or {}).get("score", 0.0),
                "skew": _pair_skew(sub.layout.tracks, pairs), "_layout": sub.layout, "fanout": fo.get("added")}

    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        results = list(ex.map(one, jobs))
    ok = [r for r in results if r.get("ok")]
    table = [{k: v for k, v in r.items() if k not in ("_layout", "failed")} for r in results]
    if not ok:
        return {"ok": False, "runs": table}
    for r in ok:
        r["cost"] = round(300 * r["n_failed"] + r["placement_score"] + 0.5 * r["vias"] + 0.05 * r["len_mm"]
                          - 50 * r["pairs_coupled"], 1)
    best = min(ok, key=lambda r: r["cost"])
    prj.layout = best["_layout"]
    prj.layout.history.append({"op": "hroute_portfolio", "best": [best["cand"], best["profile"]], "n": len(jobs)})
    return {"ok": True, "best": {"cand": best["cand"], "profile": best["profile"]}, "failed": best["failed"],
            "pairs_coupled": best["pairs_coupled"], "skew_mm": best["skew"], "vias": best["vias"],
            "len_mm": best["len_mm"], "runs": [{k: t.get(k) for k in ("cand", "profile", "n_failed", "pairs_coupled",
                                                                   "vias", "len_mm", "seconds", "cost")} for t in table]}



def route_critical_then_complete(prj: Project, critical: list[str], profile: str = "human", passes: int = 100,
                                 timeout_s: int = 1200, workdir: Path | None = None, threads: int = 0) -> dict:
    """Critical-first hybrid: hroute lays out the critical nets (+ coupled diff pairs) human-style, they are
    locked, then Freerouting routes everything else around them."""
    from . import router as frouter
    d = prj.design
    planes = sorted({c.net for c in d.constraints if c.kind == "plane"})
    prj.layout.tracks, prj.layout.vias = [], []
    fo = frouter.plane_fanout(prj, planes) if planes else {"added": 0}
    wd = workdir or (prj.root / "out" / "hroute" / "critical")
    res = run(prj, profile, workdir=wd, timeout_s=timeout_s, only_nets=critical)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error")}
    for t in res["tracks"]:
        t.locked = True
    for v in res["vias"]:
        v.locked = True
    apply(prj, res)
    n_crit_tracks = len(res["tracks"])
    fin = frouter.complete(prj, passes=passes, timeout_s=timeout_s, workdir=wd / "fr", threads=threads)
    pairs = {c.p: c.n for c in d.constraints if c.kind == "diffpair" and c.p and c.n}
    return {"ok": True, "fanout": fo.get("added"), "hroute_tracks": n_crit_tracks, "hroute_vias": len(res["vias"]),
            "hroute_failed": res["failed"], "pairs_coupled": res["pairs_coupled"],
            "freerouting": {k: fin.get(k) for k in ("new_tracks", "new_vias", "fr_unrouted", "fr_violations", "seconds")},
            "skew_mm": _pair_skew(prj.layout.tracks, pairs)}



def hybrid_portfolio(prj: Project, candidates: list[dict], profiles: list[str], critical: list[str],
                     passes: int = 100, timeout_s: int = 1200) -> dict:
    """Critical-first hybrid on (placement x profile) combinations, all concurrently (cores split between the
    Freerouting optimizer thread pools); keep the best by unrouted, violations, coupled pairs, placement, vias."""
    import copy
    from concurrent.futures import ThreadPoolExecutor
    from .ir import Placement
    jobs = [(ci, c, pf) for ci, c in enumerate(candidates) for pf in profiles]
    thr = max(1, ((os.cpu_count() or 2) - 2) // max(1, len(jobs)))

    def one(job):
        ci, cand, pf = job
        sub = copy.copy(prj)
        sub.layout = prj.layout.model_copy(deep=True)
        if cand is not None:
            sub.layout.placement = {k: Placement(**v) for k, v in cand["placement"].items()}
        try:
            r = route_critical_then_complete(sub, critical, pf, passes=passes, timeout_s=timeout_s,
                                             workdir=prj.root / "out" / "hybrid" / f"c{ci:02d}_{pf}", threads=thr)
        except Exception as e:  # noqa: BLE001
            return {"cand": ci, "profile": pf, "ok": False, "error": str(e)[:200]}
        r.update(cand=ci, profile=pf, placement_score=(cand or {}).get("score", 0.0), _layout=sub.layout,
                 vias=len(sub.layout.vias))
        return r

    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        results = list(ex.map(one, jobs))
    ok = [r for r in results if r.get("ok") and r.get("freerouting", {}).get("fr_unrouted") is not None]
    for r in ok:
        fr = r["freerouting"]
        r["cost"] = round(300 * (fr["fr_unrouted"] or 0) + 100 * (fr["fr_violations"] or 0) + r["placement_score"]
                          + 0.5 * r["vias"] - 80 * (len(r["pairs_coupled"]) // 2) + 50 * len(r["hroute_failed"]), 1)
    table = [{"cand": r.get("cand"), "profile": r.get("profile"), "pairs": len(r.get("pairs_coupled", [])) // 2,
              "hroute_failed": len(r.get("hroute_failed", [])), "fr_unrouted": (r.get("freerouting") or {}).get("fr_unrouted"),
              "fr_viol": (r.get("freerouting") or {}).get("fr_violations"), "vias": r.get("vias"), "cost": r.get("cost")}
             for r in results]
    if not ok:
        return {"ok": False, "runs": table}
    best = min(ok, key=lambda r: r["cost"])
    prj.layout = best["_layout"]
    prj.layout.history.append({"op": "hybrid_portfolio", "best": [best["cand"], best["profile"]], "n": len(jobs)})
    return {"ok": True, "best": {"cand": best["cand"], "profile": best["profile"]},
            "pairs_coupled": best["pairs_coupled"], "hroute_failed": best["hroute_failed"],
            "freerouting": best["freerouting"], "skew_mm": best["skew_mm"], "jobs": len(jobs),
            "threads_per_job": thr, "runs": sorted(table, key=lambda t: (t["cost"] is None, t["cost"] or 0))[:12]}



def complete_nets(prj: Project, nets: list[str], profile: str | list[str] = "human", timeout_s: int = 900) -> dict:
    """Incremental: route only `nets` with hroute around ALL existing copper (kept as fixed obstacles).
    Several profiles run in parallel; the winner is: everything routed > diff pairs with equal via counts
    on both legs > coupled pairs > fewest vias > shortest."""
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor
    profs = [profile] if isinstance(profile, str) else list(profile)
    if len(profs) == 1 and profs[0] == "human":
        profs = list(PROFILES)          # default: try them all, they cost one core each
    pairs = [(c.p, c.n) for c in prj.design.constraints if c.kind == "diffpair" and c.p and c.n
             and any(fnmatch.fnmatchcase(c.p, g) for g in nets) and any(fnmatch.fnmatchcase(c.n, g) for g in nets)]

    def one(arg):
        pf, forbid, tag = arg
        return f"{pf}{tag}", run(prj, pf, workdir=prj.root / "out" / "hroute" / f"complete_{pf}{tag}",
                                timeout_s=timeout_s, only_nets=nets, protect_all=True, forbid=forbid)

    # layer-forced variants for pairs: close one outer layer except a window at each SMD pad, so both legs
    # via to the same layer right at the breakout (how a human crosses one pair under another)
    variants = [(pf, None, "") for pf in profs]
    if pairs:
        pads = [q for q in brd.abs_pads(prj) if any(q.net in pn for pn in pairs)]
        for lay in ("F.Cu", "B.Cu"):
            for r in (1.3, 2.2, 3.2):
                wins = [(q.x - r, q.y - r, q.x + r, q.y + r) for q in pads if lay in q.layers and not q.drill]
                variants += [(pf, {lay: wins}, f"_no{lay[0]}{r}") for pf in ("human", "tight")]
    with ThreadPoolExecutor(len(variants)) as ex:
        results = [r for r in ex.map(one, variants) if r[1].get("ok")]
    if not results:
        return {"ok": False, "error": "no profile produced a result"}

    def score(item):
        pf, r = item
        vc = Counter(v.net for v in r["vias"])
        asym = sum(1 for p, n in pairs if vc[p] != vc[n])
        coupled = len(r.get("pairs_coupled", [])) // 2
        ln = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for t in r["tracks"] for a, b in zip(t.pts, t.pts[1:]))
        return (len(r["failed"]), asym, -coupled, len(r["vias"]), ln)

    pf, res = min(results, key=score)
    vc = Counter(v.net for v in res["vias"])
    prj.layout.tracks += res["tracks"]
    prj.layout.vias += res["vias"]
    return {"ok": True, "profile": pf, "tracks": len(res["tracks"]), "vias": len(res["vias"]), "failed": res["failed"],
            "pairs": {f"{p}/{n}": {"vias": f"{vc[p]}/{vc[n]}", "coupled": p in res.get("pairs_coupled", [])}
                      for p, n in pairs},
            "tried": {p: list(score((p, r))[:4]) for p, r in results}}
