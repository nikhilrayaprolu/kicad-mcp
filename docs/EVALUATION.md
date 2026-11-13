# Evaluation: designing an RV1106 Linux SBC from scratch with llm-pcb

I (Claude) used the llm-pcb MCP server as its only operator. Every design action went through real MCP stdio calls
(`clients/mcp_call.py`; 18 call batches are in `designs/rv1106-sbc/calls/`; every call is logged to
`client_calls.jsonl` and `mcp_calls.jsonl`). The engine code was improved between batches whenever dogfooding
exposed a problem. Those fixes are part of the result and are listed below.

## Outcome

| | |
|---|---|
| Board | **LP-1106**: Rockchip RV1106G3 (Cortex-A7, 1 TOPS NPU, 256 MB DDR3L in package), 65 × 50 mm, 4 layers (JLC 7628 stackup), single-sided assembly |
| Features | USB-C 5 V in + USB2 OTG (5.1 k Rd, USBLC6 ESD), 4× TLV62569 bucks (0.9 / 1.35 / 1.8 / 3.3 V, RC-sequenced like the Luckfox reference), 10/100 Ethernet (integrated PHY → HR911105A MagJack), 1 Gbit SPI-NAND boot, microSD, 2-lane MIPI-CSI FPC, debug UART, 2×10 GPIO header with a PTC-protected 5 V pin, reset and recovery buttons, LEDs, 4× M2 holes |
| Size | 130 components, 89 nets, 105 constraints, 6 netclasses, 9 hierarchical sheets |
| **ERC** | **0 errors, 0 warnings**; netlist exported by kicad-cli = IR (0 mismatches) |
| **DRC** | **0 errors, 0 unconnected**. 401 warnings: silkscreen overlaps (334), zone islands (63), 3 silk-edge, 1 dangling via. There are also 53 schematic-parity *warnings*, all of the form "pad has no net, schematic calls it `unconnected-(…)`", i.e. intentionally unconnected SoC pins |
| Copper | 857 track segments (F 710 / In2 114 / B 33), 481 vias (including 233 GND stitching vias), zones: GND on F/In1/B, VSYS split plane on In2 |
| Fab outputs | Gerber X2 (6 copper/mask/paste/silk + edge), Excellon PTH/NPTH + maps, CPL, JLC-format BOM (50 lines), IPC-D-356, layer PDF, schematic PDF; `out/rv1106_sbc-gerbers.zip` |
| Where | `designs/rv1106-sbc/` (`rv1106_sbc.kicad_pro` opens in KiCad 10) |

## How the work split between LLM and engines

**What the LLM decided:**
- **Requirements and architecture:** the power tree, sequencing, interfaces, and which SoC pins carry what.
- **Pin mapping:** by datasheet function name (`UART2_TX_M1`, `MIPI_CSI_RX_CK0P`, `DVDD*`), resolved against
  `knowledge/rv1106.yaml`, which was extracted from the Rockchip datasheet with a page citation for every pin.
- **Reference-circuit choices:** buck rails, crystal load, decoupling sets, USB-C sink, ESD, ZQ/EXTR resistors, reset/recovery.
- **Netclasses and diff pairs.**
- **Floorplan:** edge connectors, block regions and a VSYS split plane.
- **Corrective intent after reading diagnostics:**
  - `ETH`/`CSI` clearances that could not escape the 0.35 mm pitch
  - a PTC on the header 5 V
  - fixing its own floorplan conflict (USB-C resistors outside the power region)

**What the engines produced, with no coordinates from the LLM:**
- **Schematic:** 9 sheets, labels, PWR_FLAGs, ERC.
- **Placement:** 72-chain parallel annealing with rigid cluster macros.
- **Plane fanout and routing:** 16 placements routed concurrently through Freerouting.
- **Copper finishing:** pours, stitching, incremental completion, DRC, review, fab outputs.

**How often the LLM had to step in by hand:**
- `run_script` (the free-form escape hatch): **0 uses**.
- Two narrow deterministic micro-tools, each used once:
  - `place_part`: put the new PTC in the nearest legal spot without disturbing the routed board.
  - `connect_pads`: tie two adjacent header pins of the same net that the router left open.

