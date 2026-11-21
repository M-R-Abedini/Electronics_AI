# Schematic Integrity, 4-Track Design Review, Safe ECO & Fabrication Release

This reference extends Electronics AI 2.0 with battle-tested industrial KiCad/PCB production practices—covering pre-placement schematic integrity, independent 4-track pre-fab design review, strict DRC repair order, plane-island recovery, safe ECO on routed boards, KiCad 10 tooling guardrails, and the manufacturer-ready fabrication release package.

---

## 1. Pre-Placement Schematic & Netlist Integrity Gate

Never start PCB placement or routing on an unverified schematic. Run these checks before invoking `placer__intent_draft` or `placer__place_board`:

1. **Zero-Invention Pinout Rule:** Every IC pin number MUST come from a manufacturer datasheet, verified vendor reference schematic, or stock KiCad library symbol. For fine-pitch BGAs/SoCs/DDR packages, cross-check ball tables against two independent sources (datasheet ball map + reference schematic).
2. **Clean ERC (`kicad-cli sch erc`):** Run `kicad-cli sch erc --severity-all` until `0 errors` and `0 warnings`.
   - Nets containing only `power_in` pins require a `PWR_FLAG` (one per rail + `GND`).
   - Multi-LX / parallel switch outputs must be `passive` (not multiple `power_out` fighting each other).
3. **Checks ERC Misses (Verify From Netlist & Visual Schematic PDF):**
   - **Duplicate references across hierarchical sheets** (ERC may not catch all duplicates; BOM/netlist check catches them).
   - **Un-netted footprint pads** (symbol-to-footprint pin mismatch!). Watch specifically for **KiCad 10 library pin renames** (e.g., `USB_A` shield `5 -> SH`, `Micro_SD_Card_Det1` shield `10 -> SH`).
   - **Single-pin dangling nets** (accidental label typos across sheets).
   - **Actual regulator output voltages computed from the real netlist resistor dividers** (`Vout = Vref * (1 + Rtop / Rbot)`) and current-limit resistor values.
   - **Visual PDF review:** Render schematic PDF sheets and zoom-inspect dense IC/connector blocks for text overlaps, missing junctions, or ambiguous labels.

---

## 2. Independent 4-Track Read-Only Pre-Fab Design Review

Before ordering PCBs (and whenever the user asks for a full design review), execute **four independent read-only review tracks** over the schematic, netlist, routed board, and reference documents.

### Verification Discipline
- **Verify every finding against source datasheets and actual schematic/PCB crops BEFORE editing anything.**
- Classify every finding explicitly as **`blocker`**, **`major`**, **`minor`**, or **`false alarm`**.
- Present verified findings to the user before making structural/architectural changes.
- Record every verified accepted exception in `fab/review/WAIVERS.md`.

### The 4 Review Tracks

| Review Track | Mandatory Checks |
|---|---|
| **1. Power & Protection** | Worst-case rail voltage vs component absolute maximum (capacitors, LDOs, FETs, ICs); fuse/TVS clamping & current ratings; remote-sense fallback paths; reversed diodes/LEDs/polarized caps; buck converter `Cin` within `~1–2 mm` of `VIN`/`PGND` and short wide `SW` polygon; power feeds not accidentally left at signal width; exposed-pad thermal via count and solder-mask tenting/plugging. |
| **2. SoC / MCU / Memory & High-Speed** | SoC boot/strap/analog-reference/power balls verified against vendor reference schematic; crystal CL & unshielded keepout (`signal_under_crystal == 0`); diff-pair impedance computed from real stackup/field solver (never guessed); length/skew tuning order (**remove old meanders before re-tuning -> match DQS/CK pairs first -> grow lagging pair leg first -> match each DDR byte lane to its DQS -> tune address/command to CK**), reporting residual mismatch in **both `mm` and `ps` (`~6–7 ps/mm` on FR4)**. |
| **3. Peripherals & Connectors** | Edge-connector mating direction (card slots / USB / HDMI / headers facing outward!); **un-netted footprint pads**; connector -> ESD -> IC in-line order (`<= 5 mm`); bus signal ordering along chains; polarity and pin-1 orientation on headers and test points. |
| **4. Physical, DFM & Fabrication** | **Three fiducials per SMT side** with correct board-only / exclude-from-BOM / exclude-from-position attributes; hand-fitted modules with solder paste disabled and excluded from CPL; MPN/LCSC fields synced to footprints (`--schematic-parity == 0`); **IPC-7351 silkscreen** (`0` on pads, `0` under bodies, user port labels preserved); **mirrored bottom-view visual inspection** (`P9`). |

