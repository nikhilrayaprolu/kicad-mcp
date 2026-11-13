# AGENTS.md: playbook for designing high-quality boards with kicad-mcp

This file is for **the AI agent** (Claude, GPT, Gemini or any other MCP client) that drives the `kicad-mcp` server. Read it fully before your first tool call. It encodes what worked, and what failed, while designing a 130-part RV1106 Linux SBC with this server (`designs/rv1106-sbc/`, `docs/EVALUATION.md`).

## 0. The contract

- **You decide intent.** That covers requirements, parts, pin functions, reference circuits, netclasses, floorplan, which parts may go on the bottom, which parts must be close to which pins, and which nets are critical.
- **Engines decide geometry.** Never compute track coordinates yourself. Never place 100 parts with `move`. `move`, `place_part` and `connect_pads` are for last-mile intent only.
- **KiCad is the judge.** A board is finished only when `drc` reports **0 errors, 0 unconnected, 0 parity** and `schematic_build` reports **ERC 0 errors and netlist parity ok**. Do not claim success on anything less, and report remaining warnings honestly.
- **Every tool returns compact diagnostics.** Read them and act on them. They are your senses. `render_view` is your eyes: look at the board after `place` and after `route`.

## 1. Before any tool call: requirements and facts

1. Write down the requirements: function, interfaces, power input, rails with voltage and current, board size limit, layer count, fab house, single- or double-sided assembly, and mechanical constraints (connector edges, mounting holes).
2. **Never invent pin numbers.** For every non-trivial IC, get the datasheet pin table into a knowledge file such as `knowledge/<part>.yaml`, with page citations. Mark anything unverified. Pull reference designs, such as an evaluation-board schematic, to copy proven support circuitry: decoupling counts and values, bias resistors, ZQ, crystal loads, reset/boot straps, power sequencing.
3. Check which rails each power pin needs, including I/O voltage domains. A 1.8 V-only I/O bank wired to 3.3 V parts is a silent board killer.

## 2. Circuit capture (IR)

```
project_open(path, create=true, name, description, requirements=[...])
part_search("buck 2A") / part_info("TLV62569DBV") / pin_query("RV1106G3", "*SDMMC0*")
block_add(...) / ir_edit([...])
```

- Use **reference blocks** whenever they exist (`blocks_list`). They do the engineering math (E96 feedback dividers, crystal load caps from CL and stray capacitance, USB-C Rd, ESD) and emit **placement intent** (`near` constraints) that the placer turns into tight layouts.
  - `buck_tlv62569(block, vin, vout_net, vout, en?, soft_start_rc?)`: share an `en` net plus `soft_start_rc` to sequence rails.
  - `decoupling(block, ic, net, values=[...])`: one cap per value, each tied by a `near` constraint to a power pin of that rail.
  - `crystal(block, ic, xin, xout, freq, cl_pf, series_r?, feedback_r?)`
  - `usb_c_sink`, `led`, `button`, `resistor`, `capacitor`, `ferrite`
- **`ir_edit` is atomic.** A batch either fully applies or fully rolls back, and the reply names the failing op and the reason. Batch related edits together.
- Reference pins by **datasheet function name**, for example `{"UART2_TX_M1": "UART2_TX", "DVDD*": "VDD_0V9"}`. `pin_query` with globs on mux functions finds the right pin.
- Mark unused pins: `"RTC_XIN": "NC"`. Use a 0 Ω part or an explicit net, never a floating power pin. `design_review` flags unconnected `power_in` pins as errors.
- Use the `block` field generously (`power`, `soc`, `usb`, `eth`, `sd`, `io`, ...). Blocks become schematic sheets and placement regions.

### Gate 1: schematic

```
schematic_build  ->  erc_errors == 0  AND  netlist_parity.ok == true
```

Fix every ERC error at the IR level, then rebuild. Single-pin nets in `project_status` are almost always mistakes.

## 3. Board setup and rules (the most important step for quality)

```
board_setup(width, height, layers=4, corner_r, mounting_holes=[[x,y,drill],...], fab="jlcpcb",
            default_track=0.127, default_clearance=0.1)
ir_edit: netclass / constraint ops
```

**Netclasses:**

