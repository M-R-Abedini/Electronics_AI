# Verification

A design is not "done" because the file exists. It is done when the claims you
make are backed by a check you actually ran, and the limits of that check are
written down.

## Evidence levels

| Level | What it proves | What it does not prove |
|---|---|---|
| Spec validation | Structure, pins, nets, references are consistent | Electrical behaviour |
| Design record | Requirements, architecture, decisions, risks and sources are written | That the decision is correct or the sources are current |
| Netlist agreement | SPICE / LTspice / KiCad describe the same nodes | Simulation accuracy |
| PCB handoff | Overlay, pin map and aliases agree with the netlist | Layout, DRC, DFM, real pads |
| MNA (linear) | Exact DC/AC solution of the linear model | Nonlinear devices, parasitics, layout |
| ngspice | The generated netlist runs and converges | Real-device behaviour if models are generic |
| Metrics | Gain, bandwidth, THD, noise or RMS matched a declared target | A bench measurement, perceptual quality, or anything outside the model |
| KiCad ERC | KiCad's own electrical rules accept the schematic | Layout, DRC, DFM, real pads |
| CI (GitHub Actions) | The same checks ran at a known commit in a pinned image | That this machine can run them; bench behaviour |
| Vendor model | The simulation uses a named part with recorded vendor/source/hash | That the model matches your reel |
| Datasheet check | The part can meet the stress | The whole system |
| Bench measurement | The physical circuit works | All units, all temperatures |

Report the highest level actually reached. "PASS" at a lower level must not be
phrased as a higher-level claim.

## Worst-case tolerance and operating limits

The `tolerance` calculator enumerates every independent min/max corner for
supported scalar equations (divider, RC cutoff, sum, product, parallel and
custom linear expression), up to 16 variables. It also reports local
sensitivity and RSS, but labels those as estimates. Exact corner enumeration
is not a nonlinear circuit corner simulation and does not automatically model
temperature coefficients, aging, voltage coefficients, or correlated parts.

A design can declare `design.operating_limits` entries containing the calculated
worst-case stress, units, operating condition, datasheet min/max and a source
reference. `verify.py` checks those records and reports margins. Use
`--require limits` to fail when no limit record exists or any stress is outside
its stated bounds. This validates the recorded comparison; it does not
independently derive the stress or authenticate the datasheet citation.

## What `verify.py` checks

```bash
python3 scripts/verify.py specs/rc.json --out out/
python3 scripts/verify.py specs/rc.json --out out/ --json
```

1. **Spec** — `design_spec.validate_spec`.
2. **SPICE netlist** — re-reads `<name>.cir`, extracts device lines, and
   compares each device's node list with the spec. Catches a missing device,
   swapped nodes, or a wrong ground mapping.
3. **LTspice `.asc`** — parses `SYMBOL`, `SYMATTR` and `FLAG` lines, recomputes
   every pin coordinate, and checks that the flag at that coordinate has the
   expected net. Catches the classic "one grid unit off" bug.
4. **KiCad netlist** — parses the S-expression and compares every net's node
   set with the spec. Any extra or missing node is a failure.
5. **KiCad schematic** — uses `kiutils` to parse the file, reads the **embedded
   symbol** pin coordinates, applies the placed symbol's transform, and checks
   that a global label with the right name sits at each pin endpoint. Skipped,
   not passed, if `kiutils` is unavailable.
6. **PCB handoff** — if `<name>-pcb.json` exists, checks that every boardable
   part is in the overlay, that pin-map references exist and do not assign one
   pad to two different nets, that `node_aliases["0"]` is the ground net, and
   that every declared net uses a defined netclass. Missing footprints and
   missing pin maps are reported as remaining work. When every footprint is
   present and the sibling `pcb-design-skill` is installed, it also runs the
   sibling's spec validator and reports its warnings. This proves the overlay
   is internally consistent and accepted by the PCB skill's rules; it does not
   prove DRC, DFM or that a footprint's real pads match the datasheet.
7. **Design consistency** — for supported topologies, re-runs the sizing
   calculator and checks the chosen `L`, `Cout` or series resistor.
