# Placement decisions

Use the existing `eda__pcb_plan_placement` refine/construct engine as a candidate
generator after runtime discovery. Its attraction, grouping, HPWL and objective
terms are useful heuristics; validate the returned candidate with the currently
implemented evidence contract before considering mutation.

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