| Class | Typical | Rule |
|---|---|---|
| Default | 0.127 / 0.1 mm | must escape the finest pitch |
| PWR (rails into the SoC) | 0.25 / 0.1 | wide away from pins; the router necks down at fine-pitch pins |
| PWR_HI (VBUS, VSYS, `*_SW`) | 0.4 / 0.2, `current_a` = real load | set `current_a` honestly; the review checks width against IPC-2221 |
| Differential pairs | `diff_width`/`diff_gap` for the target impedance (≈0.127/0.15 on a 0.2 mm 7628 prepreg ≈ 100 Ω) | add `{"op":"constraint","kind":"diffpair","p":..,"n":..,"tol_mm":0.5}` |

- **Escape rule:** for every fine-pitch part a class touches, `track + clearance ≤ pitch` AND `track/2 + pad_width/2 + clearance ≤ pitch`. On the RV1106 (0.35 mm pitch), an ETH class at 0.2/0.15 could not leave the pins, and 4 nets stayed unrouted until that was fixed. `design_review` now reports `escape` errors. Run it before routing.
- **Plane strategy (4 layers):** `{"op":"constraint","kind":"plane","net":"GND","layer":"In1.Cu"}`, a solid ground reference under everything. Optional regional split planes for a high-current rail: `{"kind":"plane","net":"VSYS","layer":"In2.Cu","rect":[x1,y1,x2,y2]}`.
- **Fab rules** come from the `fab` preset (JLC: 0.09 mm min track, 0.1 clearance, 0.2 hole clearance). Do not tighten DRC beyond what the fab can build. Do not loosen it beyond what it guarantees.

## 4. Floorplan and placement intent

Constraints you set, in rough order of importance:

1. **Connectors on edges:** `{"kind":"edge","ref":"J1","side":"left","offset":13}`. The default rotation assumes the mating face is at +y in the footprint, which holds for USB-C, RJ45, microSD and FPC in the stock libraries. Keep connectors clear of mounting-hole keepouts.
2. **Block regions:** `{"kind":"region","block":"power","rect":[x1,y1,x2,y2]}`. Leave about 40 % slack. A region that excludes a part's `near` target creates a conflict, which the placer reports in `outside`.
3. **Near constraints** come from blocks automatically. Add your own for anything electrically critical, such as a flash near its SPI pins or an ESD part at its connector. Tight ones (≤ 4.5 mm) become **rigid cluster macros** around their hub.
4. **Double-sided assembly** (recommended around large fine-pitch ICs): `{"kind":"side","ref":"C*","block":"soc","side":"any"}` and the same for `R*`/`FB*`/the crystal. The placer then puts 0402 decoupling **under the QFN pin ring on the bottom**, avoiding the exposed-pad thermal vias. On the RV1106 this cut total near-constraint excess from 27 mm to 9 mm and moved the crystal from 11 mm to within 4 mm of XIN. Keep big, tall and through-hole parts on top. Bottom assembly costs extra.

```
place(seeds=64, effort=1.0, unlock_all=true)     # 64 annealing chains in parallel; top-16 kept for routing
render_view(kind="2d")                            # always look
```

Read `near_violations`, `outside` and `overlaps`. Fix the **intent** (regions, edge offsets, side permissions), not the coordinates, and re-place. Expect about 10 minutes for about 130 parts on 64 cores.

## 5. Routing

Recommended: **critical-first hybrid portfolio.**

```
route(engine="hybrid", candidates=16, profiles=["human","fewvias"],
      critical_nets=["U10_XIN","U10_XOUT*","*_SW","VSYS","VBUS"], timeout_s=1200)
```

- **Our router, hroute,** lays out the critical nets and **coupled differential pairs** the way a human would: 45° geometry, few bends and vias, layer-direction discipline, neck-down at fine pins. It then locks them.
- **Freerouting** routes the bulk around them.
- **16 placements × 2 profiles** run concurrently. The best result wins on unrouted count, violations, coupled pairs, placement quality and vias. Candidate spread was 21 to 62 unrouted on the RV1106, so diversity matters.
- Choose critical nets as a human would: crystal and clock nets, regulator switch nodes, high-current input rails, and anything with a length or impedance target.
- Other modes:
  - `engine="hroute"`: pure own-router portfolio, using negotiated congestion. About 25 min per run per core.
  - `engine="freerouting"`: Freerouting only.
  - `mode="complete"`: route only what is missing around protected copper. Add `engine="hroute", critical_nets=[...]` to do it with our router.

Then add pours and close the board:

```
zones(gnd_pour_outer=true)                 # GND on F/B (plus the In1 plane)
copper(action="restitch", net="GND", pitch=2.5)
drc
```

