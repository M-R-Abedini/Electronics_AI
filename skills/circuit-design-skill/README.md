# circuit-design-skill

An evidence-led electronics design agent for analog, digital, mixed-signal and
power circuits. It turns requirements into a justified architecture, sourced
component choices, reproducible calculations, SPICE/LTspice/KiCad deliverables,
rendered schematic images (SVG), and verification evidence. It must explain
assumptions and operating limits rather than merely generate files.

The normalized circuit specification is the design contract; the generated
SPICE netlist is the simulation source of truth. LTspice and KiCad artifacts
share the same connectivity. SVG images are rendered from the KiCad schematic,
not independently redrawn, so the image remains traceable to the editable file.

## Why this exists

Most circuit mistakes are not exotic. They are:

- a divider loaded by the thing it drives;
- an op-amp without enough GBW or slew rate;
- a Sallen-Key high-pass with the wrong Q formula;
- an inductor that saturates at the peak current;
- a schematic whose pin numbers do not match its netlist;
- a simulation with a generic transistor presented as a verified design.

This skill makes those mistakes visible: it asks for the requirements, does the
arithmetic in Python, emits files from one component list, and re-reads the
files to check them.

It also assumes the model's electronics and physics knowledge is a starting
point, not a reference. Critical numbers must come from a datasheet, an
application note, a standard, or a measurement.

## Planning, research and part selection

Before a schematic, the skill expects a short, reviewable record:

- **Requirements** with conditions and acceptance limits.
- **Architecture**: blocks, interfaces, power tree, grounding, protection, test
  points, and at least one rejected alternative.
- **Budgets**: power, thermal, tolerance, timing, noise, cost and area.
- **Part decisions**: must-have constraints, candidates, a weighted comparison,
  worst-case verification, lifecycle and second source.
- **Risks**: severity, mitigation and the test that closes them.

The guides are `references/design-process.md`,
`references/part-selection.md` and `references/electronics-research.md`; the
fill-in forms are in `templates/`.

Put the result in the spec as `design` and gate on it:

```bash
python3 scripts/verify.py specs/rc.json --out out/ --require design
```

A `design` record is optional for a quick experiment, but `--require design`
turns a missing or unsourced record into a failure. The report renders the
requirements, decisions, risks and sources it contains.

## Requirements

- Python 3.10+ (tested on 3.14)
- Standard library only: no numpy, scipy, KiCad, LTspice or ngspice required
- Optional: `kiutils` for KiCad schematic parsing
- Optional: `ngspice` for nonlinear simulation
- Optional: `kicad-cli` 9+ for ERC
- Optional: `gh` + a pushed git branch for the GitHub Actions fallback

## Environment probe and CI fallback

A tool on `PATH` is not necessarily a tool that runs. `env_probe.py` executes
each binary (including a two-resistor ngspice smoke deck) and classifies it:

```bash
python3 scripts/env_probe.py --spec examples/rc-lowpass.json
```

```
  ngspice      INCOMPAT  error while loading shared libraries: libngspice.so.0
  kicad-cli    MISSING   kicad-cli not found on PATH
  simulate     NOT READY: incompatible ngspice
  erc          NOT READY: missing kicad-cli
  Recommended backend: ci
```

To require real evidence and fail instead of silently skipping:

```bash
python3 scripts/verify.py examples/rc-lowpass.json --out out/ --require simulate
```

When the machine cannot run the stage, send the same spec to the pinned
GitHub Actions runner (KiCad 9 + ngspice):

```bash
python3 scripts/ci_dispatch.py --spec examples/rc-lowpass.json \
  --require simulate,erc --watch --download out/ci
```

Dispatch refuses a dirty tree, a missing upstream or an unpushed commit,
because CI verifies the pushed revision, not the working copy. The verification
JSON records `provenance.backend` (`local` or `ci`), the commit SHA and the run
id.

## Real device models

