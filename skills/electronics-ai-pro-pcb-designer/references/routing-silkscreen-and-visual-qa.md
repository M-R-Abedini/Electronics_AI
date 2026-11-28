# End-to-End Placement, Routing, IPC-7351 Silkscreen & Mandatory Visual QA

This reference defines the **canonical execution order** from placement through routing, post-route DRC healing, **IPC-7351 Silkscreen (Top/Bottom Overlay) & Assembly Legend (`F.Fab`/`B.Fab`)**, and the **Mandatory Vision-Driven QA Loop (`P9`)** in Electronics AI 2.0.

---

## 1. Canonical End-to-End Execution Order (Placement -> Escape -> Route -> Heal -> Silkscreen -> Visual QA)

Follow this strict engineering hierarchy. Never skip upstream stages to patch downstream symptoms.

| Stage | Engineering Priority | Deterministic Mechanism / Tool | Exit Condition |
|---|---|---|---|
| **1. Ground Truth & Intent** | Read board geometry, stackup, fixed mechanical skeleton, and baseline silkscreen/visual state; settle & validate `design_intent.json`. | `placer__inspect`, `placer__intent_draft`, `placer__intent_validate` | `errors == []`, `stackup_policy` & `side_policy` settled, mechanical locks confirmed. |
| **2. Macro Floorplan & Placement (`S0-S5`)** | Mechanical anchors -> functional blocks & signal/power flow -> critical local loops (`buck_hot_loop`, `decap`, `crystal`, `esd`) -> selective escape reservations -> Rust SA + PathFinder congestion & guarded Cell Inflation -> passive banking + mask-aperture avoidance. Component assembly-guide silkscreen texts (`Reference` designators) are NEVER placement or routing obstacles. | `placer__place_board(..., floorplan=true)` or `placer__place_cluster` | `strict_violations == 0`, `s0_s5_seconds <= 300`, tight `decap_median_mm`, low PathFinder overflow. |
| **3. Critical Escape & Copper (`S6a`, Pre-Route)** | Run deterministic DRC-guarded critical copper **before** general autorouting: (a) 4-quadrant channel-preserving BGA dogbone escapes (`>= 4L`, plane balls by default, LGA/rectangular pads automatically excluded; on sub-`0.65 mm` HDI BGAs where dogbone cannot fit, plan `L1-L2` / staggered `L2-L3` microvia-in-pad per `.kicad_pro` `allow_microvias` + netclass `microvia_diameter`/`microvia_drill` + `min_via_annular_width` and record IPC-4761 Type VII filled/capped fab notes), (b) inner-plane SMD power/GND taps (`>= 4L`, pad-trapping guarded), (c) confirmed diff pairs (`skew <= 0.15 mm`, equal vias), (d) same-side `IC pin -> decap pad -> via` direct links. | Enabled by default via `placer__place_board(..., route=true, s6a=true)` | `0` pre-route DRC touching S6a (1-pass surgical prune + automatic clean fallback to bare board if any conflict persists). |
| **4. General Autorouting & Zone Refill** | Route remaining nets with `Freerouting` (`fanout=false`, `automatic_neckdown=false`, `strict_drc=true` to prevent micro-neckdown `track_width` errors), reconcile locked fixed copper, re-verify S6a plane taps, and refill all zones. | Built into `placer__place_board(..., route=true)` | `reconcile_fixed.missing_from_session == 0`, `tap_fill_after_ses` confirmed, `router_stop` explicitly reported (`converged` vs `timeout`). |
| **5. Deterministic Post-Route Healing** | (a) Surgical `sanitize` of unlocked router-caused offending copper (`max 3` passes, never touching locked S6a/macro copper) with exact unrouted accounting; (b) two-pass `starved_thermal` healing (`45°` spoke rotation -> `ZONE_CONNECTION_FULL` fallback); (c) free-standing `.Mask` aperture `solder_mask_bridge` healing via native `DoNotAllowZoneFills` rule area + zone refill. | Built into `route/check.py` during `placer__place_board(..., route=true)` | `drc_router_caused == 0`, `drc_new_non_routing == 0`, `lint.critical_errors == 0`. |
| **6. IPC-7351 Silkscreen & Assembly Legend** | Normalize Reference Designators (`F.Silkscreen`/`B.Silkscreen`), apply board-proportional sizing (`0.80 mm` dense floor on miniature `< 400 mm^2` / `< 18 mm` boards; `1.00–1.27 mm` + Condensed-first on standard boards), enforce `>= 0.20 mm` pad clearance & outside-body placement, add DRC-clean Leader Lines for displaced critical parts, preserve user-facing `Value` port labels & polarity marks, offload any part (passive or crowded critical) that lacks clean overlay space onto `F.Fab`/`B.Fab`, and normalize `F.Fab`/`B.Fab` texts so zero assembly labels protrude outside the board outline (`fab_outside_board == 0`). | `silkscreen_policy` & `fab_profile` in `placer__place_board` / `placer__place_cluster` | `silkscreen.reference_designators.errors == 0`, `under_body == 0`, `fab_outside_board == 0`, `silk_outside_board == 0`, `drc_new_non_routing == 0`. |
| **7. Mandatory Vision-Driven QA Loop (`P9`)** | Inspect rendered **Top AND Bottom** PNGs plus `assembly_fab` (`F.SilkS`/`B.SilkS` + `F.Fab`/`B.Fab`) and `silkscreen_visual_qa` plus zoomed diagnostic crops for defects that scalar metrics miss; iterate via `placer__refine` / `placer__place_cluster` / policy change and re-inspect images before proposing `placer__apply` (and always re-inspect after any custom/manual post-route edit). | `placer__gallery(runs=[...], include_images=true)` and `placer__diagnose(run=..., include_images=true)` | Both hard gates AND visual inspection pass (overlay presence/sizing OR verified clean `F.Fab`/`B.Fab` transfer); visual renders presented to the user before apply. |