**Token efficiency:**
- 156 tool calls; about 80 kB of text responses (about 20 k tokens), excluding images.
- The heaviest text responses were `design_review` and `project_open`.
- The 9 `place`/`route` calls that returned images account for most bytes. That's by design: the LLM looks at the board.

## Convergence (same board, successive engine/rule fixes)

| Run | What changed | Freerouting unrouted | DRC errors | DRC unconnected |
|---|---|---|---|---|
| 07 | first route, single core, descriptive padstack names | ≈250, stalled (killed at 25 min) | n/a | n/a |
| 09 | **padstack-name bug fixed**, 12 router strategies on one placement | 65 | 131 | 65 |
| 10 | route 12 *placements* in parallel, GND plane fanout | 35 | 17 | 35 |
| 12 | footprint keepouts → DSN, USB class fits USB-C pitch, VSYS split plane, safer fanout | 19 | 7 | 22 |
| 13 | footprint-zone coordinate fix, outer GND pours and stitching, combined candidate cost | 5 | **0** | 7 *(but a buck inductor 16 mm from its IC)* |
| 14 | rigid cluster macros (placement score 3 634 vs 5 772) | 18 | 0 | 19 |
| 15 | macros + 1 mm fanout halo (no gain) | 21 | 0 | 20 |
| 16 | **ETH/CSI netclasses made escapable at 0.35 mm pitch**, neckdown on, QFN GND→EP tie | **2** | 0 | 4 (all VSYS) |
| 17–18 | PTC + VSYS_HDR rail, `route(mode="complete")`, hole-aware restitch, `connect_pads` | 0 | **0** | **0** |

**Parallelism (80 cores):**
- 64–72 annealing chains run concurrently, at about 2.4× the wall time of a single chain on a shared machine (load 15–45).
- 12–16 placements are routed concurrently, about 8–15 min per portfolio.
- The best-of-portfolio spread was large: 18 to 43 unrouted across the candidates of a single run. This is where the cores actually pay off.
- Running several Freerouting strategies on the *same* placement was useless. Its autorouter is deterministic and
  single-threaded, and its `-us`/`-is` flags only affect the optimizer, so 12 variants gave 2 distinct boards.

## Engineering review of the result (honest)

**Good**
- **Every SoC power pin is supplied and decoupled** per the reference (counts per rail match the Luckfox sheet).
  The ZQ (240 Ω 1 %), ETH_EXTR (6.04 k 1 %), nPOR RC, recovery key and crystal network all come from the cited reference values.
- **The bucks are compact clusters.** Every inductor is 2.2–2.3 mm from its IC's SW pin, and input caps are within 1.9 mm (one is at 4.2 mm, see below).
- **Power distribution is solid:**
  - In1 is a solid GND plane.
  - VSYS has a regional In2 pour.
  - Every GND SMD pad has a dedicated via, or a stub to the exposed pad.
  - 233 stitching vias tie the F/B pours to the plane.
- **Power-up sequencing:** 0.9 V comes up first (EN tied high). DDR and 1.8 V share an RC-delayed EN (100 k / 100 n), and 3.3 V uses a longer RC (200 k / 1 µ).
  This mirrors the reference order, though it is RC timing rather than power-good chaining.

**Weak / needs a human before fab**
1. **The 24 MHz crystal (Y1) is 11 mm from XIN/XOUT.** The macro put it next to the pins, but legalization of the
   crowded SoC ring pushed it out. It will probably oscillate, but this is poor practice. Next step: protect hub-critical
   members during legalization.
2. **Differential pairs are routed as independent nets** (Freerouting has no coupled routing). Skew:
   ETH RX 9.8 mm, ETH TX 5.2 mm, USB 5.0 mm, CSI CK 3.3 mm, CSI D0 1.8 mm. 10/100 Ethernet and USB2 HS at these
   lengths usually work, but this misses the stated ≤1 mm / ≤0.5 mm targets. Impedance is not controlled. This needs a
   coupled-pair router; Anthropic's demo needed its own router for the same reason.
3. **Decoupling distances:** 13 SoC caps sit 2.8–5.5 mm from their pins (target 2.5–4.5). Single-sided assembly around a
   QFN-128 is the root limit. Real boards put 0402s on the bottom under the package, and the engine has no
   bottom-side placement yet.