8. **MNA** — builds a linear circuit from the spec and solves DC and AC. Then:
   - divider: compares `Vout` with `Vin*R2/(R1+R2)`;
   - RC: compares the numerical -3 dB point with `1/(2*pi*R*C)`;
   - Sallen-Key: compares the numerical corner with the design target;
   - non-inverting op-amp: compares `|gain(10 Hz)|` with `1+Rf/Rg`.
9. **ngspice** — if installed *and compatible*, runs the generated netlist in
   batch mode. A singular matrix or non-convergence is a failure. If the output
   format cannot be parsed, the check is skipped and the raw log is attached; an
   unparsed run is not evidence.
10. **Metrics** — after ngspice runs, extracts the declared `gain_db`,
    bandwidth, `thd_percent`, `ein_uv`/`noise_uv` or `output_vrms` values from
    the rawfile and log, and compares them with the metric's target or limit.
    A metric that cannot be evaluated after ngspice ran is a failure. If
    ngspice is unavailable the whole check is a `SKIP`; `--require simulate`
    turns that into a failure. See `references/metrics.md`.
11. **KiCad ERC** — with `--erc`, runs `kicad-cli sch erc --severity-error
    --exit-code-violations`. Exit code 5 means error-level violations and is a
    failure; any other non-zero code is a tool error, not a clean pass. The
    report is written next to the output as `<name>-erc.rpt`.
12. **Required stages** — `--require simulate,erc,kicad-parse,models,design,limits`
    converts a `SKIP` in a required stage into a failure. This is what CI uses
    so a missing tool, a generic transistor, or an unrecorded decision cannot
    masquerade as a pass.
13. **Models** — resolves every semiconductor to its declared card. Generic
    placeholders and declarations without vendor/part/source are reported as
    `SKIP` (not pass); a missing model file, a hash mismatch or a missing
    `subckt_pins` order is a validation failure.
15. **Operating limits** — checks each declared calculated stress against its sourced min/max range and reports margin. This validates the recorded comparison, not the stress calculation or source authenticity; `--require limits` makes the record mandatory.
14. **Design record** — checks that `design` contains requirements, an
    architecture summary or blocks, decisions, risks and sources. A decision
    with neither a source nor a rationale, or a high risk without a mitigation
    and a verification, is a `SKIP`; `--require design` turns that into a
    failure. This proves the record exists and is complete enough to review; it
    does not prove the requirements are right or the sources are current.
    The report renders the record.

## Local capability and CI

`scripts/env_probe.py` classifies every tool the verifier can use:

| State | Meaning |
|---|---|
| `ok` | Found, executes, meets the minimum version |
| `incompatible` | Found, but cannot execute or is too old (reason recorded) |
| `missing` | Not on `PATH` or not importable |

A binary can be `incompatible` for aarch64/Android (x86-64 build), for a
missing shared library, or because `kicad-cli` predates the ERC flags this
skill passes. The verifier distinguishes `missing` from `incompatible` in its
`SKIP` text, so the reason is never lost.

When a required stage is not locally available:

```bash
python3 scripts/ci_dispatch.py --spec specs/rc.json --require simulate,erc --watch
```

CI runs on `ubuntu-24.04` with KiCad 9 from the `kicad-9.0-releases` PPA and
`ngspice` from apt. It checks out the pushed commit, runs the unit tests, then
generates and verifies with the required stages, and uploads `out/` and the
verification JSON.

Rules for CI evidence:

- CI proves the run happened at that commit in that image. It does **not** prove
  this machine can run the tools.
- A CI pass never upgrades a local `SKIP` to a local `PASS`. The JSON records
  `provenance.backend` and the commit/run id.
- Dispatch refuses a dirty tree, a missing upstream or unpushed commits, because
  GitHub would otherwise verify a different revision.


## Reading the result

```
PASS: rc_lowpass (9 passed, 1 skipped, 0 failed)
  [PASS] spec: ...
  [PASS] spice_netlist: 3 devices, node lists match the spec
  [PASS] ltspice_asc: 3 symbols; every pin has a matching net flag
  [PASS] kicad_netlist: 3 nets match the spec exactly
  [PASS] kicad_schematic: 3 symbols; every pin endpoint carries the correct global label
  [PASS] pcb_handoff: 2 board parts, 0 pin map(s), 1 node alias(es) ...
  [PASS] mna_dc: DC operating point solved; node voltages: ...
  [PASS] rc_corner: -3 dB at 1.59118 kHz, closed form 1.59155 kHz
  [PASS] mna_ac: AC solve is finite
  [SKIP] ngspice: ngspice is not installed; nonlinear devices were not simulated
  [SKIP] metrics: ngspice unavailable; declared metrics were not evaluated
```

