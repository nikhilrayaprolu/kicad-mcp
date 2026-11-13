"""Live-mode backend: sync the IR layout with a *running* KiCad 10 PCB editor via the IPC API
(kicad-python / kipy).  KiCad 10 IPC requires the GUI (headless IPC is planned for KiCad 11),
so this module is optional: the default headless path writes files + uses kicad-cli.

Status: implemented against the kipy 0.8 API surface; NOT exercised in this repo's CI because
the build machine has no display / running KiCad GUI.
"""
from __future__ import annotations

from .ir import Placement, Project, Track, Via


def _connect():
    from kipy import KiCad
    k = KiCad(client_name="llm-pcb")
    return k, k.get_board()


def push(prj: Project, copper: bool = True) -> dict:
    """Move footprints in the open board to the IR placement; optionally replace tracks/vias."""
    from kipy.board_types import BoardLayer, Track as KTrack, Via as KVia
    from kipy.geometry import Angle, Vector2
    k, b = _connect()
    L = prj.layout
    fps = b.get_footprints()
    commit = b.begin_commit()
    moved = []
    for fp in fps:
        ref = fp.reference_field.text.value
        pl = L.placement.get(ref)
        if pl is None:
            continue
        fp.position = Vector2.from_xy_mm(pl.x, pl.y)
        fp.orientation = Angle.from_degrees(pl.rot)
        moved.append(fp)
    if moved:
        b.update_items(moved)
    created = 0
    if copper:
        b.remove_items(list(b.get_tracks()) + list(b.get_vias()))
        nets = {n.name: n for n in b.get_nets()}
        items = []
        for t in L.tracks:
            for a, c in zip(t.pts, t.pts[1:]):
                tr = KTrack()
                tr.start, tr.end = Vector2.from_xy_mm(*a), Vector2.from_xy_mm(*c)
                tr.width = int(t.width * 1e6)
                tr.layer = getattr(BoardLayer, "BL_" + t.layer.replace(".", "_"))
                if t.net in nets:
                    tr.net = nets[t.net]
                items.append(tr)
        for v in L.vias:
            kv = KVia()
            kv.position = Vector2.from_xy_mm(v.x, v.y)
            kv.diameter, kv.drill_diameter = int(v.d * 1e6), int(v.drill * 1e6)
            if v.net in nets:
                kv.net = nets[v.net]
            items.append(kv)
        if items:
            b.create_items(items)
            created = len(items)
    b.push_commit(commit, "llm-pcb sync")
    b.refill_zones()
    return {"moved": len(moved), "copper_items": created}


def pull(prj: Project) -> dict:
    """Read footprint positions + tracks/vias edited by a human in the GUI back into the IR."""
    k, b = _connect()
    L = prj.layout
    n = 0
    for fp in b.get_footprints():
        ref = fp.reference_field.text.value
        if ref in prj.design.components:
            L.placement[ref] = Placement(x=fp.position.x / 1e6, y=fp.position.y / 1e6,
                                         rot=fp.orientation.degrees % 360, locked=True)
            n += 1
    tracks = [Track(net=t.net.name, layer=str(t.layer), pts=[[t.start.x / 1e6, t.start.y / 1e6], [t.end.x / 1e6, t.end.y / 1e6]],
                    width=t.width / 1e6) for t in b.get_tracks()]
    vias = [Via(net=v.net.name, x=v.position.x / 1e6, y=v.position.y / 1e6, d=v.diameter / 1e6,
                drill=v.drill_diameter / 1e6) for v in b.get_vias()]
    L.tracks, L.vias = tracks, vias
    prj.save()
    return {"footprints": n, "tracks": len(tracks), "vias": len(vias)}