### Safe ECO Routing Order (Local Modifications)
When modifying only a subset of footprints on an already routed board:
1. Inspect the existing board and visual state first (`placer__inspect`).
2. Run `placer__place_cluster(board_path=..., refs=[...], intent_path=..., seed=0, preserve_routing="outside_cluster", silkscreen_policy="auto", fab_profile="ipc_standard")`.
3. Confirm healthy copper outside the cluster was preserved, only moved-pad/colliding/dangling stubs were stripped, and zero new DRC errors were introduced.

---

## 2. IPC-7351 Silkscreen (Top/Bottom Overlay) & Assembly Legend Standard

Silkscreen dimensions have no single universal law, so Electronics AI 2.0 implements **IPC-7351** recommendations plus selectable manufacturer profiles while strictly respecting board DRC minimums and post-assembly legibility.

### 2.1 Dimension & Manufacturing Table

| Parameter | IPC-7351 Recommended | Dense-Board Practical Floor | Enforcement & Notes |
|---|---|---|---|
| **Text Height** | `1.00 – 1.27 mm` (`40 – 50 mil`) | `0.80 mm` (`32 mil`) | IPC-7351 recommends `>= 1.0 mm` (`1.2 mm` ideal for standard boards); never drop below `0.80 mm` on silkscreen. |
| **Stroke Width** | `0.15 – 0.18 mm` (`6 – 7 mil`) | `0.12 – 0.15 mm` | `0.15 mm` minimum on standard screen-print fabs (`ipc_standard`, `jlcpcb`, `pcbway`); `0.12 mm` allowed only on `high_precision_lpi`. |
| **Stroke-to-Height Ratio** | `1:6` to `1:8` (`0.125 – 0.167`) | **Hard floor `>= 1:10` (`0.10`)** | Below `1:10` strokes break or fade; above `1:4` (`0.25`) strokes bleed and blur. |
| **Pad & Mask Clearance** | `>= 0.20 mm` | `>= 0.20 mm` | Zero ink on exposed solder pads or solder-mask openings (`0 silk_over_copper`). |
| **Cured Ink Thickness** *(fab-process)* | `15 – 25 um` | `>= 10 um` (below `10 um` prints faint) | **CAD controls stroke width (`mm`), NOT ink thickness (`um`).** Ink thickness depends on screen-print / LPI / inkjet process; always report `cured_ink_spec_um` and remind the user to verify minimum printable height/stroke with their PCB fab before fabrication. |

