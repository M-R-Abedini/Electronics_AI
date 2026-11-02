# Copper-aware SI/PI workflow (0.5.0 candidate)

The existing `eda_agent.design` calculators, classifiers and rule synthesizers
now share a millimetre-based `BoardCopperSnapshot`. The gateway adds four
discoverable capabilities in `si_pi__`; minimal mode still advertises only the
three suite meta-tools. These are engineering checks against the supplied
geometry and constraints, not a field solver or fabrication sign-off.

## Capture and classify

`si_pi__classify_and_partition` accepts an absolute `.PcbDoc` path or an explicit
snapshot. It combines the existing motif, mixed-signal and high-speed modules
with supplied pin roles, topology and net constraints. Classification covers
sensitive analog, switching, high-speed differential/single-ended, current
sense and high-current nets. Unresolved roles remain unknown; the gateway does
not invent IC pin functions or current ratings from a net label.

The saved-file reader uses selective OLE streams. The live handler reads a
matching already-loaded board in one native pass. Automatic capture does not
silently fall back to stale disk data when Altium might hold unsaved edits.
Explicit `source="file"` analyzes saved bytes only. Every snapshot carries
coverage and missing-data fields; unknown record layouts, unsupported pad
stacks, embedded boards or unqualified plane representation prevent a complete
result. Keep the original board open for live capture; the reader does not open
an arbitrary file to make an edit precondition appear satisfied.

## Audit actual copper

`si_pi__audit_copper` evaluates tracks, bounded-error arc segments, via spans,
rotated pad contact and poured region outlines/cutouts. Checks include:

- High-speed and sensitive-analog reference-plane gaps, including the width of
  a track rather than its centreline alone.
- Parallel coupled length and actual edge spacing against 3W/5W policy limits;
  intentional differential partners are treated separately.
- Signal layer transitions without a nearby return via that spans the required
  reference layers.
- Routed decoupling paths, vias between a power pin and capacitor, and hot-loop
  area when a physical terminal cycle is provided.
- Kelvin branch routing and high-current trace/polygon neck-down or via
  capacity when the necessary qualified constraints are available.

Each violation identifies its check, net, layer, `(x_mm, y_mm)`, measured and
limit values, suggested action and geometric evidence. Reports retain explicit
unknowns and coverage; no violations with incomplete coverage is not a clean
design result. `si_pi__focus_violation` accepts a stored report/violation ID and
rechecks the live board before zooming/selecting the affected net. Stale reports
must be regenerated.

## Preview and apply constraints

`si_pi__synthesize_and_apply_rules` defaults to preview. It reuses the existing
impedance and trace-sizing functions, produces net classes, routing widths,
pair constraints, matched lengths and pairwise clearance rules, and returns
the evidence required to review them. Native application requires a separate
explicit apply call with both `expected_snapshot_id` and `expected_preview_id`
from the preview. The preview digest binds the reviewed classification,
synthesized rules, options, partial-coverage choice and source DSN bytes; fresh
live copper alone cannot authorize a different set of rules. The native handler
rechecks the captured geometry before its first mutation, and managed rules and
classes use a reserved ownership namespace.
Caller-supplied or saved-file snapshots cannot authorize a live write.

Widths are derived only for stackup geometries supported by the analytical
models. Asymmetric stripline, unknown dielectrics/reference nets and missing
stackup dimensions remain unsupported. The IPC-2152 path requires a
caller-qualified dataset with source, construction and temperature bounds;
it is not an independently certified implementation of the complete standard.
The older IPC-2221 calculator remains explicitly labelled. Default fabrication
widths and 3W/5W isolation are engineering policies, not manufacturer approval.

Native batches preflight their bounded manifest and report readback/failure.
Differential Pairs Routing rules carry their own width constraints: a separate
Width rule does not set pair width. `pcb_create_design_rule` accepts fractional
`pair_width_min_mils`, `pair_width_max_mils` and `pair_width_preferred_mils`;
supply all three together with min <= preferred <= max. These apply to every
layer and are read back through the typed differential rule alongside its gap.
Gap-only callers retain native width defaults and receive
`pair_width_verified=false`. Synthesis sends the actual qualified impedance
width to the differential rule. Missing or different member/layer geometries
remain incomplete; differing controlled-impedance layer widths require a
future per-layer pair-width API and the differential rule is omitted.
They still do not provide transactional rollback: a mid-handler native error
can leave partial changes. Retain saved checkpoints and inspect any partial
result before retrying. A disk checkpoint does not capture unsaved editor work.

## Constraint-aware routing

For a DSN preview, `si_pi__synthesize_and_apply_rules` returns a compact
`routing_context` containing `snapshot_id` and `preview_id`. Pass it unchanged
as `constraints_context` to `route__route_dsn`, using the original source DSN
reported in `routing_input_path`. The gateway loads checksum-verified private
evidence directly, verifies complete coverage and binds the isolated input to
the reviewed DSN hash. The already-constrained preview copy is for inspection;
it is not accepted as the original routing input. This avoids carrying large
copper JSON through the language model and retains automatic post-SES auditing.

`suite_autoroute` and the SI/PI DSN preview share the Specctra parser and
constraint writer. Source DSN bytes are retained separately from the modified
copy. Class widths, supported layer rules and clearance classes are embedded;
critical routing is protected only when complete copper connectivity and the
input DSN geometry agree. Unrouted sensitive/switching nets require a provider
with an explicit qualified exclusion capability. Unsupported differential
coupling/skew requirements cause a refusal rather than silently degrading to
unconstrained routing.

After SES generation, reconstructed 2D routing is audited before the result is
accepted. Unknown via padstacks/drills and unsupported geometry fail closed.
Altium's native SES-to-board import remains a separate gated adapter; producing
or auditing SES does not claim an imported, DRC-clean Altium board.

## Evidence and limits

Snapshots, previews and reports are checksum-addressed local JSON evidence;
they are not uploaded by these tools. Existing opt-in telemetry policy is
unchanged. Snapshot completeness, physics qualification, router capability,
native readback and full-wave SI/PI validation are separate claims.

Reference material for the caller-qualified current model:
[IPC technical paper](https://www.ipc.org/system/files/technical_resource/E7%26S22_03.pdf)
and [IPC-2152 contents](https://www.ipc.org/TOC/IPC-2152.pdf).

The full regression suites, native acceptance scope and reproducible-package
result for this revision are recorded in `COPPER_SI_PI_REPORT.fa.md` at the
repository root.
