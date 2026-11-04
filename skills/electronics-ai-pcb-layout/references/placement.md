# Placement decisions

Use the existing `eda__pcb_plan_placement` refine/construct engine as a candidate
generator after runtime discovery. Its attraction, grouping, HPWL and objective
terms are useful heuristics; validate the returned candidate with the currently
implemented evidence contract before considering mutation.

In `layout__plan_placement`, nested search `options` currently include `mode`
(`refine`, `construct` or `hierarchical`), `seed`, `candidate_count`, `iterations`,
`search_grid_mm`, `restart_radius_mm`, `proposal_clearance_mm`,
`selection_policy`, `managed_scope`, `recovery_mode` and explicit `candidates`.
Read the current contracts before supplying nested values. Candidate count,
seed and budget are computation settings; proposal clearance is not a
fabrication limit. The evaluator independently requires a qualified
`fabrication_profile.minimum_pad_clearance_mm` and its source.

The default gateway policy is `placement-evidence-p1-v1`; capture with
`policy_version="placement-evidence-p0-v4"` for the historical comparison.
The selected policy is bound to the context, plan, gate and receipt.

P1 uses an explicit sourced courtyard first. Otherwise its collision envelope
combines all captured pad-layer bounds and a component body/mechanical extent,
with a recorded engineering-policy margin. `envelope_source` identifies the
fallback; it is not a certified IPC courtyard or an exact 3D package model.
Partial pad ownership/geometry remains a blocker. Clearance can come from a
captured enabled All/All board DRC rule, labelled `board_drc_rule`; this proves
a design constraint, not the fabricator's capabilities.

P1's full-board-cutout override (>90% exact overlap for supported rectilinear
contours) is an operator-authorized heuristic, not proof of a nonphysical object.
Inspect `ignored_full_board_cutout_artifact` warnings and retain raw contours;
do not describe that override as manufacturing verification or remove the CAD
object. Native edits still need the exact stored passing plan and authorization
for the target and the policy assumptions used.

Use `mode="hierarchical"` to run existing local solvers, then rigid meta-cluster
placement through `UnifiedBoardModel`. Structural motif hypotheses are labelled
separately from sourced VCC/GND or TVS/crystal/switch/shunt pin bindings. A
missing pin role is not permission to manufacture a binding. The final evaluator
rejects any moved-object Tier 0 violation and any critical-leg regression;
unchanged defects outside an ECO remain visible. Proximity budgets are board-
scoped proposals unless approved. Via/layer-span cost is only a soft proxy.
`epsilon_quant_mm` stays zero without proven native grid origin and measured
on-grid evidence. Computational search pitch is not native grid evidence.

The explicitly selected P0 comparison order remains:

For explicitly supplied local PcbLibs, `scripts/extract_package_geometry.py`
can read a private native capture and emit `package_geometry` for the existing
`layout__capture_context` input. It requires a declared CourtyardTop layer,
one closed supported contour and matching live pad identities/geometry. It
does not infer courtyards from pad/overlay bounds, supply a fabrication
profile, edit a library or qualify unsupported pad stacks. Keep the detailed
source/mismatch report private. This helper does not execute placement.

- Tier 0: hard feasibility, including qualified courtyard and complete relevant
  pad/outline coverage. Missing required evidence remains unknown.
- Tier 1: transformed pad AABB Manhattan gaps for critical paths and each
  decoupler's VCC and GND relationship. Cross-side cost requires an explicit
  `via_transition_penalty_mm`. Every individual binding and aggregate must not
  increase at all; positive regression tolerance is rejected.
- Tier 2: weighted signal HPWL excluding global GND and explicitly declared
  reference/power nets. Ratsnest crossings are diagnostic proxies.
- Tier 3: declared passive-family grid and collinearity. This cannot replace
  the baseline without a measurable Tier 1 or Tier 2 improvement.

A baseline tie stays baseline. `selection_policy` cannot change tier order or
allow positive critical regression. Search budgets and weights do not waive hard
constraints or qualify unknown physics.

1. Preserve baseline and identify the permitted ECO scope. Mechanical anchors,
   locked parts, mating directions, board access and protected critical blocks
   constrain candidate generation.
