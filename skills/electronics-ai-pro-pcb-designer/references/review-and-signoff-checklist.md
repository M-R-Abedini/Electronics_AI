# Senior Hardware-Team Review, Diagnosis & Sign-Off Checklist

Follow this checklist after every `placer__place_board`, `placer__place_cluster`, `placer__score`, or `placer__refine` run before reporting results or proposing `placer__apply`.

---

## 1. Hard Gates First (A High Pro Score NEVER Excuses a Failed Hard Gate)

Verify every hard gate directly from `run_report.json` / tool output:

| Gate | Requirement | Meaning if Failed |
|---|---|---|
| **Placement Legality** | `strict_violations == 0` | Courtyard overlap, outline overhang, edge copper clearance, TH-to-opposite-pad collision, or movable pad on free-standing `mask_aperture`. **Block apply.** |
| **Solve Budget** | `s0_s5_seconds <= 300` | Placement exceeded budget; inspect `timings` and candidate breakdown. |
| **Routing Completion** | `unrouted == 0` (or explicit comparison vs expert under equal budget `C1`) | Inspect `failures` causes (`escape_blocked`, `via_blocked`, `path_blocked`) and `router_stop` (`converged` vs `timeout`). |
| **KiCad DRC (Router)** | `drc_router_caused == 0` | Router created short, clearance, track-width, or mask-bridge error. **Block apply.** |
| **KiCad DRC (Non-Routing)** | `drc_new_non_routing == 0` | New placement/silkscreen/mask error not present on untouched reference board. **Block apply.** |
| **F6 Electrical Lint** | `lint.critical_errors == 0` (when `has_copper == true`) | Fast/critical signal split-plane crossing, track under crystal, `FB` near `SW` or under inductor, or severe decap loop (`> 12 mm`). **Resolve before sign-off.** |
| **Fixed Copper Integrity** | Macro & `S6a` copper preserved bit-for-bit after Freerouting | Check `reconcile_fixed.missing_from_session == 0`. |
| **IPC-7351 Silkscreen & Assembly Legend** | `silkscreen.audit.reference_designators.errors == 0` and `under_body == 0` (unless `silkscreen_policy="off"`) | Reference designators violate IPC height (`>= 0.80 mm`), stroke (`>= 0.15 mm` / `0.12 mm` LPI), ratio (`>= 1:10`), pad clearance (`>= 0.20 mm`), or sit hidden beneath a component body. Re-run with `auto` / `dense` / `assembly_only` and confirm fab ink limits (`15–25 um`). |
| **Mandatory Visual Inspection (`P9`)** | Rendered **Top AND Bottom** PNGs visually inspected via `placer__gallery(runs=[...], include_images=true)` (plus `placer__diagnose(run=..., include_images=true)` crops if issues exist) | Metrics alone cannot catch port labels (`Value` fields) dragged under connectors, ambiguous legends, awkward rotations, or mechanical overhang hazards. **Never sign off or propose `placer__apply` without viewing the images.** |

---

## 2. Interpreting Pro Score & Congestion Metrics

- **Pro Score v1 weights:**
  - `routability`: **35%** (real Freerouting `coverage * 100` when routed; RUDY fallback when placement-only)
  - `critical_loops`: **25%** (median decap pad -> target IC supply pin distance from `design_intent.json`: `1.5 mm -> 100`, `6.0 mm -> 0`)
  - `ratsnest`: **15%** (`100 / (1 + 4 * crossings_per_edge)`)
  - `regularity`: **15%** (`50% aligned_fraction + 30% rotation_on_90_grid + 20% dominant_axis_fraction`)
  - `lint`: **10%** (F6 real-copper electrical lint score `0..100`; automatically activated when `has_copper == true`, omitted and renormalized when placement-only)
- **PathFinder Congestion (`metrics.pathfinder`):**
  - High `total_overflow` (`> 0`) or `hotspots` predicts routing failure (`Spearman +0.907` vs unrouted across benchmarks) far more reliably than `RUDY`.
  - Check `congestion_refinement.accepted_rounds` and remaining `hotspots` coordinates (`x_mm`, `y_mm`) to target `placer__diagnose` and `placer__refine`.

---

## 3. Understanding Comparisons (C1 / C2 / C3) & `inconclusive`

Always label which comparison is being reported:
- **C1 (Pure Placement):** Our placement vs expert placement, both routed from scratch with identical Freerouting settings and timeout.
- **C2 (Complete Product):** Our full output (`placement + S6a + Freerouting + refill`) vs the expert's original routed board.
- **C3 (S6a Value):** Same placement routed with `S6a` vs without `S6a` under identical router budget.
- **When a route ends with `router_stop == "timeout"`:**
  - Never declare a placement "better" or "worse" from a single timed-out run unless the opponent converged and the margin exceeds measured board routing noise. State **`inconclusive (router timeout)`** honestly!

---

## 4. Root-Cause Triage Matrix (`placer__diagnose` -> Action)

