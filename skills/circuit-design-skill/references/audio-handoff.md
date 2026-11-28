# Audio Handoff

Audio-specific requirements and target performance come from the sibling
[`audio-engineering-skill`](https://github.com/davlug3/audio-engineering-skill).
This skill owns the electrical implementation: component sizing, netlist,
simulation, schematic and verification.

The split is deliberate:

- **audio skill** — what the circuit must achieve, why, the conditions, the
  measurement method and the perceptual interpretation;
- **circuit skill** — how to make the electrons do it, and what was simulated
  or verified.

Do not restate audio targets from memory here. If a spec contains audio
requirements, they must carry a source from the audio skill's registry.

## Contract version 1

The audio skill emits `handoff.json`:

```json
{
  "handoff_version": 1,
  "application": "microphone preamplifier",
  "conditions": {"input_vrms": 0.002, "frequency_hz": 1000, "load_ohm": 10000},
  "requirements": [
    {"id": "A1", "text": "Flat gain", "acceptance": "40 +/-0.5 dB, 20 Hz-20 kHz", "source": "S041"},
    {"id": "A2", "text": "Input-referred noise", "acceptance": "<= 1.0 uV RMS, 20 Hz-20 kHz, 150 Ohm source", "source": "S045"}
  ],
  "metrics": [
    {"measure": "gain_db", "target": 40.0, "tolerance": 0.5, "requirement": "A1"},
    {"measure": "ein_uv", "target": 1.0, "tolerance": 0.0, "requirement": "A2"}
  ],
  "sources": [
    {"id": "S041", "title": "RaneNote 145: Audio Specifications", "url": "https://www.ranecommercial.com/legacy/note145.html", "accessed": "2026-11-08"},
    {"id": "S045", "title": "Noise in Audio Amplifiers", "url": "https://sound-au.com/noise.htm", "accessed": "2026-11-08"}
  ]
}
```

Source ids refer to the sibling audio registry/evidence workflow. They must be
`verified` there, and the condition in each acceptance limit must be carried
into the analysis or metric block. The examples above use S041 and S045, which
are verified in the audio registry at the time of writing.

### Terminology (audio ↔ circuit)

| Term | Audio meaning | Circuit meaning |
|---|---|---|
| `S###` source ID | Verified registry entry backing a requirement/claim | Record in `design.sources[]` (and keep provenance/link/revision/accessed context); add datasheet/app-note sources for electrical decisions |
| `A#`/requirement ID | Audio requirement identifier | Preserved in `design.requirements[]`; every non-exploratory metric references an existing requirement |
| Conditions | Measurement/application conditions (level, frequency, bandwidth, load, source impedance, weighting/method) | `metrics.conditions` and matching `analyses[]`; do not compare numbers without matching conditions |
| `exploratory` metric | Useful target not yet contractually required/provable | May be simulated if supported, but do not claim it as a required verified pass without evidence/support |

Merge mapping:

| Handoff | Design spec |
|---|---|
| `requirements[]` | `design.requirements[]` |
| `conditions` | the relevant analysis or `metrics.conditions` |
| `metrics[]` | `metrics.metrics[]` |
| `architecture` | `design.architecture` |
| `risks[]` | `design.risks[]` |
| `sources[]` | `design.sources[]` |

The spec must reject a metric whose `requirement` id is absent.

### From handoff to metrics

The handoff says **what** to measure; the circuit spec needs the electrical
test point. The merge adds these fields to each metric:

- `analysis` and the matching entry in `analyses[]` (`ac`, `tran`, `noise`, or
  `fourier`);
- `output` and, for gain and bandwidth, `input` rawfile variables such as
  `v(out)` and `v(in)`;
- `frequency` for `gain_db`, `fundamental` for `thd_percent`, and
  `start`/`stop` for `output_vrms` when the default window is not appropriate;
- a stable `id`;
- the shared test conditions under `metrics.conditions`.

Example merged block:

```json
{
  "analyses": [
    {"type": "ac", "start": 10, "stop": 100000, "points": 200},
    {"type": "noise", "output": "OUT", "source": "V1",
     "start": 20, "stop": 20000, "points": 100}
  ],
  "metrics": {
    "conditions": {"input_vrms": 0.002, "frequency_hz": 1000,
                   "load_ohm": 10000, "source_impedance_ohm": 150},
    "metrics": [
      {"id": "M1", "measure": "gain_db", "requirement": "A1",
       "analysis": "ac", "output": "v(out)", "input": "v(in)",
       "frequency": 1000, "target": 40.0, "tolerance": 0.5},
      {"id": "M2", "measure": "ein_uv", "requirement": "A2",
       "analysis": "noise", "output": "v(out)", "max": 1.0}
    ]
  }
}
```

The merged spec is validated by `scripts/design_spec.py`: a metric with a
missing analysis, a missing `output`/`input`, no target or limit, or an unknown
`requirement` is rejected before simulation.

The circuit skill ships `scripts/audio_handoff.py` to perform the merge:

```bash
python3 scripts/audio_handoff.py \
  --handoff ../audio-engineering-skill/examples/mic-preamp-handoff.json \
  --base examples/mic-preamp-base.json \
  --map examples/mic-preamp-map.json \
  --out out/mic-preamp-merged.json
```

- `--base` is the circuit implementation: components, analyses and
  deliverables. `examples/mic-preamp-base.json` is an integration fixture, not
  a complete microphone preamplifier.
- `--map` supplies the electrical test points the audio handoff cannot know:

```json
{
  "metrics": [
    {"measure": "gain_db", "requirement": "A1", "id": "M1",
     "analysis": "ac", "output": "v(out)", "input": "v(in)",
     "frequency": 1000},
    {"measure": "ein_uv", "requirement": "A2", "id": "M2",
     "analysis": "noise", "output": "v(out)"}
  ]
}
```

The mapping is keyed by `(measure, requirement)`, so handoff order does not
matter. Every handoff metric must have one mapping entry, and every metric must
match the analysis required by its measure. The merge fails on a version
mismatch, an unsupported measure, a duplicate mapping, a missing requirement,
or a target without a tolerance. The merged spec is validated before it is
written.

The circuit skill's CI has a cross-repo job that checks out both repositories,
validates the audio handoff with the audio skill's own linter, merges
`examples/mic-preamp-handoff.json`, and validates the resulting spec.

When the sibling is found (see below), the merge also checks every cited and
listed source against the audio registry: it must exist and be `verified`.
`--require-registry` makes a missing sibling a failure instead of a warning.

## Status

- Contract defined and published by the audio skill.
- The generic `metrics` stage is implemented. It extracts gain, bandwidth,
  THD, input/output noise and output RMS from the ngspice rawfile and log, and
  compares them with the merged targets. See `references/metrics.md`.
- The cross-repo merge is implemented in `scripts/audio_handoff.py` and
  exercised by CI.
- A metric is still a **simulation result**, not a bench measurement or a
  perceptual claim. State the model provenance and the test conditions.
- The audio skill validates the handoff with
  `source_check.py --strict --handoff <file>`; every requirement source must be
  verified and listed, every metric must use a supported measure and reference
  an existing requirement. The sibling repo ships
  `examples/mic-preamp-handoff.json`.
- The circuit skill's CI verifies `examples/rc-lowpass-metrics.json` with
  ngspice and requires the `simulate` stage, so the metric path is exercised
  end to end on a real simulator.

## Locating the sibling

The repository is
[`davlug3/audio-engineering-skill`](https://github.com/davlug3/audio-engineering-skill).
Search order: `--audio-skill`, the `AUDIO_ENGINEERING_SKILL` environment
variable, `../audio-engineering-skill` next to this repository, then
`./audio-engineering-skill`. A candidate is used only when it contains
`catalog/sources.json`. No runtime import is required: the handoff is plain
JSON, and a missing sibling only skips the provenance check.

## Non-goals

- This skill does not decide what sounds good.
- A simulation is not a measurement and does not prove a perceptual claim.
- Audio targets without conditions (level, frequency, bandwidth, load,
  weighting) are rejected.
