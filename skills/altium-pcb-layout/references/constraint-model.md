# Constraint and evidence model

Read the current tool schema and its returned contract before preparing data.
The names below describe responsibilities, not additional MCP endpoints.

| Record | Required meaning |
|---|---|
| Board context | Absolute document identity, baseline/revision, units and origin, dirty/coverage state, actual outline/cutouts and relevant stackup, manufacturing and assembly inputs. |
| Design intent | Functional blocks, verified pin-role/pad/net bindings, interfaces, domains and actual current/return relationships. Distinguish sourced facts, caller assertions and inferred hypotheses. |
| Constraint | ID and affected scope, applicability, hard/soft classification, measurement or topology predicate, value/unit/tolerance, source and revision/page, confidence and supported verification. |
| Placement plan | Baseline and input/constraint digests, exact poses/move list, fixed objects, selected candidate, seed/budget, reservations, differences and constraint verdicts. |
| Evidence | Check and object/net/layer IDs, witness geometry, measured/limit/unit, coverage, source snapshot, method and pass/fail/unknown/not_applicable. |
| Gate | Required checks for this scope, accepted server evidence IDs, freshness/dependencies, policy version and unresolved blockers. |

Engineering limits need a manufacturer/fabricator/project source or an explicit
`engineering_policy` label. Seeds, candidate counts and computational budgets
are algorithm settings. Missing insulation, stackup, current or timing inputs
must not become apparently valid zero/infinite/default engineering limits.

Hard requirements must all hold together. If safety, mechanics, electrical or
minimum manufacturing requirements conflict, preserve the conflict and report
infeasible. Recommended and example layouts are evidence for a preference or a
conditional rule, not automatically a hard constraint. An override needs its
actual authority, scope and consequence; never turn a generic user permission
to improve placement into permission to relax a safety requirement.

Unknown is distinct from fail and from not_applicable. Empty findings with an
incomplete geometry reader are unknown. A distance estimate does not establish
loop inductance; a visual match does not establish connectivity. Where a check
cannot be evaluated with the available model, retain the blocker and identify
the data or verification method required to resolve it.

Bind evidence to board, geometry, intent, constraints and applicable rules.
Changes invalidate the checks that depended on them. A report file's checksum
alone is not proof that Altium's unsaved buffers are unchanged.
