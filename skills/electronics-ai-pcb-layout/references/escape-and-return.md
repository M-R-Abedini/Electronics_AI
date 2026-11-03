# Escape, channels and return paths

Use actual pad shapes, rotations and board-side transforms. Declare origin and
units; grid checks use displacement from the declared origin with an explicit
tolerance. Distinguish pad-edge clearance, body clearance, routed path length,
via span and complete forward/return geometry.

For an explicit channel, record its layer, endpoints, obstructions, width and
spacing rules, pad/via/antipad access and reserved nets. For N equal-width tracks,
`N*w + (N-1)*s + 2*m` is only a cross-sectional lower bound. It does not prove
turns, fanout, layer transitions, differential topology or end access. Preserve
unknown routing/obstacle coverage rather than declaring a channel routable.

Feed supported virtual trial fanout/routing results back into candidate choice.
Missing native import support does not prevent an offline trial, but limits its
claim. A generated DSN/SES or an unconstrained straight path is not a completed
Altium route. Keep routing conditions and budgets equal when comparing options.

Map decoupling to the served power pad/pin group and its correct return. Plane
access and via topology matter; component-center proximity or an arbitrary via
nearby does not establish a low-inductance loop. Verify via net and layer span,
reference continuity and actual poured copper rather than a polygon intention.
Treat stale pours, unsupported holes or a reference gap as missing evidence for
the dependent check. Do not infer inductance or PDN impedance without a
qualified model.

For interface protection, follow the incoming current path through the selected
protection topology and its intended discharge reference. Avoid bypass stubs;
check both signal and return paths. This conditional review follows TI's
[ESD Protection Layout Guide, SLVA680A, sections 2.1-2.4](https://www.ti.com/lit/an/slva680a/slva680a.pdf)
(April 2022). It does not select a TVS, establish a ground/chassis architecture
or prescribe a universal connector spacing.

For physical findings, reuse `si_pi__audit_copper` and its explicit coverage;
cross-probe a stored finding with `si_pi__focus_violation`. Classification alone
does not prove the route or reference. [Verification](verification.md) separates
geometry, native DRC and simulation evidence.