4. **Track widths:** PWR_HI is 0.4 mm while the review flags 2 A ⇒ 0.78 mm (IPC-2221, 10 °C). The real VSYS load is about 0.6 A, and
   most VSYS current flows in the In2 pour, so this is acceptable, but `current_a` should be set to measured values.
5. **The QFN-128 land pattern is generated** from the datasheet package drawing (no recommended pattern is published) and is
   unverified. It needs a fine-pitch-capable fab (0.35 mm pitch, 0.15 mm pad gap, 0.09–0.1 mm features).
6. **Camera:** the RV1106 MIPI/CIF I/O domain is 1.8 V only. The 15-pin RPi-style FPC needs a 1.8 V-I/O camera module,
   or I2C level shifting, and that is documented only, not enforced. There is no RTC crystal (pins NC) and no SD power switch (simplified versus the reference).
7. **Silkscreen:** 334 overlap warnings come from 0402 reference designators. A real release would hide or shrink them.
   3-D bodies are absent because the `kicad-packages3d` deb was not installed.
8. **Unverified knowledge items** are carried as `unverified: true` in `rv1106.yaml`:
   - regulator part numbers in the reference
   - the 4-lane CSI pairing
   - maskrom behaviour
   - the 115200 console baud rate

## Bugs and capability gaps found by dogfooding (all fixed unless noted)

| # | Finding | Fix |
|---|---|---|
| 1 | `ir_edit` not atomic: a bad pin name left a half-connected SoC | transactional batch with rollback |
| 2 | Generated 4-sided QFN symbol: pin names overlapped | name-aware box geometry |
| 3 | Generated parts vanished when the library file was regenerated | on-demand regeneration hooks |
| 4 | Simulated annealing (SA) left overlaps; the legaliser threw parts 10 mm away | legal-only refinement with targeted jumps |
| 5 | Portfolio scoring treated soft regions as hard and picked a seed with the crystal 16 mm away | rescored |
| 6 | **Freerouting mis-resolves padstack names that differ only in their numbers**: every 0.35 mm QFN pad became a 0.7 mm blob, giving 208 phantom violations | opaque padstack ids (the single biggest blocker) |
| 7 | Router strategy portfolio gave identical results (deterministic autorouter) | portfolio over placements instead |
| 8 | Freerouting killed on timeout writes nothing | its own `job_timeout` |
| 9 | Neckdown below the fab minimum; KiCad hole clearance stricter than the fab | JLC preset corrected (0.09 mm track, 0.2 mm hole) |
| 10 | Footprint keepouts (microSD) not passed to the router | exported as DSN keepouts |
| 11 | Footprint zones are *absolute* in `.kicad_pcb` while pads are relative, so the keepout was copied to the origin | transform on write (regression test added) |
| 12 | Plane fanout ignored class clearance and keepouts | conservative clearance, stub check, via reuse, EP tie |
| 13 | Best-routed candidate had a 15 mm buck SW node | candidate cost = routing + placement quality |
| 14 | Independent-particle placement scattered buck parts | **rigid cluster macros** |
| 15 | LLM-chosen ETH/CSI netclasses could not escape 0.35 mm pitch; no tool said so | new `design_review` escape-feasibility rule |
| 16 | Long power run to the header unroutable | design change (PTC + VSYS_HDR), `route(mode="complete")` |
| 17 | Stitching vias landed on plated holes and under later-placed parts | hole-aware obstacles, `restitch` |
| open | coupled diff-pair routing, bottom-side placement, legalization that respects macro-critical members, NC-pad net names for parity, silkscreen auto-cleanup, IPC live mode (implemented, untested without a GUI) | next steps |

## Verdict

The architecture held up: the LLM never produced a coordinate, and every fix to the *board* was expressed as intent
(netclasses, constraints, a fuse, a regional plane). Every fix to the *tooling* was a deterministic engine change.
Compact diagnostics made each iteration cheap to reason about.

The decisive factors were not the size of the tool surface but:
- correct geometry interchange (padstack and zone bugs)
- verification by KiCad itself (ERC, netlist parity, DRC, IPC-D-356 coordinates)
- design-rule knowledge in the loop (escape feasibility)
- spending the 80 cores on *diverse placements* rather than on router flags

What separates this from a fab-ready board is mostly what the notes on Anthropic's demo pointed to:
- a router that understands coupled pairs
- double-sided placement around a fine-pitch SoC
- a human electrical review of the items listed above