| Observed Symptom | Root Cause to Check | Correct Action |
|---|---|---|
| High `decap_median_mm` or `decap_p90_mm` | Unresolved decap `candidates` in `design_intent.json` (no affinity pull) | Resolve target IC + supply pin from schematic/datasheet, re-validate intent, re-run with `floorplan=true`. |
| `escape_blocked` / `via_blocked` on QFN/BGA pins | Passive or neighbor blocking pad escape channel | Run `placer__diagnose` to locate blocker, test single-part move/rotation with `placer__refine(preview=true)` or run `placer__place_cluster` around the IC. |
| `reference_split_crossing` in F6 Lint | Fast/critical signal routed across a plane gap or split | Keep signal on solid-GND-referenced layer, adjust floorplan block separation, or verify stackup policy. |
| `reference_change_stitch_cap` in F6 Lint | Fast signal transitioned `GND <-> PWR` reference layers without nearby stitching cap | Keep fast signal on GND-referenced outer layer or add a `10–100 nF` stitching cap within `3.0 mm` of the via. |
| `signal_under_crystal` or `fb_near_sw` / `fb_under_inductor` | Unrelated track entered crystal body or feedback track coupled to switching node/inductor | Re-route affected net or refine local block placement (`placer__place_cluster` / `placer__refine`). |
| `solder_mask_bridge` or `silk_overlap` | Movable pad touched board/logo mask aperture or silkscreen label collided / failed IPC-7351 bounds | `placer__place_board` automatically runs the IPC-7351 silkscreen legend engine (`1.0–1.27 mm` standard / `0.80 mm` dense floor, `0.15–0.18 mm` stroke, `>= 1:10` ratio, condensed width preference, `>= 0.20 mm` pad clearance, outside-body placement, leader lines for displaced critical parts, `F.Fab`/`B.Fab` assembly offloading for dense passives) and `heal_mask_bridges`; verify `silkscreen.audit` (`dfm_score`, height/stroke/ratio, `under_body == 0`) and remind the user to confirm printable height/stroke and ink process (`15–25 um` cured ink thickness) with their PCB fab before release. |

---

## 5. Prohibited Claims & Anti-Patterns (Zero Tolerance)

1. **NEVER** output raw `(x, y)` coordinates or tell the user to manually type coordinates from your prose.
2. **NEVER** claim "DRC-clean", "100% routed", or "production-ready" without quoting the actual `run_report.json` gate results (`drc_router_caused`, `drc_new_non_routing`, `unrouted`, `lint.critical_errors`, `router_stop`).
3. **NEVER** sign off a layout or propose `placer__apply` from scalar metrics alone without visually inspecting rendered **Top AND Bottom** PCB images via `placer__gallery(runs=[...], include_images=true)` (`P9`), and **NEVER** excuse a visual defect (such as hidden port labels or ambiguous silkscreen) because `DRC == 0`.
4. **NEVER** silently overwrite the user's source `.kicad_pcb` or `.PcbDoc`—always preview (`dry_run=true`) and require user confirmation before `placer__apply(..., in_place=true)`.
5. **NEVER** invent datasheet limits, stackup `epsilon_r`, impedance values, fab ink thickness, or `power_w` numbers from memory (P5).
6. **NEVER** hide `unknowns`, `capacity_warnings`, `inconclusive` router timeouts, or unverified impedance/3D-height/fab-silkscreen gaps from the final engineering summary.

---

## 6. Sign-Off Report Template (Deliver Concisely in the User's Language)

For every completed placement/routing task, present:
1. **Board & Policy Summary:** Layer count, stackup policy, assembly side policy, `silkscreen_policy` & `fab_profile`, `intent_sha256`, fixed anchors, and any remaining `unknowns`.
2. **Hard Gates Table:** Legality (`strict_violations`), runtime (`s0_s5_seconds`), routing coverage / `unrouted` / `router_stop`, KiCad DRC (`router_caused` / `new_non_routing`), F6 Electrical Lint (`critical_errors` / `warnings` / score), Silkscreen (`reference_designators.errors`, `under_body`), and Mandatory Visual QA status (`Top + Bottom` inspected).
3. **Key Engineering Metrics:** `Pro Score` (with component breakdown), `decap_median_mm` / `p90_mm`, `crossings`, `PathFinder` overflow, and comparison vs baseline/expert (when available under P2).
4. **Silkscreen & Assembly Legend Summary (IPC-7351):** `dfm_score`, height/stroke/ratio bounds, Condensed count, Leader Lines added, references offloaded to `F.Fab`/`B.Fab`, confirmation that user port labels (`Value` fields) & polarity marks were preserved, and the pre-fab reminder to verify printable height/stroke and cured ink thickness (`15–25 um`, min `10 um`) with the target PCB manufacturer.
5. **Visual & Lint Findings (Show the User):** Concrete findings from `placer__gallery(runs=[...], include_images=true)` (Top & Bottom) and `placer__diagnose(run=..., include_images=true)` with artifact links (`placed_board`, `routed_board`, `gallery.html`, `run_report.json`) so the user can visually inspect the board before approving apply.
6. **Safe Apply Offer:** Explicit `placer__apply` options (recommended sibling copy vs `in_place=true` with `< 2 s` `placer__revert`).