### 2.2 Readability Decision Ladder (Board-Proportional Sizing & Assembly Offload)

When placing or cleaning a Reference Designator on `F.Silkscreen` / `B.Silkscreen`, the engine follows this strict priority order:
1. **Board-proportional sizing:** On **miniature boards** (`area < 400 mm^2` or `min(width, height) < 18 mm`, e.g., wearable/sensor PCBs), prefer the compact **`0.80 mm` dense floor** (`0.80 x 0.80 mm` -> condensed `0.60 x 0.80 mm`, stroke `0.15 mm`) immediately instead of oversized `1.00–1.27 mm` text. On normal-size boards, start at **`1.00 x 1.00 mm`** (`0°` or `90°`), strictly inside board outline, `>= 0.20 mm` from pads, outside all component bodies.
2. **Condensed width FIRST (`height` constant!):** On standard boards, try `0.78 x 1.00 mm` (narrower character width at full `1.00 mm` height) **before** stepping height down.
3. **Dense floor:** `0.80 x 0.80 mm` -> `0.60 x 0.80 mm` (keeping stroke `>= 0.15 mm` or `>= 0.12 mm` on `high_precision_lpi`, and ratio `>= 1:10`).
4. **Displaced critical parts -> DRC-Clean Leader Line:** For critical components (`ic`, `converter_ic`, `connector`, `crystal`, polarized caps/diodes/LEDs, switches, jumpers, test points) displaced away from their body (`>= 1.5 mm`), draw a straight or 1-bend L-shaped silkscreen leader line verified clear of pads and silkscreen obstacles.
5. **No clean overlay space -> Assembly Drawing (`F.Fab`/`B.Fab`):** Any non-critical passive—and any critical part on a miniature/crowded board—that physically has **no** legal, collision-free, outside-body slot on `F.Silkscreen`/`B.Silkscreen` is hidden from silkscreen and given a crisp, body-proportioned Reference Designator on `F.Fab`/`B.Fab` (never shoved underneath a component body or left colliding!). On sparse/miniature boards whose input references all start hidden (`visible_ref_designators == 0` with `<= 16` parts and `< 45%` courtyard utilization), `policy="auto"` evaluates open board space to place clean board-proportioned overlay refs where space exists and keeps the rest cleanly on `F.Fab`/`B.Fab`.
6. **IPC-A-610 Assembly-layer (`F.Fab`/`B.Fab`) normalization & mirror hygiene:** Every visible text on `F.Silkscreen`/`F.Fab` has `Mirrored=False` and on `B.Silkscreen`/`B.Fab` has `Mirrored=True` (`0` mirror DRC warnings). Every `F.Fab`/`B.Fab` `Reference` is centered strictly inside its component body along the long axis (`0.14–0.85 mm` vector assembly font, fitting even inside `0201` bodies without colliding with neighbors). On dense miniature boards (`is_miniature` with `> 12` footprints), cluttering `F.Fab`/`B.Fab` `Value` strings are hidden so the Assembly Drawing stays clean and Ref-only per IPC-A-610 (`fab_outside_board == 0`, `fab_overlaps == 0`, `fab_oversized == 0`).
7. **Preserve user-facing labels & polarity:** Visible `Value` fields used as user port labels (e.g., `USB1..USB7`, connector pinouts), board instructions, and polarity marks (`+`, `-`, pin-1 indicators) are **NOT** treated as component Reference Designators—they are preserved on silkscreen even in `dense` and `assembly_only` policies and are **never** dragged underneath component bodies.

### 2.3 Choosing `silkscreen_policy` and `fab_profile`

Select these parameters explicitly on `placer__place_board` and `placer__place_cluster` based on `placer__inspect` density and the user's fabrication target:

- **`silkscreen_policy` (exact live enum values):**
  - `assembly_only` *(default)*: Moves **all** component assembly-guide `Reference` designators off `F.Silkscreen`/`B.Silkscreen` (`visible_ref_designators == 0`) cleanly onto `F.Fab`/`B.Fab` (Ref-only with zero `Value` clutter, standardized discrete vector tiers `0.16-1.00 mm`, Condensed width before stepping down a tier, `1:7` stroke, `0` outside-board / overlap / mirror errors). Assembly-guide silkscreen texts NEVER constrain placement or routing. Polarity marks, pin-1 indicators, and user-facing port/safety labels stay clean on silkscreen obeying IPC-7351 bounds.
  - `auto`: Auto-detects board crowding (`courtyard_utilization >= 45%`, `median_neighbor_gap < 1.0 mm`, or `>= 60%` tight pairs). Keeps `1.00-1.27 mm` + Condensed wherever space permits; steps down to `0.80 mm`, adds Leader Lines for displaced critical parts, and offloads unplaceable passives to `F.Fab`/`B.Fab`.
  - `ipc_standard`: Enforces standard IPC-7351 readability (`1.00-1.27 mm`) across normal-density boards; offloads only non-critical passives that cannot fit cleanly.
  - `dense`: Proactively offloads all non-critical passives (resistors, unpolarized ceramic caps, beads) to `F.Fab`/`B.Fab` so critical ICs, connectors, polarized parts, crystals, switches, jumpers, and test points remain crisp and unobstructed on silkscreen.
  - `off`: Leaves existing silkscreen untouched (use only when explicitly requested or when benchmarking raw placement roundtrip).
- **`fab_profile` (exact live enum values):**
  - `ipc_standard` *(default)*: `1.00–1.27 mm` recommended, `0.80 mm` dense floor, `0.15–0.18 mm` stroke, `>= 0.20 mm` pad clearance.
  - `jlcpcb`: JLCPCB production limits (`>= 0.80 mm` dense floor / `1.00 mm` target, `>= 0.15 mm` stroke, `>= 0.20 mm` pad clearance).
  - `pcbway`: PCBWay production limits (`>= 0.80 mm` dense floor / `1.00 mm` target, `>= 0.15 mm` stroke, `>= 0.20 mm` pad clearance).
  - `high_precision_lpi`: High-precision LPI / direct-inkjet silkscreen (`0.80 mm` dense floor, `0.12 mm` minimum stroke, `>= 0.15 mm` pad clearance). Use only when the user confirms a precision LPI/inkjet fab process.

---

## 3. Mandatory Vision-Driven QA Protocol (Law P9 — Closed Visual Feedback Loop)

For new runs, use `quality.placement` and `quality.routing` to qualify the selected artifact.
Use the saved per-side, assembly, copper-layer and interface views linked from that artifact's
review. Check the reported repair rounds before comparing the resulting positions or artwork.
An approved placement can carry unfilled zone definitions. Inspect its separately labeled native
refill preview for copper geometry. Use the routed artifact and its final saved-board DRC for
connectivity and fabrication conclusions. A timed-out router or any open connections leaves routing
at trial status. The combined pipeline starts S6a after placement approval. Apply an approved
placement with `use_routed=false`. Rerun legacy or edited
artifacts to obtain a fresh review tied to the board and project-rule hashes.

> **HARD ENGINEERING RULE:** Scalar numbers (`Pro Score`, `strict_violations == 0`, `drc_new_non_routing == 0`, `silkscreen.dfm_score`) are **NEVER** sufficient to sign off a PCB layout. You **MUST** visually inspect rendered board images before declaring a layout clean or proposing `placer__apply`.

### Why Vision Is Non-Negotiable
Real-world PCB defects routinely pass automated numeric checks while failing immediate human inspection—for example:
- User port labels (`USB1..USB7`) or connector legends shifted into an awkward or hidden position under a connector overhang;
- Silkscreen text sitting legally clear of pads yet visually ambiguous about which part it labels, or upside-down/inconsistently rotated;
- Mechanically valid placements that still crowd cable entry, screw heads, or tall electrolytic/inductor clearances;
- Visually tangled macro bus flow or asymmetric differential-pair/decap clusters that hurt manufacturability and debugging.