---

# v2: double-sided placement, our own router, coupled differential pairs

Follow-up request: make routing more human-like (own router or other routers), improve placement, use both board
sides, and route differential pairs. v1 is preserved in `designs/rv1106-sbc/out/*_v1*` and `out/fab_v1_singlesided_freerouting/`.

## What was built

| Capability | Implementation |
|---|---|
| **Double-sided placement** | `Placement.side`. The board writer mirrors bottom footprints the way KiCad stores them, verified against KiCad's own IPC-D-356 export (test). The placer's orientation includes the side. Courtyards collide per side, THT parts block both sides, and SMD parts' plated holes block the opposite side locally. The LLM grants bottom-side permission with `side` constraints. |
| **Human-like router "hroute"** | C++ (`router_cpp/hroute.cpp`): 0.05 mm grid, 45°-only geometry, bend/via/wrong-direction/proximity costs, per-netclass clearances, neck-down at fine-pitch pins, weighted A* over (cell, direction) states, and negotiated congestion (PathFinder) with history costs. |
| **Coupled differential pairs** | One centre-line corridor of width 2w+gap between escape anchors, offset into P/N tracks with mitred corners and paired vias. Breakouts are maze-routed, and the result is verified against the partner's pads with a fallback. |
| **Hybrid routing** | Critical nets (crystal, buck switch nodes, VSYS/VBUS) and coupled pairs are laid out by hroute and locked. Freerouting routes the bulk around them. |
| **Parallel repair** | N concurrent rip-up-neighbourhood × Freerouting-completion variants, each scored by real KiCad DRC in its own sandbox. |
| **All 80 cores** | 64-seed placement; 16–32 concurrent router jobs (placement candidates × cost profiles); 32 concurrent repair variants. Load average reached about 57–63. |

## Router bugs found while building it (all fixed)

1. **Turn-locked dead ends:** one arrival direction per cell plus the ban on acute turns made pads unreachable. Fixed with (cell, direction) states.
2. **Float stale-check:** G was stored as float but compared with exact doubles at a 1e-6 tolerance, so valid queue entries were discarded and whole regions became unreachable. This was the decisive bug: a single power net went from "unreachable" to routed in 12 s.
3. **Sentinel clash:** the "no partner net" sentinel (-2) equalled the blocked-cell marker, so routes ran through NC pads, NPTH holes and the board edge band.
4. **Silent net loss:** a net that failed during negotiation had no copper, hence no conflicts, and dropped out without being reported. Fixed with outcome tracking and a final audit.
5. **Pair corridor over pads:** the corridor exempted both nets' pads and clipped the partner's pads. The corridor now avoids all pads, and anchors require legal offset positions.
6. **Via-to-via spacing** along one path; **pair spacing quantisation** at mitred corners.

## Result on the RV1106 board

| | v1 (single-sided, Freerouting) | v2 (double-sided, hybrid) |
|---|---|---|
| Parts on bottom | 0 | 37 (SoC decoupling under the QFN pin ring, ZQ/EXTR resistors, crystal network) |
| Near-constraint misses / total excess | 29 / 27.1 mm | **15 / 8.7 mm** |
| 24 MHz crystal to XIN | 11.1 mm (miss) | **within 4 mm** |
| Coupled differential pairs | 0 | **2 coupled** (ETH RX skew 0.83 mm, CSI D0 0.38 mm) |
| Other pairs (single-ended) | skew 1.8–9.8 mm | skew 3.2–8.1 mm (ETH TX, CSI D1, CSI CK, USB) |
| Track geometry | 61 % orthogonal, 37 % 45°, 2 % other | 56 % orthogonal, 38 % 45°, 7 % other (other = pair offsets, breakouts) |
| Vias | 481 (incl. 233 stitching) | 591 (incl. 233 stitching; bottom-side decoupling needs vias) |
| DRC errors / unconnected | 0 / 0 | **0 / 0** (ERC 0/0, netlist parity OK) |

### How v2 got to zero

