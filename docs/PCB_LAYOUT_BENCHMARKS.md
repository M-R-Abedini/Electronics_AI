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
rejection or a clearly scoped, authorized trade-off instead of automatic choice.

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
