# llm-pcb architecture

## Design principle

> The LLM reasons about **engineering intent**; deterministic engines produce **geometry**;
> every tool returns **compact engineering state**, not raw dumps.

A conventional KiCad MCP server exposes hundreds of micro-operations (`add_track`,
`move_footprint`, …) and leaves the model to invent coordinates. llm-pcb exposes ~20
coarse tools whose arguments are *decisions* (which part, which net, which reference
circuit, which floorplan region, which pins must be close) and whose results are
*diagnostics* (ERC/DRC by type, constraint misses, unrouted nets, a picture).

```
                   LLM (Claude)                                    knowledge/
   requirements -> parts, topology, constraints  <---- part facts, pin tables (rv1106.yaml, cited)
        |  MCP (stdio, ~20 coarse tools, compact JSON + images)
        v
 +-------------------------- llm-pcb server ---------------------------+
 |  Design IR (design.json)          Layout state (layout.json)        |
 |  components, pin->net, netclasses  placement, tracks, vias, zones    |
 |  constraints, board spec                                             |
 |      |            |              |            |            |         |
 |  parts/blocks  schematic.py   placer.py    router.py    checks.py    |
 |  (ref circuits (hier. sheets, (SA + legal- (DSN writer, (DRC/ERC     |
 |   + calcs)     labels, ERC,   ise + legal  Freerouting, digest,      |
 |  gen.py (QFN/  netlist        refinement)  SES import,  design       |
 |   symbol gen)  parity)                     planes)      review)      |
 |      |            |              |            |            |         |
 |      +---------- board.py: IR+layout -> .kicad_pcb (s-expr) ---------+
 +---------------------------------------------------------------------+
        |  kicad-cli 10 (ERC, DRC, zone fill, Gerber/drill/CPL/PDF/render)
        |  Freerouting 2.5 (headless, Java 25)
        v
   KiCad project: .kicad_pro / .kicad_sch (per block) / .kicad_pcb / fab outputs
```

## Why no pcbnew/SWIG and why not (only) IPC

* KiCad 10's IPC API only talks to a *running GUI* (headless IPC is planned for KiCad 11);
  it also cannot plot/export. SWIG `pcbnew` is deprecated (removal scheduled).
* llm-pcb therefore **writes KiCad's documented s-expression formats directly** (`sexpr.py`,
  `schematic.py`, `board.py`) and uses **kicad-cli** as the verification/manufacturing
  oracle. Correctness of the writers is checked *by KiCad itself*: the schematic netlist
  exported by kicad-cli must equal the IR (`netlist_parity`), DRC runs with
  `--schematic-parity`, and pad coordinates were verified against kicad-cli's
  IPC-D-356 export.
* The IR is the single source of truth; KiCad files are artefacts. `sync_from_kicad`
  pulls human edits (positions, tracks) back from the board file. A live IPC backend
  (push into an open KiCad GUI) is a natural extension once headless IPC lands.

## Modules

| Module | Role |
|---|---|
| `ir.py` | Pydantic models: `Design` (components, pin→net, netclasses, constraints, board) and `Layout` (placement, tracks, vias, zones). |
| `libs.py` | Stock KiCad 10 symbol/footprint access, `extends` flattening, pad geometry, KiCad rotation convention; generated-part hooks. |
| `gen.py` | Box-symbol generator (pins on physical sides, name-aware spacing) and IPC-style QFN footprint generator. |
| `parts.py` | Curated parts with design facts (e.g. TLV62569 Vfb/L/C guidance), generated parts from knowledge files, pin resolution by number / name / mux-function / glob. |
| `blocks.py` | Reference circuits that do the engineering math and emit placement intent: buck (E96 divider), decoupling, crystal (CL→Cload), USB-C sink (+ESD, diff-pair), LED, button, … |
| `schematic.py` | Hierarchical schematic (one sheet per block), labels at pin endpoints, NC flags, PWR_FLAGs; ERC; netlist parity check; project file with netclasses and fab rules. |
| `board.py` | `.kicad_pcb` writer: 4/6-layer stackup, footprints with nets and schematic links, outline, mounting holes, tracks, vias, zones. |
| `placer.py` | Anchors (edge/fixed) → **rigid cluster macros** (parts with tight `near` constraints packed around their hub: buck IC, SoC, connector) → block seeding in floorplan regions → simulated annealing (HPWL, near constraints, overlap, containment) → spiral legalisation → legal-only refinement with targeted jumps. `place_parallel` runs N seeds on N cores and keeps the top-16 placements. |
| `router.py` | Specctra DSN writer (opaque padstack ids, pre-rotated images, full and regional planes, footprint keepouts, netclass rules, protected wiring), Freerouting headless (own job timeout, neckdown), SES importer. `route_placements` routes the top-N placements concurrently and keeps the best by routing + placement cost; `complete` routes only missing connections around protected copper; deterministic `plane_fanout` (via/stub per plane-net pad, QFN GND→EP tie) and hole-aware `stitch_vias`. |
| `checks.py` | DRC/ERC digests (grouped by type with examples), design review (near-constraint misses, diff-pair skew, IPC-2221 current vs width, unconnected power pins). |
| `render.py` | Fast PIL board view (blocks coloured, ratsnest, tracks by layer), kicad-cli 3-D render, schematic PDF→PNG. |
| `export.py` | Gerbers (zones refilled), drill, CPL, JLC BOM, IPC-D-356, PDFs, zip. |
| `server.py` | MCP server (`mcp` SDK 2.x `MCPServer`), compact JSON responses, per-call latency/size log, atomic batched IR edits. |

