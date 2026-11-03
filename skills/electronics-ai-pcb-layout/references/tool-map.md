# PCB tool map

This is a routing guide. `suite_catalog(..., with_schema=true)` is the authority
for the installed connection. Paginate with `limit<=100` and `offset`; use the
returned qualified `name` with `suite_invoke`. Never call an internal unprefixed
Python function as though it were the client's tool name.

| Need | Discover/invoke | Scope and limits |
|---|---|---|
| Nested contracts | `layout__get_contracts` | Read the returned BoardContext, DesignIntent, Constraint and Move schemas; a catalog's generic `object` field is not their full contract. |
| Placement workflow | `layout__get_workflow` | `context_id`; uses the existing session/state machine's placement-review path. Gate evidence and journal statements remain distinct. |
| Capture a placement context | `layout__capture_context` | Explicit board, geometry and fabrication/assembly inputs; inspect coverage and unknown inputs. |
| Produce scoped alternatives | `layout__plan_placement` | `context_id`, current intent/constraints and search options; preserve the baseline and review the actual candidates. |
| Obtain server validation | `layout__validate_plan` | Validate the stored exact `plan_id`; inspect hard/unknown blockers and dependent evidence. |
| Apply the reviewed plan | `layout__apply_plan` | Use the returned `plan_id`, `validation_id` and a stable `operation_id`. No optimizer rerun or blind replay. |
| Retrieve evidence | `layout__get_evidence` | `evidence_id` and `kind` (`contexts`, `plans`, `validations`, `receipts`); retrieval checks stored integrity, not current board freshness. |
| Abandon qualification | `layout__end_managed_scope` | `context_id` and nonblank `reason`; ends the scope without marking its gate passed. Ordinary edits no longer inherit that qualification. |
| Existing candidate engine | `eda__pcb_plan_placement` | Refine/construct candidate generation and objective diagnostics. Native placement edits use stored `layout__` plans. |
| Existing collision diagnostic | `eda__pcb_check_placement_collision` | Same-side AABB, per-object against old positions. Not final-state batch, courtyard, copper or 3D certification. |
| Copper classification/audit | `si_pi__classify_and_partition`, `si_pi__audit_copper` | Explicit `.PcbDoc` or allowed snapshot; inspect source/live state, topology assumptions and physical coverage. |
| Copper rules | `si_pi__synthesize_and_apply_rules` | Preview first; apply requires reviewed preview and live snapshot IDs. Qualified physics and partial-coverage policy are separate. |
| Cross-probe | `si_pi__focus_violation` | Stored report/finding IDs; changes selection/viewport, not copper. |
| Virtual route trial | `eda__route_plan` | Offline unless geometry is fetched; grid/model limitations and actual failure results remain visible. |
| Visual preview | `eda__pcb_render_svg` | Supported visual primitives only; missing pour/body coverage prevents electrical conclusions. |
| Disk recovery | `suite__create_checkpoint`, `suite__restore_checkpoint` | Saved bytes, not a full unsaved-buffer Undo. Restore needs closed Altium. |
| Session orchestration | `eda__design_next_action`, `eda__design_session_log` | Workflow guidance/journal. An `ok` event is not independent gate evidence. |

If a required capability is absent, record the installed version/schema gap and
keep the operation in preview/review. Do not reconstruct an imagined mutation
payload or bypass through `eda__tool_invoke` or raw script tools.

The generated [schema snapshot](tool-map.json) records exact selected inputs,
version/provenance and a catalog digest. Refresh it from a complete exported
catalog with [update_tool_map.py](../scripts/update_tool_map.py). Runtime
discovery remains necessary: the snapshot must not hide a newer valid tool or
advertise a removed one. Source-schema export is not native acceptance.

For the underlying audit, units and source/test files see
[capability inventory](../../../docs/PCB_CAPABILITY_INVENTORY.md).