### Required Visual Inspection Sequence
1. **Baseline Sight (Existing Boards, Stage 1 / 3):** Before modifying an existing board, inspect its current top/bottom renders (via `placer__gallery(runs=[...], include_images=true)` on a baseline `placer__score` run when available) so you know what port labels, logos, connectors, and keepout zones must be preserved.
2. **Mandatory Top + Mirrored-Bottom + Assembly (`F.Fab`/`B.Fab`) Full-Board Inspection (Stage 5):** After EVERY `placer__place_board` or `placer__place_cluster` run (and after ANY manual/custom post-route copper edit), call:
   - `placer__gallery(runs=[...], include_images=true)` (max 2 runs per call) and inspect **BOTH Top AND Mirrored-Bottom rendered PNGs**, the **`assembly_fab` overlay view** (automatically emitted whenever `F.Fab`/`B.Fab` legends are used or defective), AND the **`silkscreen_visual_qa`** summary (`overlay_mode`, `is_miniature`, `visible_ref_designators`, `visible_fab_texts`, `fab_outside_board`, `oversized_for_board`, `legend_coverage`).
   - **Explicit Overlay vs. Assembly Check:** Never accept a completely blank green PCB (`visible_ref_designators == 0`) just because `dfm_score == 100`. Verify visually: (a) if open space exists on Top/Bottom Overlay, are legends printed at board-proportioned IPC size (`0.80 mm` on miniature boards, `1.00–1.27 mm` on normal boards)?; (b) where space does NOT exist, are those legends cleanly transferred to `F.Fab`/`B.Fab` with `fab_outside_board == 0`?
3. **Localized Diagnostic Zoom (Stage 5):** Whenever there are unrouted nets, congestion hotspots (`metrics.pathfinder.hotspots`), DRC/Lint warnings, BGA/QFN escapes, or silkscreen/assembly findings (`fab_text_outside_board`, `silk_oversized_for_miniature_board`, `silk_under_component_body`, `missing_component_legend`), call:
   - `placer__diagnose(run=..., include_images=true)` to inspect localized PNG crops around the exact problem areas. Resolve DRC issues in the strict cascade order in [Schematic Review, ECO & Fab Release](schematic-review-eco-and-fab-release.md) (`shorts -> clearance -> unconnected -> keepouts -> diff-pair -> dangling -> silkscreen -> --schematic-parity`).
4. **Closed Visual-Feedback Loop (Spot -> Fix -> Re-Inspect):**
   - Check all 6 visual criteria from [Feranec Floorplan Methodology](feranec-floorplan-methodology.md) **plus**:
     - Every visible Reference Designator is board-proportioned (`0.80 mm` on miniature boards, `1.00–1.27 mm` on standard boards), sharp, readable (`0°`/`90°`), outside component bodies, clear of pads, and unambiguously associated with its component (or connected via a clean Leader Line);
     - Any component without clean Top/Bottom Overlay space has a clean, in-bounds Reference on `F.Fab`/`B.Fab` (`fab_outside_board == 0`, `missing_legends == 0`);
     - User port labels (`Value` fields), polarity marks (`+`, `-`, cathode bars, pin-1 dots), and board instructions remain intact and readable after assembly;
     - Copper routing (when `route=true`) shows clean bus bundles, tight buck hot loops, unobstructed crystal zones, and clean ground pour continuity.
   - **If you see ANY visual defect:** Do NOT excuse it with `DRC == 0` or `Silkscreen: 100`. Diagnose the root cause, adjust `design_intent.json`, `silkscreen_policy`/`fab_profile`, or run `placer__refine(run=..., change=..., preview=true)` / `placer__place_cluster`, re-run, and **visually re-inspect the new rendered images** until the defect is resolved.
5. **Show the User Before Apply (Stage 6):** Before asking the user to approve `placer__apply`, present the verified visual findings and links to the rendered Top/Bottom images and `gallery.html` so the user can visually confirm the board as well.
