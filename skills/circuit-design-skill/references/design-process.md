# Design process: plan, choose, verify

A circuit is not a netlist that runs. It is a set of requirements, an
architecture, a set of part decisions, and evidence that each decision holds
under the stated conditions. This file is the workflow the skill expects before
and around schematic generation.

The ordering matters. Most expensive mistakes are made before the first
schematic: an unstated requirement, an architecture that cannot meet the
budget, or a part chosen from memory rather than a datasheet.

## The four rules

1. **No requirement, no design.** Write down what the circuit must do, under
   what conditions, and how it will be accepted. "12 V in, 5 V out" is not a
   requirement; "5.0 V ±2% at 0-1 A, 0-40 °C, <50 mV ripple" is.
2. **No source, no claim.** Every critical number — absolute maximum, thermal
   resistance, tolerance, timing, derating rule — must be traceable to a
   document (see `electronics-research.md`). Your memory of a part is a
   hypothesis, not evidence.
3. **No decision, no part.** Record why this part and not the alternatives.
   A selection matrix plus a short decision record is enough; a part number
   with no rationale is not.
4. **No test, no pass.** A simulation is evidence about the model, not the
   board. A check that did not run is `SKIP`, not `PASS`.

## Phase 0 — Frame the problem

Produce a one-page brief before touching a schematic:

- **Function** — what the circuit does, and what it explicitly does not do.
- **Inputs and outputs** — ranges, impedance, rate, accuracy, isolation.
- **Environment** — ambient temperature, airflow, humidity, vibration, EMI,
  altitude, ingress, duty cycle.
- **Supplies** — available rails, tolerance, transient range, current limit,
  sequencing, reverse polarity.
- **Mechanical** — board size, connectors, height, mounting, thermal paths.
- **Lifecycle** — prototype quantity, production volume, expected lifetime,
  second-source requirement, compliance marks.
- **Acceptance** — the measurements that decide pass/fail, with limits and
  conditions.

If a requirement is unknown, write it as an assumption with a risk and a plan
to close it. Do not silently pick a number.

## Phase 1 — Research before architecture

Collect, for the candidate technologies and parts:

- the datasheet (revision and date);
- the relevant application note or design guide;
- errata and known issues;
- the reference schematic or evaluation-board schematic;
- the applicable standard, if compliance is claimed;
- a SPICE model only with provenance (`models.md`).

Use `electronics-research.md` for the source hierarchy and search queries.
Record every source in the design record as you go, not at the end.

## Phase 2 — Architecture

Draw a block diagram with every interface labelled: voltage, current, impedance,
direction, reference, and ground return. Then write an interface contract for
each boundary.

### Architecture patterns and trade-offs

| Choice | Use when | Main trade-off | Verify with |
| --- | --- | --- | --- |
| Linear regulator | Low noise, low dropout headroom, small load | Efficiency, heat | Power dissipation, θJA, max TJ |
| Switching regulator | Wide input/output ratio, higher current | EMI, ripple, layout sensitivity | Loop stability, ripple, thermal, EMI |
| Low-side switch | Load can float, N-channel device | Load is not ground-referenced | Gate drive, inductive kick |
| High-side switch | Load must stay grounded | Gate drive above supply | Bootstrap/charge pump, SOA |
| Single-ended signal | Short, low-noise, shared ground | Ground shift, crosstalk | Return path, CMRR |
| Differential signal | Long cable, low level, noisy environment | Complexity, matching | CMRR, impedance, termination |
| Star ground | Small return currents, sensitive analog | Wiring complexity | Return currents, IR drop |
| Ground plane | Mixed digital/analog, RF | Split placement discipline | Current paths, stitching |
| Isolated interface | Safety, ground loop, high common mode | Cost, space, delay | Isolation rating, creepage, CMTI |

Do not select a pattern because it is familiar. State the constraint that
forces it, and the alternative you rejected.

### Budgets

Calculate, do not estimate. Put the numbers in the design record.

- **Power**: `P = V × I` for each rail and each dissipating part; sum with
  efficiency and quiescent current. Include startup and fault conditions.
- **Thermal**: `TJ = TA + P × θJA` (or the datasheet's thermal model). Check
  the actual copper area and airflow; θJA is board-dependent.
- **Signal**: divider tolerance, amplifier offset and bias current, ADC
  reference error, noise bandwidth, settling time.
- **Timing**: propagation delay, rise time, clock tolerance, setup/hold,
  watchdog and startup sequencing.
- **Layout**: current density, creepage/clearance, loop area, thermal relief.
- **Cost and area**: BOM at volume, board area, connector and harness cost.

Every budget needs a source for each input and a margin. A budget without a
margin is a wish.

## Phase 3 — Part selection

Follow `part-selection.md`. The short version:

1. Convert requirements into hard constraints (voltage, current, temperature,
   package, compliance, lifecycle).
2. Generate candidates from vendor parametric search, distributor stock, and
   reference designs.
3. Gate on must-haves, then score the survivors on weighted criteria.
4. Verify the winner against the datasheet at the actual operating point.
5. Record the decision and the rejected alternatives.

## Phase 4 — Schematic and layout gates

Before generating files:

- every part has a model or an explicit "generic placeholder" status;
- every net has a driver and a return path;
- every connector has protection, polarity, and a pinout;
- every IC has decoupling, a defined logic level, and a defined reset state;
- test points exist for the rails and the critical signals;
- the schematic and the spec agree (run `verify.py`).

Before release:

- ERC/DRC evidence is attached, or the absence is stated;
- the BOM has footprints, lifecycles, and second sources;
- thermal and tolerance budgets are re-checked with the chosen parts;
- the bring-up plan and the acceptance measurements are written.

## Phase 5 — Bring-up and release

1. Inspect the bare board (shorts, orientation, footprint).
2. Power with a current-limited supply, no load, and measure the rails.
3. Bring up one block at a time; record every measurement against its limit.
4. Compare against the simulation; explain every difference.
5. Freeze the design only when the acceptance criteria are met and the
   remaining risks are accepted explicitly.

## Review checklist

Use this at each gate. A "yes" without evidence is a "no".

- [ ] Requirements have IDs, conditions, and acceptance criteria.
- [ ] Each requirement maps to a block or a test.
- [ ] Every interface has a voltage/current/reference definition.
- [ ] Every critical part has a datasheet revision and a source in the record.
- [ ] Derating is applied at the worst-case operating point, not nominal.
- [ ] Thermal, tolerance, and timing budgets are written down.
- [ ] High risks have a mitigation and a verification.
- [ ] Alternatives were considered and the reason for rejection is recorded.
- [ ] Simulation status and model provenance are stated.
- [ ] ERC/DRC and bench evidence are labelled with what they do and do not prove.

## Outputs

- a filled `templates/design-plan.md`;
- one `templates/decision-record.md` per significant choice;
- a `templates/research-log.md` with sources and dates;
- a spec `design` object, so `verify.py --require design` can gate CI.

The design record is not paperwork. It is the difference between "it worked on
my bench once" and a design someone else can review, reproduce, and maintain.
