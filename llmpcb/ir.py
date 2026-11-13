"""Design IR: the single source of truth the LLM edits.

design.json  = circuit (components, connectivity, netclasses, constraints, board spec)
layout.json  = physical state produced by engines (placement, tracks, vias, zones)
All KiCad files are *generated* from these two.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class Component(BaseModel):
    ref: str
    symbol: str                       # lib_id, e.g. "Device:R" or "llmpcb:RV1106G3"
    footprint: str                    # lib_id, e.g. "Resistor_SMD:R_0402_1005Metric"
    value: str = ""
    part: str = ""                    # part-db id (optional)
    mpn: str = ""
    lcsc: str = ""
    block: str = "main"               # functional block (schematic grouping + placement grouping)
    pins: dict[str, str] = Field(default_factory=dict)   # pin number -> net name
    nc: list[str] = Field(default_factory=list)           # pins intentionally unconnected
    fields: dict[str, str] = Field(default_factory=dict)
    dnp: bool = False
    note: str = ""


class NetClass(BaseModel):
    name: str
    track: float = 0.15
    clearance: float = 0.15
    via_d: float = 0.45
    via_drill: float = 0.2
    diff_width: float = 0.0
    diff_gap: float = 0.0
    nets: list[str] = Field(default_factory=list)          # exact names or glob patterns
    current_a: float = 0.0                                   # design current (for review)


class Constraint(BaseModel):
    """Placement / routing intent.  Interpretation lives in placer/router/checks.

    kinds:
      fixed   : ref, x, y, rot                        - pin a part
      edge    : ref, side(left|right|top|bottom), offset(mm along edge, optional), rot
      near    : ref, target ("U1" or "U1.pin"), max_mm  - e.g. decaps, crystal
      region  : block, rect [x1,y1,x2,y2]               - floorplan a block
      keepout : rect, layers                            - no parts / copper
      diffpair: p, n, impedance                          - route as pair (checked)
      length_match: nets[], tol_mm
      plane   : net, layer [, rect]                      - power/ground pour (rect = regional split plane)
      side    : ref (glob) or block, side(top|bottom|any) - which board side(s) a part may be placed on
    """
    kind: Literal["fixed", "edge", "near", "region", "keepout", "diffpair", "length_match", "plane", "side"]
    ref: Optional[str] = None
    target: Optional[str] = None
    block: Optional[str] = None
    side: Optional[str] = None
    x: Optional[float] = None
    y: Optional[float] = None
    rot: Optional[float] = None
    offset: Optional[float] = None
    max_mm: Optional[float] = None
    rect: Optional[list[float]] = None
    layers: Optional[list[str]] = None
    p: Optional[str] = None
    n: Optional[str] = None
    impedance: Optional[float] = None
    nets: Optional[list[str]] = None
    tol_mm: Optional[float] = None
    net: Optional[str] = None
    layer: Optional[str] = None
    note: str = ""


class BoardSpec(BaseModel):
    width: float = 50.0
    height: float = 40.0
    corner_r: float = 1.0
    layers: int = 4
    thickness: float = 1.6
    fab: str = "jlcpcb"               # rule preset
    mounting_holes: list[list[float]] = Field(default_factory=list)   # [[x,y,drill],...]
    mh_drill: float = 3.2
    title: str = ""
    rev: str = "A"
    silk_refs: Literal["all", "large"] = "all"   # "large": small passives' designators on the fab layer only


class Design(BaseModel):
    name: str
    description: str = ""
    requirements: list[str] = Field(default_factory=list)
    board: BoardSpec = Field(default_factory=BoardSpec)
    components: dict[str, Component] = Field(default_factory=dict)
    netclasses: dict[str, NetClass] = Field(default_factory=dict)
    constraints: list[Constraint] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    # ------------------------------------------------------------ connectivity
    def nets(self) -> dict[str, list[tuple[str, str]]]:
        out: dict[str, list[tuple[str, str]]] = {}
        for c in self.components.values():
            for pin, net in c.pins.items():
                if net:
                    out.setdefault(net, []).append((c.ref, pin))
        return dict(sorted(out.items()))

    def netclass_of(self, net: str) -> NetClass:
        from fnmatch import fnmatchcase
        for nc in self.netclasses.values():
            if nc.name == "Default":
                continue
            for pat in nc.nets:
                if fnmatchcase(net, pat):
                    return nc
        return self.netclasses.get("Default") or NetClass(name="Default")

    def next_ref(self, prefix: str) -> str:
        nums = [int(m.group(1)) for r in self.components
                if (m := re.fullmatch(re.escape(prefix) + r"(\d+)", r))]
        return f"{prefix}{max(nums, default=0) + 1}"


# ------------------------------------------------------------------ layout state
class Track(BaseModel):
    net: str
    layer: str
    pts: list[list[float]]        # polyline
    width: float
    locked: bool = False


class Via(BaseModel):
    net: str
    x: float
    y: float
    d: float = 0.45
    drill: float = 0.2
    locked: bool = False


class Zone(BaseModel):
    net: str
    layer: str
    poly: list[list[float]] = Field(default_factory=list)   # empty = board outline
    priority: int = 0
    clearance: float = 0.2
    min_width: float = 0.2
    thermal_gap: float = 0.3
    thermal_spoke: float = 0.3
    connect: Literal["thermal", "solid", "thru_hole_only"] = "thermal"


class Placement(BaseModel):
    x: float
    y: float
    rot: float = 0.0
    locked: bool = False
    side: Literal["top", "bottom"] = "top"


class Layout(BaseModel):
    placement: dict[str, Placement] = Field(default_factory=dict)
    tracks: list[Track] = Field(default_factory=list)
    vias: list[Via] = Field(default_factory=list)
    zones: list[Zone] = Field(default_factory=list)
    history: list[dict[str, Any]] = Field(default_factory=list)


# ------------------------------------------------------------------- project io
class Project:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.design = Design.model_validate_json((self.root / "design.json").read_text())
        lp = self.root / "layout.json"
        self.layout = Layout.model_validate_json(lp.read_text()) if lp.exists() else Layout()

    @classmethod
    def create(cls, root: Path, name: str, **kw) -> "Project":
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        d = Design(name=name, **kw)
        d.netclasses.setdefault("Default", NetClass(name="Default"))
        (root / "design.json").write_text(d.model_dump_json(indent=1))
        (root / "layout.json").write_text(Layout().model_dump_json(indent=1))
        return cls(root)

    def save(self) -> None:
        (self.root / "design.json").write_text(self.design.model_dump_json(indent=1, exclude_defaults=False))
        (self.root / "layout.json").write_text(self.layout.model_dump_json(indent=1))

    @property
    def name(self) -> str:
        return self.design.name

    def path(self, suffix: str) -> Path:
        return self.root / f"{self.design.name}{suffix}"

    def log(self, event: str, **data) -> None:
        with open(self.root / "history.jsonl", "a") as f:
            f.write(json.dumps({"event": event, **data}) + "\n")