Generic BJT/diode/MOSFET cards do not describe a part you can buy. Declare the
real model in `models`, with provenance:

```json
"models": {
  "2N3904": {
    "file": "models/2N3904.lib",
    "name": "2N3904",
    "vendor": "onsemi",
    "part": "2N3904",
    "source": "https://www.onsemi.com/.../2N3904.lib",
    "sha256": "4f0c…"
  }
}
```

Only the named `.model`/`.subckt` block is inlined into the netlist, with a
provenance comment. A subcircuit model (common for MOSFETs) also needs
`subckt_pins` — the generator will not guess the pin order. The report gains a
**Device models** table and the status moves from
`unverified (generic models)` to `vendor models supplied`.

Gate on it in CI:

```bash
python3 scripts/verify.py specs/rc.json --out out/ --require models,simulate
```

### Find a model

`catalog/models.json` lists parts whose models are published in a stable,
fetchable place. `scripts/model_fetch.py` downloads the file, checks that it
actually defines the requested `.model` or `.subckt`, records the SHA-256 and
provenance, and writes an entry the spec accepts:

```bash
python3 scripts/model_fetch.py --list
python3 scripts/model_fetch.py --part 2N3904 --out models/
python3 scripts/model_fetch.py --part 2N7002 --write-spec specs/switch.json --ref M1
python3 scripts/model_fetch.py --url https://example.com/part.lib \
  --part XYZ --vendor Acme
python3 scripts/model_fetch.py --file downloaded.lib --part XYZ \
  --vendor Acme --source https://example.com/part.lib
```

A `.subckt` model is refused unless its node order is known (`--subckt-pins`
or the catalog entry), because guessing the pin order is worse than refusing
the model. The fetcher does not certify the model against your physical part,
and it does not grant a redistribution licence: read the header, keep the
source URL and hash, and do not commit restricted vendor files.

See `references/models.md` for the forms, hashing and licence notes.

## Quick start

```bash
# 1. Scaffold a design
python3 scripts/design_spec.py scaffold --topology rc_lowpass \
  --param r=1k --param c=100n --out specs/rc.json

# 2. Validate and inspect it
python3 scripts/design_spec.py validate specs/rc.json
python3 scripts/design_spec.py explain specs/rc.json

# 3. Generate everything
python3 scripts/generate.py specs/rc.json --out out/

# 4. Verify it
python3 scripts/verify.py specs/rc.json --out out/

# 4a. Render the KiCad schematic to SVG (requires KiCad CLI 9+)
python3 scripts/render_schematic.py out/rc_lowpass.kicad_sch --out out/images/

# 5. Optional PCB handoff for pcb-design-skill
python3 scripts/generate.py specs/rc.json --out out/ --pcb-board 50x40
```

Outputs in `out/`:

| File | Purpose |
|---|---|
| `rc_lowpass.cir` | Self-contained SPICE netlist (models inline) |
| `rc_lowpass.models` | Model cards for the LTspice include |
| `rc_lowpass.asc` | LTspice schematic |
| `rc_lowpass.net` | KiCad netlist |
| `rc_lowpass.kicad_sch` | KiCad 9 schematic |
| `rc_lowpass-bom.csv` | Bill of materials |
| `rc_lowpass-report.md` / `.json` | Design report |
| `rc_lowpass-verification.json` | Verification evidence |
| `images/rc_lowpass.svg` | Rendered schematic image; not a PCB layout or proof of operation |
| `rc_lowpass-pcb.json` | PCB overlay (when `pcb` or `--pcb-board` is requested) |
| `rc_lowpass-pinmap.json` | SPICE pin -> footprint pad map |
| `rc_lowpass-node-aliases.json` | SPICE node -> PCB net aliases |
| `rc_lowpass-pcb-handoff.md` | Exact PCB-skill command and gaps |

## Design optimization

The first automated optimizer targets first-order RC low-pass filters. It
searches discrete E24 resistor and E12 capacitor values, enumerates exact
independent tolerance corners, ranks candidates by worst-case cutoff error,
and writes a revised design spec:

```bash
python3 scripts/optimize.py examples/rc-lowpass.json --target-fc 1.59k \\
  --r-min 1k --r-max 100k --c-min 10n --c-max 1u \\
  --r-tol 1 --c-tol 10 --max-error-percent 5 \\
  --out-spec out/rc-optimized.json --require-feasible
python3 scripts/verify.py out/rc-optimized.json --out out/optimized/ --require simulate
```

It currently assumes an ideal source and unloaded output. The generated spec
must still be simulated with real source/load conditions and checked against
component datasheets. This is a first bounded optimizer, not yet a universal
circuit optimizer. See `references/optimization.md`.

## Calculators

```bash
python3 scripts/circuit_calc.py list
python3 scripts/circuit_calc.py divider --vin 12 --r1 10k --r2 4k7 --json
python3 scripts/circuit_calc.py buck --vin 12 --vout 5 --iout 2 --fsw 500k
python3 scripts/circuit_calc.py sallen-key --topology highpass --fc 1k --q 0.7071

# Enumerate exact worst-case RC cutoff corners
python3 scripts/circuit_calc.py tolerance --mode rc_fc \\
  --components '{"r":{"value":1000,"tol":1},"c":{"value":1e-7,"tol":10}}' --json
```

26 commands across passives, analog, devices, power and digital/interface.
Every command returns the same JSON envelope with formulas, assumptions,
warnings and notes. The tolerance command evaluates all independent min/max
corners (up to 16 variables), and reports exact range/corner assignments plus
separate linearized sensitivity and RSS estimates.

## Design intake

The questions are in `references/intake.md`. If the user cannot answer, the
skill proposes a default and records it in the spec's `assumptions`. It does
not hide assumptions in the netlist.

## Bench test plan

Add a `design.measurements` array to the spec. Each measurement links to a
requirement and defines the test point, measured quantity, method, equipment,
conditions and acceptance criterion. Generation writes a standalone
`<name>-test-plan.md` with requirement traceability.

Require it at release with:

```bash
python3 scripts/verify.py specs/design.json --out out/ --require measurements
```

This validates that the plan is actionable and traceable; it does not claim a
measurement has been performed. Record actual readings and instrument
calibration status during bench bring-up.

## Verification levels

| Level | Proves | Does not prove |
|---|---|---|
| Design record | Requirements, decisions, risks and sources are written down | The decision is correct, or that the sources are current |
| Spec | Pins, nets and references are consistent | Electrical behaviour |
| Netlist agreement | SPICE, LTspice and KiCad describe the same nodes | Accuracy |
| PCB handoff | Overlay, pin map and aliases agree; sibling validator accepts | Layout, DRC, DFM, real pads |
| MNA | Exact linear DC/AC solution | Nonlinear devices, parasitics |
| ngspice | The netlist runs and converges | Real devices with generic models |
| Bench | The physical circuit works | All units and temperatures |

`verify.py` reports `PASS`, `FAIL` or `SKIP`. A skipped check is never a pass.
Generic models are labelled "unverified" in the report.

## Worst-case operating limits

Add calculated stresses and sourced datasheet limits to `design.operating_limits`.
Each record includes the calculation condition and source revision/URL; for
example, junction temperature against the datasheet maximum, MOSFET VDS against
its rating, or resistor power against its derated limit. The verifier checks
the supplied stress against the stated bounds. It does not independently derive
the stress, validate the source document, or replace a thermal/electrical
calculation. Use `--require limits` to make missing or out-of-range records fail.

## Topologies with scaffolds

`rc_lowpass`, `rc_highpass`, `divider`, `sallen_key_lp`, `sallen_key_hp`,
`bjt_ce`, `led`, `i2c`, `opamp_noninverting`, `opamp_inverting`, `zener_regulator`, `buck`.

