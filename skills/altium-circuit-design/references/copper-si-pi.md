# Copper-aware SI/PI workflow

Discover the current `si_pi` backend through `suite_catalog` with schemas.
Use one explicit absolute `.PcbDoc` path throughout. `source=auto` prefers the
loaded board; `source=file` describes saved bytes and cannot authorize edits.
Caller-supplied snapshots are analysis inputs, never live-write preconditions.

1. Use `si_pi__classify_and_partition` with the existing DesignPlan or verified
   `topology.net_roles`/`pin_roles`. Record explicit shunt Kelvin terminals,
   local-decoupling pin mappings, ordered switching-loop terminals and named
   differential pairs when relevant. Unknown IC functions stay unknown; labels
   such as USB/MIPI do not establish package pin functions or a stackup.
2. Supply source-backed impedance/current/length constraints for actual net
   names. Targets may be `target_z0_ohm` (single ended) or
   `target_zdiff_ohm` with pair spacing. Stackup height/Er/reference evidence
   must support the selected model. IPC-2152 sizing requires a caller-qualified
   table including source, board conditions, copper/temperature/layer and
   bounded current-width points; no table is bundled or certified by the tool.
3. Run `si_pi__audit_copper`. Retain report/snapshot IDs, coverage, unknowns and
   violation coordinates. `status=unknown` is not a clean board. Adjacent-layer
   3W/5W checks are projected geometry heuristics, not electromagnetic coupling
   simulation. Use `si_pi__focus_violation` with report and violation IDs to
   cross-probe; regenerate a stale report rather than bypassing its guard.
4. Preview `si_pi__synthesize_and_apply_rules` using the intended topology,
   constraints and options. Review rules and coverage. Within authorized PCB
   editing work, apply with `apply=true` and both returned
   `expected_snapshot_id` and `expected_preview_id`, preserving the reviewed
   inputs. Changed inputs require a new preview. `allow_partial=true` is an
   explicit choice of incomplete rule coverage, not an SI/PI pass.
5. Native class/rule readback and `possible_partial_native_edit` are separate
   from copper audit and native DRC. On partial failure inspect the board and
   retained checkpoint; do not blindly retry or claim transactional rollback.
   Disk checkpoints do not include unsaved work.

For routing, the preview can write a constrained private DSN. Pass the returned
synthesis plus its matching complete snapshot as `constraints_context` to
`route__route_dsn`. Existing critical copper needs exact DSN agreement and a
complete connectivity proof; otherwise a qualified exclusion-capable provider
is needed. Coupling/skew extensions require explicit provider enforcement
acknowledgement. A reconstructed SES must pass the automatic copper audit.
Unsupported arc/region reconstruction or unknown via padstacks refuses the
result. Automatic native SES import still needs a tested adapter; changing its
extension to RTE does not translate it. See [routing limitations](../../../docs/AUTOROUTE.md).

Retain editable designs, engineering sources, audit findings and actual native
DRC evidence. The copilot does not establish manufacturing readiness or replace
field simulation and hardware validation. Implementation and coverage are in
[copper SI/PI documentation](../../../docs/COPPER_SI_PI.md).
