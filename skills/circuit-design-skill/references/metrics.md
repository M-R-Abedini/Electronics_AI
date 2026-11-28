# Metrics: gain, bandwidth, THD, noise and level

A structural pass says the netlist matches the spec. It does not say the
circuit has the gain, bandwidth, distortion or noise the brief asked for. The
`metrics` stage extracts those numbers from the ngspice result and compares
them with declared targets or limits.

The stage is deliberately generic. Audio targets, weighting, test conditions
and interpretation come from the sibling
[`audio-engineering-skill`](https://github.com/davlug3/audio-engineering-skill);
this skill performs the electrical extraction and reports what the simulation
actually produced.

## Spec block

```json
{
  "analyses": [
    {"type": "ac", "start": 10, "stop": 100000, "points": 200},
    {"type": "tran", "step": 1e-6, "stop": 0.005},
    {"type": "fourier", "fundamental": 1000, "output": "v(out)"},
    {"type": "noise", "output": "OUT", "source": "V1",
     "start": 20, "stop": 20000, "points": 100}
  ],
  "metrics": {
    "conditions": {
      "temperature_c": 25,
      "input_vrms": 0.002,
      "load_ohm": 10000,
      "source_impedance_ohm": 150
    },
    "metrics": [
      {"id": "M1", "measure": "gain_db", "requirement": "A1",
       "output": "v(out)", "input": "v(in)", "frequency": 1000,
       "target": 40.0, "tolerance": 0.5},
      {"id": "M2", "measure": "bandwidth_hz", "requirement": "A1",
       "output": "v(out)", "input": "v(in)", "min": 20000},
      {"id": "M3", "measure": "thd_percent", "requirement": "A2",
       "output": "v(out)", "fundamental": 1000, "max": 0.01},
      {"id": "M4", "measure": "ein_uv", "requirement": "A3",
       "output": "v(out)", "max": 1.0}
    ]
  }
}
```

Every metric must have one of:

- `target` plus `tolerance` — absolute tolerance in the metric's unit;
- `min` or `max`;
- `exploratory: true` — measure and report, but do not pass or fail.

A metric with none of these is a validation error. So is a `requirement` id
that is absent from `design.requirements`.

## Measures

| Measure | Analysis | Required fields | Unit | Definition |
|---|---|---|---|---|
| `gain_db` | `ac` | `output`, `input`, `frequency` | dB | `20 log10(|Vout| / |Vin|)` at the nearest AC point |
| `bandwidth_hz` | `ac` | `output`, `input` | Hz | upper -3 dB corner relative to the maximum AC gain |
| `bandwidth_low_hz` | `ac` | `output`, `input` | Hz | lower -3 dB corner relative to the maximum AC gain |
| `bandwidth_width_hz` | `ac` | `output`, `input` | Hz | upper corner minus lower corner |
| `thd_percent` | `fourier` | `output`, `fundamental` | % | THD reported by the ngspice `.four` block for that output |
| `ein_uv` | `noise` | `output` | µV RMS | `inoise_spectrum` integrated over the noise sweep |
| `noise_uv` | `noise` | `output` | µV RMS | `onoise_spectrum` integrated over the noise sweep |
| `output_vrms` | `tran` | `output`, optional `start`/`stop` | V RMS | RMS of the output over a steady-state window |

`output` and `input` are rawfile variables. `v(out)` and `out` are both
accepted; a bare node is wrapped as `v(...)`. Use `i(V1)` for a current.

### Gain

`gain_db` uses the nearest AC sweep point to `frequency`; it does not
interpolate. Put a sweep point at the test frequency, or make the sweep dense
enough that the nearest point is within about 5%. If it is farther away the
metric fails instead of reporting the gain at the wrong frequency. The default
frequency is 1 kHz.

The AC source must have an `ac` magnitude. With the usual `ac 1`, the input
variable is the source node voltage.

### Bandwidth

The -3 dB corners are relative to the **maximum** gain in the sweep, not to
the first point. This works for low-pass, high-pass and band-pass responses:

- `bandwidth_hz` finds where the response falls 3 dB below the maximum;
- `bandwidth_low_hz` finds where it rises to 3 dB below the maximum;
- `bandwidth_width_hz` subtracts the two.

The corner is interpolated in log frequency between the two bracketing sweep
points. The sweep must actually cross the threshold. A sweep that ends while
the gain is still above -3 dB is an error, not a very wide bandwidth.

For a first-order low-pass, the -3 dB point relative to the 0 dB passband is
`fc * sqrt(10^(3/10) - 1)`, about `0.9976 fc`; the traditional corner `fc` is
-3.0103 dB. Do not expect the two to agree to more digits than the sweep
resolution supports.

### THD

`thd_percent` reads the `Total harmonic distortion` or `THD` line from the
ngspice `.four` block that names the metric's `output`. It requires:

- a transient analysis with a sine source;
- a `fourier` analysis in the `analyses` list, with a matching `fundamental`
  and output;
- enough transient cycles to reach steady state. `.four` uses the last cycle
  of the transient result.

The generator emits `.four <fundamental> <output>` for the `fourier`
analysis. Put `tran` before `fourier` in the list so the intent is clear.

THD from `.four` is a numerical result at the chosen timestep and last cycle.
It is not a bench distortion measurement, and it does not cover distortion
that is not harmonically related to the test tone.

### Noise

`ein_uv` and `noise_uv` integrate the input- or output-referred noise spectral
density over the noise-analysis sweep:

`V_rms = sqrt(integral(S_v(f)^2 df))`

The result is reported in microvolts. The noise analysis must name an output
node and an input source; the source should carry `ac 1`. The sweep range is
the measurement bandwidth. A 20 Hz–20 kHz sweep gives an unweighted 20 Hz–20
kHz noise figure, not an A-weighted or ITU-R BS.468-weighted value.

The integration uses the raw spectrum and trapezoidal intervals. It assumes
the spectrum is in V/√Hz and the frequency axis is ascending. If ngspice
provides a scalar total, the scalar is used.

Noise depends on the device models, operating point and source resistance. A
generic transistor model gives a smoke-test number, not the noise of the real
part.

### Output RMS

`output_vrms` integrates `v(out)^2` over `start..stop` and takes the square
root. If no window is given, it uses the last 25% of the transient result.
The value includes any DC offset. For an AC-only RMS, choose a window that
contains whole cycles and state the DC condition, or measure the AC component
with a separate metric in a later version.

## Analysis requirements

The validator requires the analysis that each measure needs. It does not add
it silently:

- `gain_db` and the bandwidth measures need an `ac` analysis;
- `thd_percent` needs `tran` **and** `fourier`;
- `ein_uv` and `noise_uv` need a `noise` analysis;
- `output_vrms` needs a `tran` analysis.

The `analyses` block is also what the netlist generator emits. A metric whose
analysis is missing is a spec error before simulation starts. Keep `spice` in
`deliverables`: without a generated netlist the metrics check fails rather than
silently skipping.

## Running it

```bash
python3 scripts/verify.py specs/preamp.json --out out/ --json
python3 scripts/verify.py specs/preamp.json --out out/ --require simulate
```

The result contains a `metrics` check with the measured value, unit, rule and
pass/fail state for each metric. If ngspice is unavailable, the check is a
`SKIP`; `--require simulate` turns that into a failure. A metric that cannot
be evaluated after ngspice ran (missing plot, missing variable, no THD line)
is a failure, never a silent zero.

`examples/rc-lowpass-metrics.json` is a worked AC example. CI verifies it with
ngspice and requires the `simulate` stage.

## Limits

- Simulation is not a measurement. Temperature, tolerance, parasitics, layout
  and part variation are outside the model.
- Generic device models are placeholders. A metric computed with them is a
  smoke test, not a vendor-part guarantee.
- Bandwidth accuracy is limited by the AC sweep density and by the -3 dB
  definition above.
- `.four` THD is limited by the transient timestep, the last-cycle choice and
  the harmonic count. It is not a full distortion spectrum.
- Noise integration is only as good as the model noise parameters and the
  declared bandwidth.
- No metric here proves a perceptual claim. Loudness, masking, preference and
  listening-room behaviour belong to the audio skill and to listening tests.

## Implementation

| File | Role |
|---|---|
| `scripts/ngspice_raw.py` | parses ASCII rawfiles into plots, variables and complex values |
| `scripts/metrics.py` | extracts and compares gain, bandwidth, THD, noise and RMS |
| `scripts/verify.py` | runs the `metrics` check after ngspice |
| `scripts/design_spec.py` | validates the block, analysis links and requirement links |
| `scripts/generate.py` | emits `.four` for a `fourier` analysis |
| `scripts/audio_handoff.py` | merges an audio handoff and a test-point map into a circuit spec |
| `examples/rc-lowpass-metrics.json` | CI-checked AC example |

## Sources

- ngspice manual, batch mode, `.ac`, `.tran`, `.noise`, `.four` and rawfile
  format.
- The circuit's own closed-form response and the generated netlist.
