# MCP tool reference

_Generated from the running server by `clients/gen_tool_docs.py` — do not edit by hand._

## Server instructions

```
kicad-mcp (llm-pcb): LLM-orchestrated PCB design. YOU decide intent (parts, nets, reference blocks,
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
 7 zones(gnd_pour_outer + In-layer power pours) -> copper(restitch) -> drc ; close leftovers with copper(repair) /
   route(mode="complete") / connect_pads ; copper(min_width) ; copper(dangling_vias)
 8 diff pairs: every pair must take the SAME layer changes on both legs (design_review diffpair_vias). Fix with
   copper(unstitch) + copper(ripup, nets=[P,N]) + route(mode="complete", engine="hroute", critical_nets=[P,N])
   (all profiles in parallel, best symmetric/coupled result wins), one pair at a time.
 9 finish in this order: copper(smooth) -> tune_lengths(reset=true) -> copper(restitch) -> copper(stitch_islands)
   -> copper(dangling_vias) -> schematic_build -> drc
10 QUALITY GATE: expert_review -> fix every failed check with the tool it names, LOOK at the returned renders
   against visual_checklist, repeat until grade == "expert" (0 DRC errors AND 0 warnings, symmetric pairs,
   pours on every layer, stitching, 45-degree routing, fiducials, test points, labels).
11 export_fab -> publish(push=true) to render 3-D/2-D/per-layer/schematic images + README gallery into
   designs/<name>/ and commit+push them (skip push only if the user said not to).
Pins: number, exact name, a '/'-segment of the name, or a glob ('DVDD*'). Nets are created by connecting pins.
```

## Tools