## 6. Closing the last connections (systematically)

Work through these in order and re-run `drc` after each:

1. `copper(action="min_width")`: widens router neck-down artefacts below the fab minimum.
2. `copper(action="repair", pitch=32)`: **32 concurrent rip-up/re-route variants, each scored by real KiCad DRC.** The best is applied only if it improves on the current board. This closed the RV1106 from 8 to 1, then from 1 to 0 unconnected.
3. `route(mode="complete")` for what remains, `connect_pads(a, b, layer)` for trivial same-net straight links, and `place_part(ref, near)` when a new part must be added to a routed board.
4. `copper(action="dangling_vias")` removes one-layer vias and the stubs they leave.
5. `copper(action="ripup", nets=[...])`, then repair or complete, when a net is boxed in. Rip up the blockers too. Add `rect=[x1,y1,x2,y2]` to remove only the copper touching that area, e.g. one link that violates clearance.

Never use `run_script` to fake geometry. Each use is logged as a capability gap.

## 7. Signal-integrity finishing

```
tune_lengths(default_tol=0.5)     # serpentine meanders on the shorter leg, clearance-checked per layer
drc ; design_review
```

- Order matters: `copper smooth` **before** `tune_lengths`. Smoothing skips length-matched pairs, and meanders carry their own 45° mitres. To re-tune after any change to a pair, use `tune_lengths(reset=true)`, which strips old meanders first.
- Coupled pairs from hroute are usually already matched (0.4–0.8 mm on the RV1106). Single-ended fallbacks get meanders; the RV1106 went from 6.6, 3.2 and 8.1 mm to 0.3, 0.15 and 0.15 mm.
- Multi-pin pairs, such as USB with ESD branches or A/B rows, are reported as such and are not tuned.
- `design_review` also checks decap and crystal distances, current versus width, escape feasibility, unconnected power pins and single-pin nets. Fix errors and judge warnings, then explain to the user whatever you accept.

## 8. Release

```
schematic_build      # ERC 0 / parity ok (also refreshes the netlist used for NC-pad names)
drc                  # 0 errors / 0 unconnected / 0 parity
export_fab           # Gerbers, drill, CPL, JLC BOM, IPC-D-356, PDFs, zip
render_view(kind="3d", side="top") ; render_view(kind="3d", side="bottom")
```

Set `{"op":"board","silk_refs":"large"}` on dense boards. It moves small-passive designators to the fab layer. The silkscreen legaliser runs on every board write. It places designators clear of pads, outlines, text, holes and the edge, and nudges `silk_texts` labels. Aim for **0 DRC warnings**: silk and pour-island warnings are what make a board look amateur.

Expert finish checklist: fiducials (`board.fiducials`), labelled test points on key rails, connector and pin-1 labels (`silk_texts` anchored to parts), an edge via fence (`copper restitch`), `no_pour` rule areas under magnetics and antennas, 45° corners (`copper smooth`), and identical layouts for repeated cells.

## 9. Pitfalls already paid for (do not repeat)

| Symptom | Cause | Fix |
|---|---|---|
| Hundreds of unrouted nets, about 3 min per pass | Freerouting mis-resolves look-alike padstack names | already fixed in the DSN writer (opaque ids) |
| Nets cannot leave a QFN | netclass `track + clearance > pitch` | `design_review` escape rule; shrink the class (impedance needs a coupled-pair geometry, not width) |
| Placer leaves a buck inductor 15 mm from its IC | routing completeness chosen over placement quality | candidate cost includes placement score; tight `near` constraints become macros |
| Crystal far from XIN | single-sided crowding | allow `side:any` for the crystal network |
| "Items not allowed" near the origin | footprint zones are stored in absolute coordinates | fixed in the board writer |
| Track width below the fab minimum after Freerouting | auto neck-down | `copper min_width`, then `drc` |
| Parity warnings for NC pins | schematic names NC nets `unconnected-(…)` | run `schematic_build` before `drc`; the board writer reuses those names |
| A conflict between your own constraints | e.g. a region that excludes a part's `near` target | read `outside` and `near_violations` after `place` |

## 10. Reporting to the user

State the DRC/ERC numbers, what was verified, every remaining warning class with a one-line judgement, the files (`out/fab/*`, the gerber zip, renders), and the design risks you could not verify. Examples: an unverified land pattern, I/O voltage domains, regulator choice versus the reference design. Do not round "1 unconnected" down to "done".