2. Bind schematic function to actual package pads. Generate local motif/group
   alternatives around critical pin relationships before polishing global
   arrangement. A component reference point is not its pad or body centroid.
3. Search only supported rotations/side changes. Bottom-side transforms need a
   validated mirror model; a side flip must preserve numbering and chirality.
   Do not perform a pin/gate/part swap without verified equivalence and the
   required schematic synchronization.
4. Compare the baseline and a bounded, seeded set of alternatives. Group moves,
   legal swaps, limited backtracking and construct restarts can explore choices
   a greedy rotate-only pass misses. Record the budget and selected seed.
5. Reject hard violations and unknown required checks before scoring. Compare
   nonregression of critical paths, return/loop obligations and escape, then
   ordinary wirelength, movement and assembly/aesthetic preferences. Explain
   Pareto trade-offs; weighted totals alone are not selection evidence.

Use AABB only for broad-phase rejection. Courtyard, assembly envelopes, copper
clearance, through-board protrusions, height and enclosure checks have different
meanings. Missing courtyard or a concave outline represented only by its bbox
cannot establish legality. Top/bottom XY overlap is not inherently a collision,
but through-hole and assembly constraints still matter. An approved connector
overhang must be scoped to that object and region.

Validate the final state of all moves together: two legal swapped positions can
look colliding when each move is tested against the other's old pose. Bind the
applied list to that validated final state; do not assume native intermediate
execution is transactional.

HPWL, straight ratsnest crossings and predicted via counts are proxies.
Crossing airwires may be routed around obstacles on one layer; they do not prove
that a via is required. Filtering global rails from a signal-attraction metric
does not remove local PDN, decoupling, current or return obligations. Do not
discard every high-fanout net without its functional context.

For an explicitly authorized placement-only spike, `assembly_profile.placement_spike`
can bind `scope` (exact board path), `source.kind=user_intent`,
`critical_allowance_mm`, `target_proximity_mm` and `placement_only=true`.
These are declared engineering allowances for that board, not measured
quantization or defaults for other boards. Unrouted decoupling-return evidence
stays unknown/diagnostic; placement qualification is not routing qualification.

Use `mode=cluster_scoped` for a bounded joint passive search with explicit
decoupling pad bindings. Per-layer pad clearance against actual tracks, bounded
arcs, via annuli and poured polygons is a hard check; definitions are not pours.
`affected_routed_nets` identifies attachments requiring rerouting. Such moves
are blocked by default. An explicitly authorized disposable-board placement
trial may set board-scoped `allow_routing_detach=true` alongside
`placement_only=true`; the receipt must retain `routing_repair_required=true`.
No copper is silently ripped up. Via access contributes to the critical combined
loop proxy; it is not routed length. Run `si_pi__audit_copper_loops` once after
selection to measure real copper paths and inspect reference/crosstalk findings.

For a complete subcircuit use `layout__plan_placement` with `mode=systematic`.
The same engine is available as a dry-run `eda__pcb_plan_placement` preview with
`engine=systematic`, an explicit captured `board_context` and `design_intent`.
Declare fixed `anchor_component_ids`, sourced per-component `placement_roles`
and connected `target_pad_ids`. Series/pull/feedback, decoupling, crystal,
oscillator, inductor and diode roles guide proposals; inferred roles remain
structural hypotheses. Optional `preserve_target_distance=true` is an explicit
relative nonregression policy, not an invented datasheet distance.

The four stages bind full subcircuit roles, choose pin-facing poses, refine
scoped weighted HPWL/MST crossings with signal-escape reservations, then try
uniform-pitch/common-angle passive banks. Existing-pose neighborhoods and real
obstacle-edge slots are retained when fixed packages surround an anchor.
All moved pads still pass the full geometry/copper gate. An unchanged obstruction
outside the actual move set is diagnostic, not a claim that the board is clean.
Reported metric nets retain every captured remote endpoint; power/reference
rails are excluded only from the ordinary signal attraction. Explicit
`grid_mm`/`grid_origin_mm` describe the computational grid, not measured native
grid-origin calibration. Banking cannot compensate for a higher-tier regression.
