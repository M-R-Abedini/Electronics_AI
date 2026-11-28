# Part selection: constraints, evidence, decision

A part number is the end of a selection process, not the start. This file turns
requirements into constraints, ranks candidates, and records why the winner won.
It is deliberately conservative: every parameter must be checked against the
datasheet at the worst-case operating point.

## 1. Turn requirements into constraints

Write the constraints before searching. A useful row has a value, a condition,
and a source.

| Constraint | Example | Source |
| --- | --- | --- |
| Supply range | 4.5-5.5 V, 1 A | system brief |
| Load | 0-1 A, resistive + 10 µF | load spec |
| Ambient | -20 to 60 °C, still air | enclosure spec |
| Lifetime | 10 years, 24/7 | product requirement |
| Compliance | IEC 61010-1 clearance | safety plan |
| Mechanical | SOT-23 or smaller, < 1.1 mm | board outline |
| Lifecycle | active, second source available | purchasing |

Then separate **must-haves** (gate) from **nice-to-haves** (score). A part that
misses a must-have is rejected even if it scores well elsewhere.

## 2. Apply derating

Derating is a design margin, not a datasheet rule. The defaults below are a
starting point; confirm the applicable limits in the datasheet and the standard
your product must meet. If a company or customer standard is stricter, it wins.

| Item | Typical starting point | Notes |
| --- | --- | --- |
| Resistor power | ≤ 50-60% of rated power at max ambient | Check pulse and surge ratings separately |
| Film/ceramic capacitor voltage | ≤ 50-80% of rated | Ceramic capacitance falls with DC bias; use the vendor's bias curve |
| Electrolytic capacitor | ≤ 80% voltage, ≤ 80% ripple current, rated temperature margin | Lifetime halves roughly every 10 °C above rated |
| Diode/BJT/MOSFET voltage | ≤ 70-80% of absolute maximum | Include ringing and reverse recovery |
| Diode/BJT/MOSFET current | ≤ 70-80% of rated at case temperature | Check SOA and pulsed ratings |
| Junction temperature | ≤ 80-90% of TJ(max), or a documented limit | Use the real θJA/thermal model, not the headline number |
| Connector current | ≤ 50-70% per contact | Check mating cycles and temperature rise |
| PCB trace | Within the allowed temperature rise for the copper weight | Use IPC-2221 or the fabricator's table; do not guess |
| Gate drive | Stay inside the datasheet's VGS range with margin | Include supply tolerance and ringing |

**Do not** apply a derating number without recording where it came from. The
table above is a prompt to look it up, not a substitute for the datasheet.

## 3. Generate candidates

Use more than one source:

- **Vendor parametric search** — filter on the hard constraints, then open the
  datasheet of every shortlisted part.
- **Distributor search** — stock, minimum quantity, lifecycle, second source.
- **Reference designs and evaluation boards** — the vendor has already solved
  the bias, decoupling, and layout; check whether their conditions match yours.
- **Application notes** — selection guidance and worked examples.
- **Standards** — when compliance is claimed, the standard defines the test and
  the creepage/clearance rules.

Record the search date and the filters. A candidate list without a date is not
reproducible.

## 4. Score the survivors

Use a weighted matrix. Keep it small enough to explain.

| Criterion | Weight | Part A | Part B | Part C |
| --- | --- | --- | --- | --- |
| Meets all must-haves | gate | yes | yes | no |
| Electrical margin at worst case | 25% | 4 | 3 | — |
| Thermal margin | 20% | 3 | 4 | — |
| Package and area | 15% | 4 | 2 | — |
| Availability and second source | 15% | 3 | 5 | — |
| Cost at volume | 10% | 4 | 3 | — |
| Documentation and models | 10% | 3 | 4 | — |
| Compliance evidence | 5% | 3 | 3 | — |
| **Weighted total** | | **3.55** | **3.45** | rejected |

The score does not make the decision; it exposes the trade-off. Write the
rationale and the conditions under which you would revisit it.

## 5. Verify the winner against the datasheet

For every selected part, check:

- **Absolute maximum ratings** — voltage, current, power, temperature, ESD.
- **Recommended operating conditions** — the range where the part is specified,
  not just where it survives.
- **Electrical characteristics at your operating point** — min/typ/max, not
  only typical, and at your temperature and supply.
- **Thermal** — θJA with the actual copper area, θJC, power dissipation, TJ.
- **Dynamic behaviour** — capacitance, gate charge, switching times, reverse
  recovery, stability, slew rate, output swing.
- **Package and pinout** — footprint, pitch, thermal pad, pin 1, creepage.
- **Lifecycle** — active, NRND, obsolete; last-time buy; second source.
- **Documentation** — errata, app notes, reference layout, SPICE model
  provenance.

If the datasheet does not state a number you need, measure it or choose a part
that does. Do not fill the gap with a plausible value.

## 6. Worked example: low-side N-MOSFET switch

Requirements: 5 V rail, 2 A resistive load, 20 kHz PWM, 40 °C ambient, still
air, board area allows SOT-23 or PowerPAK.

1. **Voltage**: allow for inductive kick and ringing. A 30 V part at 5 V is
   comfortable; check the actual drain ring on the bench.
2. **Current**: 2 A continuous. Apply the derating rule from the datasheet and
   the package thermal limit.
3. **Conduction loss**: `Pcond = I² × RDS(on) × D`, with `RDS(on)` at the gate
   voltage and junction temperature you will actually have. Datasheet
   `RDS(on)` at 25 °C is optimistic; use the hot value.
4. **Switching loss**: estimate from gate charge and switching times, or use the
   vendor's loss calculator with your conditions. At 20 kHz it may be small; at
   500 kHz it dominates.
5. **Gate drive**: check total gate charge, drive current, and the driver's
   output impedance. A weak driver turns a good MOSFET into a heater.
6. **Thermal**: `TJ = TA + (Pcond + Psw) × θJA`. Use the θJA for your copper
   area, or a thermal model. Check the SOA for startup and short-circuit.
7. **Body diode**: check reverse conduction, dead time, and whether the load is
   inductive.
8. **Decision**: record the shortlisted parts, the hot `RDS(on)`, the loss
   estimate, the package, the second source, and the source URLs.

If the numbers do not close, change the architecture (parallel devices, a
different package, a gate driver, or a different topology) rather than ignoring
the margin.

## 7. Red flags

- A part chosen because it appears in a tutorial, without checking conditions.
- "Typical" used where the design needs min or max.
- `RDS(on)` or saturation voltage at 25 °C only.
- Capacitor value at 0 V DC bias, or at 25 °C only.
- θJA from the front page instead of the board-dependent test condition.
- No second source for a long-life product.
- A model with no vendor, part, source, or hash.
- A datasheet revision older than the errata or the current revision.
- A compliance claim with no standard, test level, or report.

## 8. Decision record

Use `templates/decision-record.md`. At minimum:

- the requirement IDs the decision serves;
- the alternatives and why each was rejected;
- the operating point used for the comparison;
- the sources (datasheet revision, app note, measurement);
- the residual risk and the trigger to revisit.

A decision record is not a vote. It is the evidence a reviewer needs to disagree
with you productively.