1. **Placement** (11 min): 64 annealing chains with bottom-side permissions and cluster macros.
2. **Pure-hroute portfolio** (16 runs): our router completed **80 of 84 nets** on the best candidate, with 4 coupled pairs. This run exposed the silent-net-loss bug: KiCad showed 39 unconnected.
3. **Critical-first hybrid portfolio** (32 jobs: 16 placements × 2 profiles): crystal, switch nodes, VSYS/VBUS and coupled pairs by hroute, the rest by Freerouting. Best: DRC 2 errors / 8 unconnected. Across candidates, Freerouting left 21 to 62 unrouted, so diversity again mattered.
4. **Last mile:**
   - widen neck-down artefacts to the fab minimum (`copper min_width`)
   - `route mode=complete` and `connect_pads` for a header link
   - **parallel DRC-scored repair** (32 variants twice). The second pass found 8 variants with 0/0.

## Honest assessment of v2

- **Placement is clearly better**, and that is the biggest electrical win:
  - decoupling sits under the SoC pins on the bottom
  - the crystal is next to XIN/XOUT
  - near-constraint excess dropped 3×
- **hroute geometry looks hand-drawn:** 45° corners, few bends, parallel buses, layer direction discipline. Its completion rate on this dense double-sided board is still below Freerouting's push-and-shove.
  - Negotiated congestion takes it from 54/84 (sequential rip-up) to 75–80/84.
  - It needs about 25 min single-core per run, and the safety fixes made iterations slower.
  - So the production path is the hybrid: hroute for structure and critical nets, Freerouting for bulk, and parallel repair to close the board.
- **Coupled pairs work** (skew 0.38–0.83 mm) but survive in only 2 of 5 pairs on the final board. The others failed the coupled corridor in this placement, or were re-routed single-ended during repair. Next steps:
  - route pairs before placement freeze (reserve corridors)
  - add serpentine length tuning for single-ended fallbacks
  - add pair-aware repair
- **Remaining review items are unchanged from v1:** buck input caps 0.4 mm over target, PWR_HI width vs 2 A, the camera 1.8 V I/O domain, and an unverified QFN land pattern. Double-sided assembly also costs more at JLC.

| v2 artefact | Path |
|---|---|
| Final layout | `designs/rv1106-sbc/out/layout_v2_final.json` |
| Gerbers | `out/rv1106_sbc-gerbers.zip` (v1: `out/rv1106_sbc-gerbers_v1.zip`) |
| Renders | `out/board_2d.png`, `out/board_3d_top.png`, `out/board_3d_bottom.png` (v1: `*_v1.png`) |
| MCP call batches | `designs/rv1106-sbc/calls/19_*.json` … `29_*.json` |


## v2.1: length matching and clean-up

| Item | Before | After | How |
|---|---|---|---|
| ETH TX skew | 6.63 mm | **0.30 mm** | `tune_lengths`: 2 serpentine meanders on ETH_TXN |
| CSI D1 skew | 3.23 mm | **0.15 mm** | 1 meander on CSI_D1_P |
| CSI CK skew | 8.07 mm | **0.15 mm** | 1 meander on CSI_CK_N |
| ETH RX / CSI D0 | 0.83 / 0.38 mm | unchanged | already coupled by hroute |
| USB D+/D− | "3.58 mm skew" | reported as multi-pin (A/B row bridge + ESD stub), not a path skew | review rule fixed |
| Schematic-parity warnings | 53 | **0** | NC pads get KiCad's `unconnected-(…)` net names |
| Silkscreen warnings | 296 | 57 | designators of small passives moved to the fab layer (`board.silk_refs = large`) |
| Dangling vias / stubs | 4 / 3 | 0 / 0 | `copper dangling_vias` (pour-aware) + T-junction-aware stub pruning |
| PWR_HI current finding | 2 A ⇒ 0.78 mm | none | rated 1 A (≈0.6 A estimated load, noted in the design) |
| **Final DRC** | | **0 errors · 0 unconnected · 0 parity** · 116 warnings (59 isolated pour islands, 57 silk) | |

Meanders are single-sided trombones placed on the longest straight segment with room. Their height and period are sized to the skew, and every point is checked against copper on its layer with netclass clearance before it is committed. KiCad DRC confirms the result.

Remaining review items:
- buck input caps 0.4 mm over a tight 1.5 mm target
- USB-C CC resistors 6 mm from the connector (target 5)
- PWR escape at the 0.35 mm-pitch SoC relies on neck-down
- camera 1.8 V I/O
- unverified QFN land pattern
