# Design intake

Use conversational questions, one important unknown at a time. Ask about assembly preference (through-hole/SMD), DIY soldering ability, board size, layers, connectors, mounting and cost when relevant. Common non-critical defaults may be proposed but must be recorded; never guess safety-critical, electrical, pinout or mechanical requirements.

Ask before designing. A circuit is a set of coupled trade-offs, and the
"obvious" values are often wrong by an order of magnitude. The questions below
are grouped so a user can answer quickly, and each one maps to a field in
`schemas/design-spec.schema.json`.

If the user cannot answer a question, propose a default **and record it in
`assumptions`**. Never leave a hidden assumption in the netlist.

## 0. Defaults confirmation gate

Before creating specs, handoffs, or generated artifacts, restate the locked
defaults and assumptions and get explicit confirmation (e.g. `defaults
confirmed`) or a list of changes. Do not silently lock revision/topology,
power, bypass, build form factor, anchor source, or unverified substitutions.

## 1. Function and topology

| Question | Why it matters | Spec field |
|---|---|---|
| What must the circuit do, in one sentence? | Drives topology and verification | `description` |
| Is it analog, digital, mixed-signal, or power? | Selects analysis and models | `topology` |
| Which topology do you expect? | Avoids inventing a novel circuit | `topology` |
| **If this is a clone/variant family, which exact revision/topology is the target?** | Values, buffering, bypass, and behavior vary by revision | `description`, `design.requirements` |
| **Primary anchor source** (schematic/analysis/datasheet link or name)? | Locks evidence; conflicts must be recorded, not averaged | `design.sources`, `design.decisions` |
| **Known conflicts between sources** and which one wins? | Prevents mixing incompatible revisions/values | `design.risks`, `design.decisions` |
| **Allowed substitutions** (parts, values, features, tolerances)? | Controls what “equivalent” means | `assumptions`, `design.decisions` |
| Is there an existing schematic, netlist or reference design? | The existing netlist wins over a guessed topology | `components` |
| What is the input signal: source, amplitude, impedance, bandwidth? | Sets drive, noise and stability requirements | `input` |
| What is the load: resistance, capacitance, current, inductive? | Sets output stage and stability | `output.load` |
| Must it be inverting, non-inverting, differential, isolated? | Topology choice | `topology`, `parameters` |

## 2. Supplies and environment

| Question | Why it matters | Spec field |
|---|---|---|
| Supply voltage(s) and tolerance? | Headroom, logic levels, derating | `supplies` |
| Supply source: battery, adapter, USB, bench? | Ripple, transient and protection | `supplies` |
| Maximum supply/input transient? | TVS, clamp and voltage rating | `environment` |
| Ambient temperature range and airflow? | Thermal and derating | `environment.ta_c` |
| Maximum junction temperature? | Thermal budget | `environment.tj_max_c` |
| Vibration, humidity, altitude, conformal coating? | Mechanical and creepage | `environment` |
| EMC/ESD environment? | Filtering and protection | `environment` |

## 3. Signals and performance

| Question | Why it matters | Spec field |
|---|---|---|
| Signal frequency range and required flatness? | GBW, SR, filter order | `parameters` |
| Required gain (linear or dB) and accuracy? | Resistor tolerance, op-amp offset | `parameters` |
| Output voltage/current swing? | Headroom and dissipation | `parameters` |
| Load regulation / line regulation? | Feedback loop and reference | `parameters` |
| Input and output impedance? | Loading and divider errors | `parameters` |
| Noise limit (input-referred or output)? | Op-amp and resistor noise | `parameters` |
| Distortion limit (THD, THD+N)? | Loop gain and output stage | `parameters` |
| Is this audio or sound equipment? | Take audio targets, conditions and measurement method from `audio-engineering-skill`; do not invent them here | `design.requirements` |
| Settling time or transient response? | Loop bandwidth and SR | `parameters` |
| Common-mode range? | Instrumentation topology | `parameters` |

## 4. Digital and interface

| Question | Why it matters | Spec field |
|---|---|---|
| Logic family and supply (3.3 V CMOS, 5 V TTL, 1.8 V)? | Noise margin and drive | `parameters` |
| Clock/data rate and bus topology? | Timing, termination, pull-ups | `parameters` |
| Number of loads and trace length? | Fanout, reflections, capacitance | `parameters` |
| Interface standard (I2C, SPI, UART, USB, CAN, Ethernet)? | Standard limits are hard gates | `parameters` |
| Required isolation? | Galvanic isolation and creepage | `topology` |
| Power-up / power-down sequencing? | Bus contention and latch-up | `parameters` |

## 5. Power and protection

