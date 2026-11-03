# PCB placement benchmark protocol

Evaluate the capability as a general decision system. Keep fixture construction,
selected constraints and benchmark interpretation separate. This protocol is not
itself an executed benchmark result; record actual commands/artifacts/counts in
the implementation report.

| Fixture family | Exercise | Required evidence and intentional uncertainty |
|---|---|---|
| Precision analog | A converter/reference/input block with explicitly bound roles; compare a same-side option against an allowed bottom decap option, and a short cap center distance with a wrong/long return. | Actual pad transforms and role mappings; return topology and reference coverage. Unknown thermal/leakage models remain unknown. No ADS1262-specific coordinates or universal cap-side rule. |
| Switching power | Declared buck switching states with commutation, input, gate/sense and feedback relationships; compare visually compact and electrically protected alternatives. | Topology-derived loop evidence, current/thermal bounds and sensitive-victim separation. An inductor/load path is not automatically the hot loop. |
| Digital interface | Verified driver/receiver or pair pins, connector access and stackup/reference obligations. | Width/gap/timing inputs and pad-facing/escape; no interface-limit inference from net name. |
| Dense escape | Concave outline/cutout, narrow layer channel, pad obstructions and legal group swap. | Per-layer cross-sectional lower bound, endpoint access and virtual route results when supported. Fewer ratsnest crossings must not hide a failed critical escape. |
| Mixed isolated | Analog/digital or power scopes with an isolation overlay, approved component crossing and forbidden copper/return bridge. | Domain geometry, applicable safety inputs and explicit unsupported creepage/3D checks. Missing safety evidence blocks the dependent scope only. |

Include a professional existing baseline and a small-ECO case. Baseline retention
is a valid result; score improvement alone is not success. Exercise a candidate
with lower HPWL/crossings but a worse critical path or escape, and require
rejection. Under the P0 policy, no positive regression of an individual critical pair or decoupling VCC/GND leg is allowed; a caller cannot bypass this by supplying a positive tolerance.

For reproducibility, publish fixture/input hashes, source/schema/policy versions,
seeds, candidate/time/iteration budgets, transforms/units and selected plan IDs.
Use independent seeds and held-out fixtures for any solver-improvement claim;
do not report gains only on the tuning set. Keep raw metrics independently:
legality, hard/unknown counts, critical path/loop evidence, congestion and escape,
movement outside scope, and route completion/via/DRC results where measured.

For native acceptance use explicit scratch files or a test copy. Verify target,
staleness refusal, repeated operation IDs, partial receipts and final readback.
Do not change a production board for benchmark convenience. Native export/import,
a PNG/SVG or an SES file does not establish completed routing or native DRC.

An expert-layout comparison needs the actual artifacts and their constraints,
not a screenshot or quoted coordinate/wirelength figures. Compare engineering
outcomes under equal routing conditions; pixel or coordinate similarity is not
the target. If expert review or native Altium is unavailable, mark that acceptance
unexecuted and state exactly which input or capability would close the gap.

## Executed synthetic decision benchmark

Run from the repository root:

```powershell
.\.venv\Scripts\python.exe scripts/benchmark_pcb_layout.py
```

The measured output is [PCB_LAYOUT_BENCHMARK_RESULTS.json](PCB_LAYOUT_BENCHMARK_RESULTS.json).
It contains 15 primary cases: the five families above with computational seeds
`211,307,401`, kept separate from development seeds `0,1,7,8`. Each case also
exercises one proposal from the existing refiner (8 iterations). Extra dense
package-unknown and scoped-ECO cases, plus an independent ECO on the mixed
isolated board, are recorded. All inputs are synthetic; no production PCB is
loaded or edited.

The script executes the complete case set twice and compares fixture hashes,
selection, metrics, evidence/plan digests and artifact byte sizes. Timings are
excluded from equality. It also refuses a source change during execution.
`--seeds`, `--iterations`, `--output` and optional `--artifacts-dir` are actual
CLI parameters. Full comparison reports remain optional artifacts; the checked
JSON records their measured sizes without duplicating every geometry witness.

The checked run uses `placement-evidence-p0-v4` and the environment recorded in
the JSON. Both passes completed in 3.51 seconds; all assertions passed. Source
hashes include the P0 metric module as well as the geometry and solver modules. The table shows the median
primary-decision time across the three seeds, the largest full primary report,
and the maximum total check count across that decision's candidates.

| Family | Required observed decision | Median ms | Max report bytes | Checks |
|---|---|---:|---:|---:|
| Precision analog | Reject lower ordinary-net HPWL when the transformed-pad AABB Manhattan gap regresses | 8.388 | 21,114 | 29 |
| Switching power | Reject a compact placement with a synthetic courtyard collision | 3.794 | 19,323 | 23 |
| Digital interface | Preserve baseline over an unnecessary no-op reapply; retain declared sides/origin | 3.404 | 17,692 | 21 |
| Dense escape | Reject a shorter link that blocks the channel and a move across a cutout; accept the legal scoped ECO | 106.212 | 115,726 | 203 |
| Mixed isolated | Block missing safety/unsupported creepage evidence in the dependent scope; permit the independent ECO | 5.596 | 29,179 | 47 |

The dense fixture has 10 components and 200 real synthetic pad rectangles; its
outline is concave and includes a cutout. Cold imports and machine load affect
timings, and the first precision case includes startup work. These are measured
smoke/regression timings, not a latency guarantee or a solver-superiority claim.

The seeds hold out computational starts only: there is no expert-layout dataset,
native placement/routing run, native DRC, field simulation, measured via
optimization or routing-completion comparison here. Reported HPWL, transformed-pad AABB Manhattan gaps
and channel cross-section capacity remain proxies or lower bounds. A
professional-baseline comparison still requires the actual expert artifacts
and matched constraints described above.


## P0 completeness and stationary copper regressions

The focused regression suite also covers omitted/free copper pads and incomplete
snapshot sections. A fixed pad without a component owner must still obstruct
manufacturing clearance and reserved channels; it cannot be silently dropped or
turned into a movable fictional package. Incomplete pad, board-outline or cutout
extraction must produce a required unknown. Conversely, explicitly complete
placement geometry is not invalidated merely by an unrelated unknown stackup.
These checks are in `tests/test_layout_review_regressions.py` and
`eda-agent/tests/design/test_placement_evidence.py`; they are separate from the
15-case timing table. The focused placement/P0/protocol run passed 92 tests.
This is not a full-suite or live-native acceptance claim.


A live read-only capture exposed a semantic counterexample also covered by
synthetic regressions: layer-specific `polygon_cutout` records are pour voids,
not physical FR4 holes. Only `board_cutout` is mapped into the mechanical model;
all raw records remain in the copper snapshot. Unknown cutout kinds remain
required unknown. Edge notches, intersecting physical cutouts or another
unsupported composed board contour are retained as raw evidence and produce
unknown mechanical coverage; they are not silently treated as intact FR4.
Exact coincidence of a courtyard with a physical hole also fails containment.


Physical-hole overhang permission is intentionally conservative: when a
courtyard overlaps a hole, one sourced allowance must contain the whole hole.
An unrelated or partial-hole allowance cannot pass an exact-boundary coincidence.
The current verifier does not prove clipping of the courtyard/hole intersection
or combine several partial permissions into a whole-hole grant. Focused
placement/P0/protocol/cutout regressions passed 104 tests after this correction.