With ngspice available, the metrics check reports each value and rule, for
example:

```
  [PASS] metrics: M1: gain_db=-1.44507 dB (PASS; target -1.4451 +/- 0.01); M2: bandwidth_hz=1591.3 Hz (PASS; target 1591.55 +/- 31.83)
```

- **PASS** — the check ran and the assertion held.
- **FAIL** — the check ran and the assertion did not hold. Fix the design.
- **SKIP** — the check could not run (missing tool, nonlinear device, no AC
  source). A skip is not a pass. Say why it was skipped. The PCB handoff is
  skipped when it was not generated; the sibling PCB-spec validation is
  deferred, not passed, while footprints are still missing.

The result is written to `<name>-verification.json`, and the Markdown report is
regenerated with the evidence.

## Honesty rules

1. **No "this should work".** If you did not run it, say "not verified".
2. **Generic models are placeholders.** A simulation with a generic BJT or
   diode is a smoke test, not a design validation. The report must say so, and
   a real part needs vendor, part and source provenance (`references/models.md`).
3. **MNA is linear.** It refuses diodes, BJTs and MOSFETs. Do not linearise
   them silently.
4. **A structural pass is not ERC/DRC.** KiCad's ERC checks more than the
   generator does. Run it in KiCad.
5. **A simulation is not a bench measurement.** Temperature, tolerance,
   parasitics, layout and part variation are outside the model.
6. **Do not quote a number without its condition.** "1.59 kHz" is meaningless
   without the resistor and capacitor values and the model.
7. **A design record is not a proof.** It proves the requirements, decisions
   and risks were written down and sourced, not that they are correct. Review
   the sources and the operating point.
8. **No source, no claim.** A datasheet value without a revision, a formula
   without a units check, or a part choice without a comparison is a
   hypothesis. See `references/electronics-research.md`.

## Adding a check

A check is a function returning a `Check`:

```python
Check(name, ok, detail, evidence)
# ok=True  -> ran and passed
# ok=False -> ran and failed
# ok=None  -> could not run (skipped)
```

Guidelines:

- Assert something falsifiable. "The netlist exists" is weak; "every pin's
  node matches the spec" is strong.
- Put the actual and expected values in `detail`.
- Attach raw numbers in `evidence` so a later reader can recompute.
- Never return `ok=True` when the tool was missing or the output was not
  parsed.
- Keep the check independent of the generator where possible. Re-read the
  generated file; do not trust the in-memory object.

## What to verify by hand

Even with a full pass:

- Read the datasheet for every semiconductor, connector and power part.
- Check the current revision and the errata, not a remembered version.
- Check every metric's condition: frequency, bandwidth, input level, source
  impedance, load and weighting. A number without its condition is not a
  specification. See `references/metrics.md`.
- Check the absolute maximum ratings at the worst-case temperature.
- Check the pinout against the package drawing.
- Check the footprint pad numbering against the logical pin numbering.
- Review the thermal and derating numbers.
- Review the protection and fault behaviour.
- Run ERC/DRC in KiCad before fabrication.
- Build and measure a prototype before production.

## Sources

- IPC-2221B, *Generic Standard on Printed Board Design*.
- Vendor datasheets and application notes for the specific parts.
- KiCad, *Electrical Rules Checker* documentation.
- `ngspice` manual, batch mode and analyses.


## KiCad PCB DRC

The verifier supports `--require pcb-drc`. It requires an actual
`<name>.kicad_pcb` file in the output directory and a KiCad CLI whose
`pcb drc --help` exposes `--exit-code-violations`. It writes
`<name>-drc.rpt`. A missing board or unsupported CLI is recorded as SKIP;
when `pcb-drc` is required, that SKIP makes the required-stage gate fail.
A PCB handoff JSON is never accepted as a board file.