---

## 3. Strict DRC Repair Cascade & Plane-Island Recovery

### 3.1 Exact DRC Repair Order
Never chase cosmetic warnings while hard connectivity or clearance errors remain. Resolve KiCad DRC (`kicad-cli pcb drc --refill-zones --schematic-parity --severity-all`) in this strict cascade order:
1. **`shorts` (`shorting_items`)**
2. **`clearance` & `track_width` / `via_diameter` / `hole_to_hole`**
3. **`unconnected_items`** (distinguish `escape_blocked`, `via_blocked`, `path_blocked`, and severed inner-plane links)
4. **`crossing` & `forbidden items` (keepout / rule-area / courtyard / `mask_aperture` violations)**
5. **`diff_pair` & `skew` / `length` violations**
6. **`dangling` tracks & orphan vias** (match exact net, layer, length, and reported endpoint before deleting—never hit-test blindly and delete a good trunk)
7. **`silkscreen` (`silk_over_copper`, `silk_overlap`, IPC-7351 height/stroke/ratio, `under_body`)**
8. **`--schematic-parity` (`0` mismatches)**

### 3.2 Plane, Pour & Island Connectivity Discipline
- **Count zone-fill islands after EVERY copper or zone edit:** distinguish islands containing pads/vias from pad-less dead copper islands.
- **Check after widening power feeds or editing zone outlines:** a wider track or modified keepout can silently sever an inner-plane link—always re-refill and re-check `unconnected_items`.
- **Outer GND pours:** respect RF keep-away, remove dead pad-less islands, stitch only where both outer/inner pours overlap, and respect board `hole_to_hole` clearance.
- **Last-Resort 4-Layer Plane Escalation (ONLY when normal signal layers leave a few open nets):**
  1. Never allow clocks, crystals, USB, MIPI, Ethernet, or high-current power nets on a plane escalation layer.
  2. Shield high-speed copper on adjacent layers and preserve all footprint/board rule areas.
  3. **Bridge every reference-plane slot cut by an escalated track** with GND stitching vias on both sides plus a short outer-layer GND strap at slot crossings, bus ends, and roughly `5 mm` intervals.
  4. Refill zones, prune pad-less islands, report total plane-layer track length and bridged/unbridged slot crossings honestly, and re-run DRC.

---

## 4. Safe ECO Protocol on a Routed Board

When applying a schematic/BOM revision to an already routed board:
1. **Diff old vs new netlists BEFORE touching copper:** list changed values, swapped footprints, added/removed parts, and pin-to-net changes.
2. **Preserve existing references:** removed parts keep their reference consumed; newly added parts take explicit free references (never re-annotate existing references on a routed board!).
3. **Apply minimally & deterministically:**
   - For a flipped 2-pin part, rotate `180°` in place rather than relocating.
   - Strip copper belonging to vanished nets.
   - For localized footprint moves/swaps, use `placer__place_cluster(board_path=..., refs=[...], intent_path=..., seed=0, preserve_routing="outside_cluster", silkscreen_policy="auto", fab_profile="ipc_standard")` so healthy copper outside the moved cluster is preserved and only moved-pad, colliding, and dangling stubs are stripped.
4. **Re-verify:** Refill all zones, confirm locked BGA/IC fanout escapes survived (`reconcile_fixed.missing_from_session == 0`), run `kicad-cli pcb drc --refill-zones --schematic-parity`, and visually re-inspect Top & Bottom renders (`P9`).

---

## 5. KiCad 10 & Production Tooling Guardrails