| Tool | Purpose |
|---|---|
| [`project_open`](#project_open) | Open (or create) a design project directory. Returns status. |
| [`project_status`](#project_status) | Compact summary of the design state and suggested next step. |
| [`part_search`](#part_search) | Search curated parts (with design facts), KiCad stock symbols and footprints (all words must match). |
| [`part_info`](#part_info) | Details for a curated part id (or a KiCad symbol lib_id): symbol, footprint, pins, design facts. |
| [`pin_query`](#pin_query) | List pins of a placed component (ref, e.g. 'U1') or a part id, filtered by glob on number/name |
| [`block_add`](#block_add) | Instantiate a reference circuit: buck_tlv62569, decoupling, crystal, usb_c_sink, led, button, |
| [`blocks_list`](#blocks_list) | Signatures/docs of available reference-circuit blocks. |
| [`ir_edit`](#ir_edit) | Batch edit the design IR. Each op is {"op": ..., ...}: |
| [`netlist_view`](#netlist_view) | Readable connectivity: nets (optionally of one block / glob) with their ref.pin(name) members. |
| [`schematic_build`](#schematic_build) | Generate hierarchical schematic (one sheet per block), run ERC and verify netlist parity with the IR. |
| [`board_setup`](#board_setup) | Board outline, layer count, fab rule preset, mounting holes [[x,y,drill]...] and Default netclass. |
| [`place`](#place) | Run constraint-driven placement (regions, edge, fixed, near, keepouts) and write the PCB. |
| [`place_part`](#place_part) | Place ONE (new) part at the nearest legal spot to `near` ("REF", "REF.pin" or "x,y") without touching |
| [`connect_pads`](#connect_pads) | Draw a straight track between two pads of the SAME net ("REF.pin"), only if the straight path is clear of |
| [`move`](#move) | Manually set/lock part positions: [{ref, x?, y?, rot?, dx?, dy?, lock?}]. Use for intent the placer can't infer. |
| [`route`](#route) | Autoroute with Freerouting (planes from 'plane' constraints become pours), then refill zones + DRC. |
| [`zones`](#zones) | Copper pours. add: [{net, layer, poly?[[x,y]..], priority?, clearance?, connect?}]. gnd_pour_outer adds |
| [`copper`](#copper) | Deterministic copper helpers. action=plane_fanout: drop a via + stub next to every SMD pad of the given |
| [`tune_lengths`](#tune_lengths) | Length-match differential pairs: 45-degree-mitered serpentine meanders on the shorter leg, sized to the skew |
| [`drc`](#drc) | Refill zones, run KiCad DRC (incl. schematic parity), return grouped violations with examples. |
| [`design_review`](#design_review) | Engineering review beyond DRC: near-constraint misses (decoupling/crystal), diff-pair skew, current vs width, |
| [`expert_review`](#expert_review) | The quality gate: is this board at expert standard? Re-runs KiCad DRC (zones refilled, parity) and the |
| [`publish`](#publish) | Release step: renders the board (3-D top/bottom, 2-D layout, every copper layer, every schematic sheet) |
| [`render_view`](#render_view) | Look at the design: kind=2d (fast placement/routing view), 3d (kicad-cli render), schematic (page N). |
| [`export_fab`](#export_fab) | Gerbers (zones refilled), Excellon drill, CPL, BOM (JLC format), IPC-D-356, PDFs; zips gerbers. |
| [`run_script`](#run_script) | Escape hatch: run Python with `prj`, `d` (design), `L` (layout) and llmpcb modules in scope. |
| [`sync_from_kicad`](#sync_from_kicad) | Pull footprint positions/rotations and tracks/vias back from the .kicad_pcb (after manual edits in KiCad). |
| [`ipc_sync`](#ipc_sync) | LIVE MODE (optional): sync with a running KiCad 10 PCB editor over the IPC API. push = move footprints / |

### `project_open`

Open (or create) a design project directory. Returns status.

| Parameter | Type | Default | Required |
|---|---|---|---|
| `path` | string |  | yes |
| `create` | boolean | false |  |
| `name` | string | "" |  |
| `description` | string | "" |  |
| `requirements` | array / null | null |  |

### `project_status`

Compact summary of the design state and suggested next step.

### `part_search`

Search curated parts (with design facts), KiCad stock symbols and footprints (all words must match).

| Parameter | Type | Default | Required |
|---|---|---|---|
| `query` | string |  | yes |
| `limit` | integer | 12 |  |

### `part_info`

Details for a curated part id (or a KiCad symbol lib_id): symbol, footprint, pins, design facts.

| Parameter | Type | Default | Required |
|---|---|---|---|
| `part` | string |  | yes |

### `pin_query`

List pins of a placed component (ref, e.g. 'U1') or a part id, filtered by glob on number/name
    (for RV1106 also matches every datasheet mux function, e.g. 'UART2*', '*SDMMC0*'). Shows current net.

| Parameter | Type | Default | Required |
|---|---|---|---|
| `target` | string |  | yes |
| `pattern` | string | "*" |  |

### `block_add`

Instantiate a reference circuit: buck_tlv62569, decoupling, crystal, usb_c_sink, led, button,
    resistor, capacitor, ferrite. params = the block's keyword args (see blocks_list).
    Adds parts, nets and placement constraints; returns refs + design calculations.

| Parameter | Type | Default | Required |
|---|---|---|---|
| `block` | string |  | yes |
| `params` | object |  | yes |

### `blocks_list`

Signatures/docs of available reference-circuit blocks.

### `ir_edit`

Batch edit the design IR. Each op is {"op": ..., ...}:
    add_part {part|symbol+footprint, ref?, block, value?, conn{pin:net}, nc[], mpn?, lcsc?, note?}
    connect {ref, conn{pin_or_name_or_glob: net|"NC"}}
    remove {ref} | set {ref, field, value} | rename_net {old, new}
    netclass {name, track, clearance, via_d?, via_drill?, nets[globs], current_a?, diff_width?, diff_gap?}
    constraint {kind, ...}  (kinds: fixed, edge, near, region, keepout, diffpair, length_match, plane)
    clear_constraints {kind?, ref?, block?}
    board {width?, height?, layers?, corner_r?, mounting_holes?, title?, rev?, fab?}
    note {text} | requirement {text}
    The batch is atomic: if any op fails nothing is applied and the failing op index + reason are returned.

| Parameter | Type | Default | Required |
|---|---|---|---|
| `ops` | array |  | yes |

### `netlist_view`

Readable connectivity: nets (optionally of one block / glob) with their ref.pin(name) members.

| Parameter | Type | Default | Required |
|---|---|---|---|
| `block` | string | "" |  |
| `net_glob` | string | "*" |  |
| `max_nets` | integer | 60 |  |

### `schematic_build`

Generate hierarchical schematic (one sheet per block), run ERC and verify netlist parity with the IR.

### `board_setup`

Board outline, layer count, fab rule preset, mounting holes [[x,y,drill]...] and Default netclass.

| Parameter | Type | Default | Required |
|---|---|---|---|
| `width` | number |  | yes |
| `height` | number |  | yes |
| `layers` | integer | 4 |  |
| `corner_r` | number | 1.0 |  |
| `mounting_holes` | array / null | null |  |
| `title` | string | "" |  |
| `rev` | string | "A" |  |
| `fab` | string | "jlcpcb" |  |
| `default_track` | number | 0.15 |  |
| `default_clearance` | number | 0.15 |  |

### `place`

Run constraint-driven placement (regions, edge, fixed, near, keepouts) and write the PCB.
    seeds>1 runs that many independent annealing chains in parallel (one per core) and keeps the best.
    macros: parts with tight `near` constraints form rigid clusters around their hub (buck IC, SoC, connector).
    fanout_halo_mm: keep this ring free around fine-pitch ICs (>=32 pads) for escape vias.
    Locked parts (fixed/edge or locked placement) stay put unless unlock_all. Returns stats (+ 2-D image).

| Parameter | Type | Default | Required |
|---|---|---|---|
| `effort` | number | 1.0 |  |
| `seed` | integer | 1 |  |
| `unlock_all` | boolean | false |  |
| `image` | boolean | true |  |
| `seeds` | integer | 32 |  |
| `macros` | boolean | true |  |
| `fanout_halo_mm` | number | 1.0 |  |

### `place_part`

Place ONE (new) part at the nearest legal spot to `near` ("REF", "REF.pin" or "x,y") without touching
    any other part or copper; checks courtyards (same side + through-hole parts), board edge, mounting holes and
    existing copper on the layers its pads use. side = top|bottom (default: the part's `side` constraint, else
    top). Connect it afterwards with route(mode="complete").

| Parameter | Type | Default | Required |
|---|---|---|---|
| `ref` | string |  | yes |
| `near` | string |  | yes |
| `max_radius_mm` | number | 12.0 |  |
| `rot` | number / null | null |  |
| `side` | string | "" |  |

### `connect_pads`

Draw a straight track between two pads of the SAME net ("REF.pin"), only if the straight path is clear of
    other-net copper on that layer (checked with netclass clearance). For last-mile fixes the router missed.
    b = a NET name instead of a pad: tap pad `a` into the nearest existing copper of that net with one via
    (clearance-checked on every layer) and a straight/45-degree track on the pad's layer.

| Parameter | Type | Default | Required |
|---|---|---|---|
| `a` | string |  | yes |
| `b` | string |  | yes |
| `layer` | string | "B.Cu" |  |
| `width` | number | 0.0 |  |

### `move`

Manually set/lock part positions: [{ref, x?, y?, rot?, dx?, dy?, lock?}]. Use for intent the placer can't infer.

| Parameter | Type | Default | Required |
|---|---|---|---|
| `moves` | array |  | yes |

### `route`

Autoroute with Freerouting (planes from 'plane' constraints become pours), then refill zones + DRC.
    candidates>1 routes the top-N placements from the last parallel `place` concurrently (one core-group each)
    and keeps the best routed board (fewest unrouted, then violations, vias, length). fanout_planes drops a via
    next to every SMD pad of plane nets first, so the router only handles signals.
    mode="complete": keep all existing copper (protected) and route only the missing connections.
    engine="hroute": llm-pcb's own human-like router (45-degree, few bends/vias, layer directions, neck-down at
    fine pins, coupled differential pairs). engine="hybrid": critical_nets (globs) + diff pairs by hroute, locked,
    then Freerouting routes the rest. Routes candidates x profiles (human|fewvias|tight|orthogonal) in
    parallel; finish_with_freerouting lets Freerouting complete any leftovers around the protected copper.

| Parameter | Type | Default | Required |
|---|---|---|---|
| `passes` | integer | 100 |  |
| `timeout_s` | integer | 900 |  |
| `image` | boolean | true |  |
| `candidates` | integer | 12 |  |
| `fanout_planes` | boolean | true |  |
| `mode` | string | "full" |  |
| `engine` | string | "freerouting" |  |
| `profiles` | array / null | null |  |
| `finish_with_freerouting` | boolean | true |  |
| `critical_nets` | array / null | null |  |

### `zones`

Copper pours. add: [{net, layer, poly?[[x,y]..], priority?, clearance?, connect?}]. gnd_pour_outer adds
    GND pours on F.Cu and B.Cu. Zones are filled by kicad-cli during drc/export.

| Parameter | Type | Default | Required |
|---|---|---|---|
| `add` | array / null | null |  |
| `clear` | boolean | false |  |
| `gnd_pour_outer` | boolean | false |  |

### `copper`

Deterministic copper helpers. action=plane_fanout: drop a via + stub next to every SMD pad of the given
    plane nets (run BEFORE route so the router only has signals left). action=stitch: grid of `net` vias in free
    space (after routing, ties pours/planes). action=clear: remove all tracks/vias (keep locked unless
    clear_unlocked_routes=False is overridden). action=stitch_islands: every floating pour island of `net` gets a
    via to the plane (run after zones/restitch; unstitchable slivers are removed). action=ripup: remove copper of `nets`; with rect=[x1,y1,x2,y2]
    only tracks/vias touching that rectangle (local surgery on one bad link).

| Parameter | Type | Default | Required |
|---|---|---|---|
| `action` | string |  | yes |
| `nets` | array / null | null |  |
| `net` | string | "GND" |  |
| `pitch` | number | 5.0 |  |
| `clear_unlocked_routes` | boolean | false |  |
| `edge_pitch` | number | 2.5 |  |
| `rect` | array / null | null |  |

### `tune_lengths`

Length-match differential pairs: 45-degree-mitered serpentine meanders on the shorter leg, sized to the skew
    and checked against copper on that layer with netclass clearance. pairs = optional list of net names to limit
    to. reset=True first strips earlier meanders/bevels on those pairs and re-tunes from the routed geometry.

| Parameter | Type | Default | Required |
|---|---|---|---|
| `pairs` | array / null | null |  |
| `default_tol` | number | 0.5 |  |
| `reset` | boolean | false |  |

### `drc`

Refill zones, run KiCad DRC (incl. schematic parity), return grouped violations with examples.

| Parameter | Type | Default | Required |
|---|---|---|---|
| `parity` | boolean | true |  |

### `design_review`

Engineering review beyond DRC: near-constraint misses (decoupling/crystal), diff-pair skew, current vs width,
    unconnected power pins, single-pin nets.

### `expert_review`

The quality gate: is this board at expert standard? Re-runs KiCad DRC (zones refilled, parity) and the
    design review, then checks diff-pair via symmetry and skew, copper pours on every layer, ground stitching,
    45-degree routing (no acute corners), fiducials, test points on rails, connector labels and title. Returns
    grade (expert|needs_work), each failed check with the tool that fixes it, and 2-D + 3-D top/bottom
    renders for your own visual review against `visual_checklist`. Iterate until grade == expert.

| Parameter | Type | Default | Required |
|---|---|---|---|
| `images` | boolean | true |  |

### `publish`

Release step: renders the board (3-D top/bottom, 2-D layout, every copper layer, every schematic sheet)
    into designs/<name>/images/, writes designs/<name>/README.md (gallery + review metrics) and git-commits them
    with the design sources and gerber zip (only these paths). push=True also pushes to the remote. Refuses
    while expert_review grade is needs_work unless allow_needs_work=True (the README then lists open items).

| Parameter | Type | Default | Required |
|---|---|---|---|
| `push` | boolean | false |  |
| `message` | string | "" |  |
| `allow_needs_work` | boolean | false |  |

### `render_view`

Look at the design: kind=2d (fast placement/routing view), 3d (kicad-cli render), schematic (page N).

| Parameter | Type | Default | Required |
|---|---|---|---|
| `kind` | string | "2d" |  |
| `side` | string | "top" |  |
| `page` | integer | 0 |  |
| `layers` | array / null | null |  |
| `highlight` | array / null | null |  |

### `export_fab`

Gerbers (zones refilled), Excellon drill, CPL, BOM (JLC format), IPC-D-356, PDFs; zips gerbers.

| Parameter | Type | Default | Required |
|---|---|---|---|
| `step` | boolean | false |  |

### `run_script`

Escape hatch: run Python with `prj`, `d` (design), `L` (layout) and llmpcb modules in scope.
    Saves the project afterwards. Use sparingly; every use is logged as a capability gap.

| Parameter | Type | Default | Required |
|---|---|---|---|
| `code` | string |  | yes |

### `sync_from_kicad`

Pull footprint positions/rotations and tracks/vias back from the .kicad_pcb (after manual edits in KiCad).

### `ipc_sync`

LIVE MODE (optional): sync with a running KiCad 10 PCB editor over the IPC API. push = move footprints /
    replace copper in the open board from the IR; pull = read human edits back. Needs the KiCad GUI with the
    API server enabled (KiCad 10 IPC is GUI-only).

| Parameter | Type | Default | Required |
|---|---|---|---|
| `direction` | string | "push" |  |
| `copper` | boolean | true |  |

