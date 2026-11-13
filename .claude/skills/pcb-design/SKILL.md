---
name: pcb-design
description: Design a manufacturable PCB (schematic, double-sided placement, routing with differential pairs, DRC-clean fab outputs) with the kicad-mcp MCP server. Use whenever the user asks to design, place, route, review or fabricate a circuit board, or mentions KiCad, Gerbers, an SoC/MCU board, or a PCB layout.
---

# Designing a board with kicad-mcp

Follow `AGENTS.md` at the repo root. It is authoritative. Condensed flow:

1. **Facts first:** requirements list; datasheet pin tables into `knowledge/<part>.yaml` with page cites; never invent pin numbers; check I/O voltage domains.
2. **Capture:** `project_open(create)` → `part_search`/`part_info`/`pin_query` → `block_add` (buck, decoupling, crystal, usb_c_sink, …) + atomic `ir_edit` batches.
3. **Gate 1:** `schematic_build` must give ERC 0 errors and `netlist_parity.ok`.
4. **Rules:**
   - `board_setup` + netclasses. Every class must escape the finest pitch: `track+clearance ≤ pitch`. Run `design_review` to check.
   - diffpair constraints with `tol_mm`; GND `plane` on In1.Cu.
5. **Intent:**
   - `edge` constraints for connectors
   - `region` per block, with about 40 % slack
   - `near` for critical parts
   - `side:any` for small passives around fine-pitch ICs (decoupling under the chip)
6. **Place:** `place(seeds=64)` → `render_view` → fix the *intent* from `near_violations`/`outside`, then re-place.
7. **Route:** `route(engine="hybrid", candidates=16, profiles=["human","fewvias"], critical_nets=[clock, *_SW, power rails])`.
8. **Close:** `zones(gnd_pour_outer)` → `copper(restitch)` → `drc`. Then work through `copper(min_width)` → `copper(repair, pitch=32)` → `route(mode="complete")` / `connect_pads` → `copper(dangling_vias)`, re-running `drc` after each.
9. **Pours and pairs:**
   - Pour every layer: GND on F/B and In1, power pours on In2. Then `copper(restitch)` → `copper(stitch_islands)`.
   - Pairs must have equal via counts on both legs. If not, per pair: `copper(unstitch)` → `copper(ripup, nets=[P,N])` → `route(mode="complete", engine="hroute", critical_nets=[P,N])`.
10. **Finish:** `copper(smooth)` → `tune_lengths(reset=true)` → `copper(dangling_vias)` → `schematic_build` → `drc`.
11. **Quality gate:** `expert_review`.
    - Fix each failed check with the tool it names.
    - **Look** at the returned renders against the `visual_checklist`.
    - Repeat until `grade == "expert"`: ERC 0, DRC 0 errors **and 0 warnings**, symmetric pairs, pours on every layer, fiducials, test points, labels.
12. **Release:** `export_fab` → `publish(push=true)`. This commits the 3-D/2-D/per-layer/schematic images and a README gallery under `designs/<name>/` and pushes them to GitHub.
13. Report honestly: grade, DRC/ERC numbers, every remaining finding with a judgement, and the risks you could not verify.

Engines own geometry. Do not compute track coordinates or hand-place large numbers of parts.