Any other topology can be described with an explicit component list. The spec
is the contract; see `schemas/design-spec.schema.json`.

## Important limitation: logical pin numbers

The embedded KiCad symbols use **SPICE pin order** (NPN: 1=C, 2=B, 3=E;
MOSFET: 1=D, 2=G, 3=S; diode: 1=A, 2=K), not the standard KiCad `Device:`
numbering. This guarantees the schematic, KiCad netlist and SPICE netlist agree.

For PCB layout, map each logical pin to the real footprint pad. Do not assume
logical pin 1 is package pin 1. See `references/kicad-format.md`.

## PCB handoff

The sibling `pcb-design-skill` imports the SPICE netlist for connectivity and
a JSON overlay for board mechanics. `pcb_handoff.py` writes that overlay plus a
pin map and node aliases, and prints the exact command:

```bash
python3 scripts/pcb_handoff.py specs/rc.json --out out/ \
  --board 50x40 --profile 2layer-low-speed
```

Add a `pcb` block to the circuit spec for footprints, rules, netclasses,
placement and pin maps. Board dimensions are required; footprints and package
pinouts are never guessed. Missing ones are reported and left for the PCB
skill's visual picker. An op-amp `U1` becomes SPICE element `XU1`; the handoff
maps the reference explicitly. A MOSFET's SPICE line has a fourth bulk node
tied to source, so its pin map needs `b` as well as `d`/`g`/`s`; for a 3-pad
package, map `b` to the same pad as `s`. See `references/pcb-handoff.md`.

`verify.py` checks the handoff for internal agreement. When every footprint is
present and the sibling skill is installed, it also runs the sibling's own spec
validator. That is a structural/rules check, not DRC or a manufactured board.

## Scope and safety

This skill produces schematics, netlists and calculations. It does not replace
a datasheet, a bench measurement, ERC/DRC, or a safety review. Stop and ask for
mains, high voltage, isolation, lithium charging, medical/automotive safety, RF
power, or anything where a wrong pin assignment could destroy hardware.

For PCB layout, use `pcb-design-skill` with the generated overlay, pin map and
node aliases. The PCB skill's DRC, DFM and unconnected gates, not this skill,
decide whether the board is manufacturable.

## Audio handoff

