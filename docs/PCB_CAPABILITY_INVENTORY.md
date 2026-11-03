# PCB capability inventory

Initial audit: 2026-11-02, baseline checkout `3f2effa`, plugin manifest `0.5.1`.
The development prompt is a capability-development request, not authorization to
rearrange a production board. ADS1262, Pt100 and U12 are examples, not a default
board architecture.

This inventory was made from source and tests. The author's inspection did not
invoke Altium tools. Installed package/catalog, checkout version, native script
declaration and actual native execution are distinct evidence. The baseline
native declaration was `2026.11.01.field-fixes1`; read the current declaration and
live health after deployment. New runtime acceptance and benchmark measurements
belong in the implementation report, not in this static inventory.

The lean contract is `suite_status`, paginated `suite_catalog` with schemas and
`suite_invoke`. Schema `limit` is 1-100 and offset is nonnegative. Client-qualified
names below are derived from the suite's namespace routing. The maintained
[skill tool map](../skills/electronics-ai-pcb-layout/references/tool-map.md) and generated
JSON snapshot complement runtime discovery.

| Need | Real tool/function and schema summary | Units/source/backend/effect | Geometry, limit and inspected tests |
|---|---|---|---|
| Placement candidates | `eda__pcb_plan_placement`: `designators`, `fixed`, `region`, `engine`, iterations/grid/clearance, critical nets/weight, groups, `plan_json`, restarts, rendering and dry-run/apply selector. | mil/degrees; `eda-agent/src/eda_agent/tools/pcb.py::pcb_plan_placement`; EDA, reads live board then deterministic computation; legacy apply path is a write. | Refine/construct exist. Group attraction, bounding regions, wirelength and structural decoupling are heuristics. `eda-agent/tests/test_pcb_placement.py`, `test_pcb_plan_placement.py`, `placement/test_autoplace.py`. Existing scalar legality is not a complete physical gate. |
| Candidate scorer | `design/pcb_placement.py::score`, `ObjectiveWeights`, `ratsnest_crossings`, `decoupling_report`; `placement.py::plan_placement`. | mil/degrees and documented objective terms; deterministic Python, no CAD mutation. | Reuse seeded engines, HPWL, grouping, overlap/edge/congestion terms. `_build_objective_report` in the old tool path presents all components on the top side. Crossings and layer-split counts do not prove required vias. |
| Functional groups | `placement_constraints.py::infer_placement_constraints`, `mixed_signal`, `motifs`, `diffpairs`, HSD modules. | DesignPlan net-role/group mappings; Python, read/derive. | Inferred roles are hypotheses; explicit groups override inferred tags. Supports composing analog/digital separation and matched groups but does not establish package/source correctness. `eda-agent/tests/design/test_placement_constraints.py`, mixed-signal/motif/HSD tests in that directory. |
| Collision diagnostic | `eda__pcb_check_placement_collision(designator,x,y,rotation,margin_mils)`. | mil/degrees; `tools/pcb.py`, `PCB.pas`; EDA/native read. | Same-side AABB against old component positions; quarter turns swap bbox dimensions, other angles approximated. No courtyard/pad/height/concave-outline proof. Cannot alone validate a legal swap or final group state. |
| PCB geometry | `eda__pcb_capture_snapshot`, shared read helpers; `generic.get_pcb_geometry` is an internal bridge command, not a public qualified MCP tool. | Generic payload mil, precision declaration 0.0001mil; `Generic.pas`, `suite_reads.py`, `suite_pcb_primitives.py`; read/file/native as declared. | Preserve actual floating pad coordinates. Snapshot source and buffer consistency are explicit. `tests/test_shared_reads.py`, `test_geometry_precision.py`. |
| Full copper evidence | `suite_reads.py::capture_copper_snapshot`, `design/copper_snapshot.py::BoardCopperSnapshot`, private `pcb.get_copper_snapshot`. | mm, schema 1; native loaded-target or independently justified file read; no design write. | Tracks/arcs/vias/pads/regions/keepouts/net ties and stackup with explicit gaps; file reader is not proof of unsaved buffers. Loaded/uncertain live failures refuse hidden disk fallback. `tests/test_copper_snapshot.py`. |
| Physical intent and audit | Four `si_pi__` tools declared by `suite_si_pi.py::list_tools`: classification/audit/rules share `board_path` or `snapshot`, plan/topology/constraints and source. | mm in copper evidence; Python+EDA; classify/audit read, focus changes view; rule apply writes. | Reuses `design/copper_audit.py`, `copper_rules.py`, signal-integrity/current models. Completeness and qualification are separate. Coordinates/finding geometry are not full-wave SI/PI or safety certification. `tests/test_copper_si_pi.py`, `test_si_pi_service.py`, `test_copper_rules_routing.py`. |
| Rule preview/apply | `si_pi__synthesize_and_apply_rules` with `apply`, `expected_snapshot_id`, `expected_preview_id`, qualified options and partial policy. | Source-aware rule units/stackup; `suite_si_pi.py`, native `PCB_ApplyCopperRules`; bounded write. | Stored preview, fresh same-handler native copper proof and ownership checks precede mutation. No transactional rollback. `tests/test_differential_rule_width.py`, SI/PI service tests. |
| Routing trial | `eda__route_plan(geometry,rules,nets,net_classes,grid_pitch_mils,...,fetch_geometry)`; `suite_autoroute.py` local DSN/SES orchestration. | mil for grid route, file units as declared for Specctra; Python, offline unless fetching/exporting/importing. | Grid A* failures are reported; DSN constraints and post-session auditing exist. Native SES import is gated. An SES is not imported, completed Altium routing. Route/model tests and `tests/test_copper_rules_routing.py`. |
| Renderer | `eda__pcb_render_svg` path/layers/margin/flip/drill/text/legend options. | SVG bbox in mil; `tools/render.py`, SVG renderer; live read plus output artifact write. | Regions/pours and full body outlines are omitted in baseline docstring. Rendering and copper-reader coverage must remain separate. SVG/render tests. |
| Session workflow | `eda__design_session_log` accepts `stage_result` and caller `status`; `design_next_action` uses `design/state_machine.py`, `MAX_STAGE_ATTEMPTS=3`. | Journal/stages, no geometric unit; Python; journal write, advisory action. | Caller success is not independently verified. Existing playbooks and retry policy can be extended; their presence does not prove all dispatches are gated. Session/state-machine tests. |
| Checkpoints | `suite__create_checkpoint`, `list_checkpoints`, `restore_checkpoint`; gateway automatic native-write checkpoints. | Saved file bytes/digests; `suite_checkpoints.py`, `suite_stdio.py`; local checkpoint/restore writes. | No forced save, no complete unsaved recovery, no transactional native batch. Restore requires closed Altium. Opaque effects require explicit target list; standalone external connections are outside the gateway. Checkpoint/gateway tests and `docs/PRODUCT_SAFETY.md`. |