Keep these concrete production pitfalls in mind whenever inspecting, routing, or validating KiCad 10 boards:
- **Always copy BOTH `.kicad_pro` AND `.kicad_dru`** next to every intermediate `.kicad_pcb` before zone refill or DRC. Without `.kicad_pro`, DRC falls back to KiCad defaults and creates hundreds of false errors; **one malformed rule in `.kicad_dru` can silently drop ALL custom rules!**
- **Never trust DRC zone results without `--refill-zones`:** `kicad-cli pcb drc` evaluates the saved zone fills unless refilled.
- **Honor footprint-owned rule areas (`fp.Zones()`):** e.g., SD-card sockets or RF modules with internal via/copper keepouts; filter paste-only QFN sub-pads from copper obstacle checks.
- **SWIG geometry rules (for custom inspection scripts):** `GetSize()` is pre-rotation (use `GetBoundingBox()` for rotated geometry); never compare `pcbnew` SWIG proxies with `is`/`in` (compare UUID/position); keep references to `Remove()`d objects alive until the helper exits; avoid calling `LoadBoard` twice in the same process.
- **Check `.kicad_dru` custom rules explicitly:** Electronics AI warns whenever a `.kicad_dru` file contains custom rules not translated into Specctra DSN—verify them directly in KiCad DRC after routing.

---

## 6. Manufacturer-Ready Fabrication Release Checklist

Before declaring a project ready to send to a PCB fabricator / assembler, verify every item and deliver the **actual KiCad project files** alongside manufacturing outputs (never renders alone):

1. **Zero-Error Electrical & Parity Gates:** `kicad-cli sch erc --severity-all` (`0` errors / `0` warnings) AND `kicad-cli pcb drc --refill-zones --schematic-parity --severity-all` (`0` errors, `0` unconnected, `0` parity mismatches), plus F6 `lint.critical_errors == 0`.
2. **Real Stackup & Impedance Table:** Document actual copper weights, dielectric thicknesses, `epsilon_r`, and field-solver controlled-impedance track/gap targets (flag as `unknown` if fab stackup is not yet locked).
3. **Grouped BOM + Supplier BOM + CPL:** Verify MPN/LCSC part numbers, stock, package, and voltage ratings; exclude DNP parts, fiducials, test pads, mounting holes, and hand-fitted modules from CPL.
4. **Fiducials & Mechanicals:** Confirm **3 fiducials per SMT side** with proper BOM/CPL exclusions, mounting-hole clearances, and enclosure/connector mating orientation.
5. **IPC-7351 Silkscreen & Assembly Drawings:** Verify `reference_designators.errors == 0`, `under_body == 0`, user port labels & polarity marks preserved, and export `F.Fab`/`B.Fab` assembly drawings PDF.
6. **Fabrication Outputs:** Export **Gerber X2**, **Excellon PTH + NPTH drill files & drill maps**, **CPL/positions**, **schematic PDF**, **ERC/DRC JSON reports**, and **Top + Mirrored-Bottom visual renders**.
7. **Visual & Second-Opinion Review (`P9`):** Visually inspect both **Top and Mirrored-Bottom** views plus zoomed BGA/power/connector regions, complete the 4-track review, and record any accepted waivers in `fab/review/WAIVERS.md`.
8. **Explicit Fabrication Notes (Include in Release README / Fab Drawing):**
   - Target controlled impedance nets & tolerance;
   - Surface finish (e.g., ENIG for fine-pitch BGA/QFN, HASL lead-free for standard);
   - **Via-in-pad IPC-4761 Type VII (resin-filled & copper-capped)** whenever microvia-in-pad or blind/buried HDI stackup is used;
   - Minimum track/space/via/drill and laser microvia dimensions (if HDI);
   - **Silkscreen fab confirmation:** confirm minimum printable text height/stroke and cured ink thickness (`15–25 um`, min `10 um`) with the target fab;
   - Assembly sides (Level 1 / 2 / 3), low-stock parts, THT/hand-assembly items, and panelization rails/breakaway tabs.
9. **Honest "Physical Checks Still Required" List:** Always end a release hand-off by listing checks software cannot prove (e.g., physical 1:1 printout pad handedness for custom modules, connector mechanical mating/detect polarity, thermal bench verification, RF antenna tuning).