## Tool surface (MCP)

`project_open, project_status, part_search, part_info, pin_query, blocks_list, block_add,
ir_edit (atomic batch), netlist_view, schematic_build, board_setup, place (parallel seeds, macros),
place_part, move, route (parallel placement portfolio | complete), copper (plane_fanout | stitch | restitch),
connect_pads, zones, drc, design_review (incl. escape feasibility), render_view, export_fab,
run_script (escape hatch, logged), sync_from_kicad, ipc_sync (live KiCad GUI, optional)`.

## Double-sided placement

`Placement.side` (`top|bottom`) is supported end-to-end:

- **Board writer:** writes a bottom-side footprint the way KiCad stores a flipped one. Local x is mirrored, F./B. layers are swapped, text is mirrored, and zones are made absolute. This is verified against KiCad's own IPC-D-356 export (`tests/test_core.py::test_bottom_side_mirroring_matches_kicad`).
- **Placer:** an *orientation* encodes rotation and side.
  - Courtyards collide only on the same side.
  - Through-hole parts block both sides.
  - Plated holes inside SMD parts block the opposite side locally, for example the QFN's exposed-pad thermal vias.
- **Permissions:** `side` constraints from the LLM decide which parts may use the bottom. By default only the top is allowed, with a small cost for using the bottom.
- **Cluster macros:** members such as decoupling caps may sit on the other side from their hub. The result is 0402 decoupling directly under the QFN pin ring, between the thermal vias, as a human would place it.

## hroute: the human-like router (`router_cpp/hroute.cpp`, `llmpcb/hroute.py`)

hroute is a C++ grid router (0.05 mm grid, which aligns with 0.35 mm pitch). It is built on demand with g++.

- **Geometry:** 45° moves only, with no acute turns. Costs per bend (45°/90°), per via, for wrong-direction travel on directional layers (In2 horizontal, B vertical) and for proximity to foreign copper. The result is few bends, few vias, orthogonal or 45° runs and even spacing, which is what makes it look hand-routed.
- **Clearance:** a centre-line cell is legal if no foreign copper lies within `half-width + max(own, other net's clearance)`. Board edge, keepouts, NC pads and holes are hard obstacles. Coarse 8×8 occupancy blocks give O(1) free-space queries, and legality is memoised per search.
- **Neck-down:** inside a 0.9 mm zone around a net's own fine-pitch pads (≤ 0.45 mm), tracks narrow to 0.127 mm. That lets 0.25 mm power tracks enter a 0.35 mm-pitch QFN.
- **Search:** weighted A* over (cell, arrival-direction) states. Per-cell direction state made pads unreachable under the turn constraints, which is a classic bug and was fixed.
- **Completion:** negotiated congestion (PathFinder).
  - Nets are first routed cleanly where possible.
  - Contested nets are then routed allowing overlaps, at a present-congestion cost × (1 + history).
  - All copper is repainted, conflicts are detected, history is raised, and only conflicting nets are re-routed (24 iterations).
- **Differential pairs:** routed as ONE coupled centre-line of width `2w + gap` between escape anchors found outward from each pad pair. The line is then offset into P/N tracks with mitred corners. Pair layer changes are side-by-side via pairs. The four breakouts are routed by the maze engine, and the offset tracks are verified against everything except their own net. Any failure falls back to single-ended routing.
- **Hybrid:** whatever hroute cannot finish is completed by Freerouting around hroute's copper, which is protected (`route(engine="hroute", finish_with_freerouting=true)`).

Other routers considered:

| Router | Status |
|---|---|
| Freerouting | kept as the completion engine and fallback |
| KiCad push-and-shove | interactive only, needs the GUI via IPC in KiCad 10 |
| OrthoRoute | GPU router; also needs the KiCad GUI via IPC |
| Quilter / DeepPCB / TopoR | cloud or commercial, not usable headless here |

## Using the cores

Freerouting's autorouter is single-threaded and deterministic, so the server spends parallelism on **diversity of
placements**: `place(seeds=72)` runs 72 annealing chains (one process each) and keeps the 16 best; `route(candidates=16)`
routes those 16 placements concurrently (the remaining cores go to Freerouting's optimizer threads) and keeps the best
by a combined cost (unrouted, violations, placement quality, vias). With `engine="hroute"` every
(placement × cost-profile) combination is an independent router process (≈0.4 GB each), all run concurrently.

## Toolchain (user space, no sudo)

* KiCad 10.0.8 from the official PPA, unpacked with `dpkg-deb -x` into `tools/kicad-root`
  plus its shared-library dependencies (`apt-get download`), activated by `tools/env.sh`.
* Freerouting 2.5.0 jar; Java 25 + Python 3.12 + deps via pixi (`pixi.toml`).