## Development integration boundary

The new `layout__` context/plan/validation/apply/evidence workflow connects the
existing candidate engines to explicit intent, package geometry, hard/unknown
checks and stored-plan mutation. Consult its actual exported schemas and tests;
the conceptual names in the supplied prompt were not pre-existing APIs.

Package/courtyard or manufacturing data supplied by a caller remain declared
inputs until their source is established; a data schema cannot authenticate a
datasheet. A gate may only claim the checks its model and geometry cover.
Raw/legacy/nested dispatch boundaries must be reported precisely rather than
implying that every external CAD client is controlled by this gateway.

## Claims to correct and acceptance gaps

- Straight airwire crossings do not force a via; lower HPWL/crossing counts do
  not establish escape, loop quality or routing completion.
- Old per-object AABB collision checks are not a final-state batch validator or
  a real courtyard/body/pad/3D checker.
- A journal's `stage_result=ok` and state-machine prose are not an enforced
  evidence gate. A newly protected workflow must identify which effects and
  nested/raw paths it actually controls.
- Existing saved-byte snapshots do not justify unsaved rollback or automatic
  restoration while Altium remains open.
- The U12 netlist, original layout, expert constraints and quoted metrics were
  not supplied as executable artifacts. Do not present them as a regression or
  calibration result. See [benchmark protocol](PCB_LAYOUT_BENCHMARKS.md).