Audio and sound-engineering requirements, perceptual limits and measurement
conditions come from the sibling
[`audio-engineering-skill`](https://github.com/davlug3/audio-engineering-skill).
That skill emits a versioned `handoff.json`; this skill merges its requirements
and target metrics into the design spec, then sizes, simulates and verifies the
electrical implementation.

This skill does not hold audio-specific knowledge or decide what sounds good.
A simulation is not a measurement, and an audio target is not a verified
result. The contract, merge mapping and current status are in
`references/audio-handoff.md`. The generic `metrics` stage is implemented; see
`references/metrics.md`.

Merge a handoff with the electrical test-point map:

```bash
python3 scripts/audio_handoff.py \
  --handoff ../audio-engineering-skill/examples/mic-preamp-handoff.json \
  --base examples/mic-preamp-base.json \
  --map examples/mic-preamp-map.json \
  --out out/mic-preamp-merged.json --require-registry
```

The sibling is found with `--audio-skill`, `AUDIO_ENGINEERING_SKILL`, or a
relative search. When present, every cited source is checked against the audio
registry; `--require-registry` turns a missing sibling into a failure instead
of a warning.

CI checks out both repositories, validates the handoff with the audio skill's
linter, merges it and validates the resulting spec.

## Metrics

A spec can declare the electrical result it expects. `verify.py` extracts it
from the ngspice rawfile and log and fails when a target or limit is missed.

```json
"analyses": [
  {"type": "ac", "start": 10, "stop": 100000, "points": 200}
],
"metrics": {
  "conditions": {"temperature_c": 25, "load_ohm": 10000},
  "metrics": [
    {"id": "M1", "measure": "gain_db", "requirement": "A1",
     "output": "v(out)", "input": "v(in)", "frequency": 1000,
     "target": 40.0, "tolerance": 0.5},
    {"id": "M2", "measure": "bandwidth_hz", "requirement": "A1",
     "output": "v(out)", "input": "v(in)", "min": 20000}
  ]
}
```

Supported measures are AC gain, upper/lower/width bandwidth, THD from a
`.four` analysis, input/output-referred noise from `.noise`, and transient RMS.
Each metric must reference a requirement in `design.requirements`, and the
matching analysis must be present in `analyses`.

```bash
python3 scripts/verify.py examples/rc-lowpass-metrics.json --out out/ \
  --require simulate
```

Without ngspice the metrics check is a `SKIP`; `--require simulate` makes that
a failure. Read `references/metrics.md` for definitions, conditions and limits.

## Graphical interaction

The optional `scripts/design_interact.py` command provides guided design intake
and artifact review. Its UI implementation is maintained separately; this
repository does not vendor it. The wrapper uses this sibling checkout layout:

```text
<workspace>/
├── _shared/
│   └── design-interact/
│       └── scripts/
│           └── design_interact.py
└── circuit-design-skill/
    └── scripts/
        └── design_interact.py
```

Clone the shared UI repository into `<workspace>/_shared/design-interact`.
Then run the wrapper from this repository:

```bash
python3 scripts/design_interact.py intake --out out/intake.json
python3 scripts/design_interact.py review-artifacts \
  --spec specs/rc.json --out-dir out/ --out out/review.json
```

The core circuit-generation and verification workflows do not depend on this
optional UI. Without the sibling checkout, the wrapper exits with an actionable
installation message (exit status 2); `--help` remains available.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests cover value parsing, the calculators, the MNA solver, generation,
verification, rawfile parsing, gain/bandwidth/THD/noise metrics, model
fetch/ingestion, the design record and the PCB handoff.
The KiCad parse test is skipped when `kiutils` is not installed; the
sibling-PCB integration test is skipped when `pcb-design-skill` is not
installed.

## License

MIT. See `LICENSE`.


## DIY-first footprint research

Before assigning an IC or semiconductor footprint, research the exact manufacturer's package options for the selected MPN. Prefer suitable through-hole packages, then larger-pitch hand-solderable SMD. Do not infer package families from component type. See `references/footprint-research.md`.

## End-to-end KiCad project and PCB

Use the orchestrator when you want the circuit and physical board in one run.
It generates the schematic, SPICE netlist, BOM and PCB handoff, then calls the
sibling pcb-design-skill pipeline to build the native KiCad project, place
footprints, route, run DRC/DFM and export fabrication files.

    python3 scripts/full_design.py specs/design.json \
      --board 50x40 --assembly through-hole \
      --pcb-skill ../pcb-design-skill --backend local --out out/design

Set PCB_DESIGN_SKILL instead of passing --pcb-skill when the sibling skill is
installed elsewhere. Board dimensions and assembly style are explicit because
they affect mechanical fit and footprint selection. Select a board-class
profile appropriate to voltage, speed, impedance and safety requirements;
the default 2-layer profile is only for ordinary low-speed boards.

The current orchestrator runs the sibling PCB pipeline locally: its CI workflow cannot read temporary handoff files outside the PCB repository. Run it on a machine with the PCB skill's KiCad 9, pcbnew, Java 25 and Freerouting dependencies installed. CI orchestration across the two repositories is a follow-up integration.

Outputs are split into out/design/generated/ (schematic, SPICE, BOM,
verification and PCB handoff) and out/design/pcb/ (native .kicad_pcb,
.kicad_pro, route/check reports and fabrication exports). The command returns
non-zero when PCB release gates fail. A board is not called fabrication-ready
unless the PCB report says all required gates and sign-offs passed. Missing or
provisional footprints, generic SPICE models, skipped simulation, ERC/DRC
failures and required human reviews remain explicit blockers.