| Question | Why it matters | Spec field |
|---|---|---|
| Output voltage, current and ripple? | Converter sizing | `parameters` |
| Efficiency target? | Loss budget and thermal | `parameters` |
| Switching frequency or noise-sensitive bands? | EMI and filter design | `parameters` |
| Input reverse polarity, overvoltage, overcurrent, ESD? | Protection devices | `parameters` |
| Battery chemistry, capacity, cutoff? | Charging and protection | `parameters` |
| Isolation voltage? | Creepage/clearance | `environment` |

## 6. Deliverables and verification

| Question | Why it matters | Spec field |
|---|---|---|
| Which files: SPICE, LTspice `.asc`, KiCad `.kicad_sch`, `.net`, BOM, report? | Output selection | `deliverables` |
| Which analyses: OP, AC, transient, DC sweep, noise, transfer function? | Directives in the netlist | `analyses` |
| Are vendor SPICE models available? | Generic models are unverified | `models` |
| Which simulator/version? | Syntax and model compatibility | `parameters` |
| Is a physical board expected? | Hand off to `pcb-design-skill` | `pcb` |
| Board size, layer count, thickness, copper weight, finish? | Mechanics and rules | `pcb.board` |
| Which PCB profile (2-layer low speed, 4-layer impedance, RF, HV, flex)? | Gates and rules | `pcb.profile` |
| Real footprint for every part? | Logical pins are not pads | `pcb.footprints` |
| Package pinout for multi-pin parts? | Pin map from the datasheet | `pcb.pin_map` |
| Connectors, mounting holes, test points, keepouts? | Board-only parts | `pcb.extra_components` |
| Fixed placement or automatic? | Placement mode | `pcb.placement` |
| Net current/voltage, isolation, creepage, impedance? | Netclasses and gates | `pcb.nets`, `pcb.netclasses` |
| What tolerance is acceptable for pass/fail? | Verification thresholds | `verification.tolerance_percent` |

## 7. PCB handoff (only if a board is requested)

Ask these only when the user wants a physical board. The electrical design does
not need them.

| Question | Why it matters | Spec field |
|---|---|---|
| Board width, height and layer count? | Required; the skill does not guess mechanics | `pcb.board` |
| Thickness, copper weight, material and finish? | Stackup and fabrication | `pcb.board` |
| Which pcb-design-skill profile? | DRC/DFM/thermal/impedance gates | `pcb.profile` |
| Which real footprint for each reference? | Logical SPICE pins are not pads | `pcb.footprints` |
| Package pinout for each transistor, MOSFET or op-amp? | Wrong pad map destroys hardware | `pcb.pin_map` |
| Connectors, mounting holes, test points, keepouts? | Board-only parts absent from SPICE | `pcb.extra_components` |
| Fixed coordinates or automatic placement? | Placement mode | `pcb.placement` |
| Net current, voltage, differential pairs, impedance? | Track width and rules | `pcb.nets`, `pcb.netclasses` |
| Which simulation-only parts need real replacements? | Ideal sources/switches are not boards | `notes` |

Do not infer a footprint from a value (`10k` does not imply 0805) or a package
pinout from a part number. If the user cannot supply a pin map, leave it out;
the PCB skill will ask. See `references/pcb-handoff.md`.

## Minimum viable intake

If the user wants speed, ask only these five:

1. What does it need to do, and what are input and output?
2. What supply voltage(s) are available?
3. What load must it drive?
4. What accuracy/bandwidth/current is required?
5. Which deliverables (SPICE, LTspice, KiCad, BOM, report, PCB handoff)?

Then state the remaining assumptions explicitly and proceed.

## Turning answers into a spec

```bash
# Start from a known topology, then edit values and pins.
python3 scripts/design_spec.py scaffold --topology rc_lowpass \
  --param r=1k --param c=100n --out specs/rc.json

python3 scripts/design_spec.py validate specs/rc.json
python3 scripts/design_spec.py explain specs/rc.json
```

Rules:

- **Explicit components beat topology parameters.** If the user supplies a
  component list, do not overwrite it with a scaffold.
- **Pin numbers are SPICE order, not package order.** See
  `references/kicad-format.md`.
- **Every net must have at least two pins**, except an intentional test point.
  A one-pin net is reported as floating.
- **Ground must be connected.** The SPICE netlist maps the spec ground to node
  `0`.
- **A source is required for simulation.** At least one `V` or `I` element.

## Questions that must stop the design

Do not proceed silently if any of these is unknown:

- Mains, high voltage (> 60 V DC or > 30 V AC), or isolation barrier.
- Battery charging or lithium cells.
- Medical, automotive safety, or life-support use.
- RF power or antenna-facing circuitry.
- Anything where a wrong pin assignment could destroy hardware.

Ask, document the answer, and add a safety note to the report.
