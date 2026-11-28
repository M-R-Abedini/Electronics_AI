# PCB handoff

This skill designs and verifies the **electrical** circuit. The sibling
`pcb-design-skill` owns board mechanics, placement, routing, DRC, DFM and
fabrication. The handoff is a set of files, not a claim that the board is
manufacturable.

The contract is:

- the generated SPICE netlist is authoritative for connectivity;
- the PCB overlay is authoritative for board size, stackup, rules, footprints
  and placement;
- logical SPICE pin numbers are **not** physical footprint pads;
- the PCB skill's DRC/DFM/unconnected gates decide manufacturability.

## Contract version 1

The overlay carries `"version": 1`, and the PCB skill rejects any other value.
That field is the circuit-to-PCB handoff contract; the four artifacts and the
authority split above are part of it.

- Additive optional fields keep version 1.
- Renaming or removing a field, changing units, or changing a field's meaning
  requires a version bump in both repositories.
- The consumer fails on an unknown version instead of guessing.

The audio skill is not part of this contract. Audio targets stay in the audio
handoff and reach the board only as electrical requirements in the circuit
spec; the PCB overlay carries no audio claims.

## Files

When `"pcb"` is in `deliverables`, `--pcb-board` is passed, or
`--formats ...,pcb` is used, the generator writes:

| File | Purpose |
|---|---|
| `<name>-pcb.json` | PCB design-spec overlay |
| `<name>-pinmap.json` | SPICE pin -> footprint pad map |
| `<name>-node-aliases.json` | SPICE node -> PCB net-name aliases |
| `<name>-pcb-handoff.md` | Exact command, missing data and caveats |
| `<name>.cir` | Connectivity source imported by the PCB skill |

`pcb` is opt-in. It needs a board size, because guessing board mechanics is not
a design decision this skill can make.

## Circuit-spec block

```json
{
  "pcb": {
    "profile": "2layer-low-speed",
    "board": {
      "width_mm": 50,
      "height_mm": 40,
      "layers": 2,
      "thickness_mm": 1.6,
      "copper_oz": 1.0,
      "surface_finish": "HASL-LF"
    },
    "rules": {
      "min_track_width_mm": 0.2,
      "min_clearance_mm": 0.2
    },
    "netclasses": {
      "Default": {"track_width_mm": 0.25, "clearance_mm": 0.2},
      "Power": {"track_width_mm": 0.5, "clearance_mm": 0.3}
    },
    "nets": [
      {"name": "VIN", "class": "Power", "voltage_v": 12, "current_a": 2}
    ],
    "footprints": {
      "R1": "Resistor_SMD:R_0805_2012Metric",
      "C1": "Capacitor_SMD:C_0805_2012Metric"
    },
    "pin_map": {
      "refs": {
        "Q1": {"c": "3", "b": "1", "e": "2"},
        "U1": ["1", "2", "8", "4", "3"]
      }
    },
    "node_aliases": {"0": "GND"},
    "placement": {"mode": "auto"},
    "routing": {"mode": "freerouting"},
    "extra_components": [
      {
        "ref": "J1",
        "value": "2-pin header",
        "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
        "pads": {"1": "VIN", "2": "GND"}
      }
    ]
  },
  "deliverables": ["spice", "pcb", "report"]
}
```

`pcb.board.width_mm` and `height_mm` are required. `layers` defaults to 2.
`profile` defaults to `2layer-low-speed` only for a 2-layer board; choose an
explicit profile for other stackups.

## Command line

```bash
# From the circuit skill:
python3 scripts/pcb_handoff.py design.json --out out/ \
  --board 50x40 --profile 2layer-low-speed

# Or generate everything, including the PCB handoff:
python3 scripts/generate.py design.json --out out/ --pcb-board 50x40
```

The handoff document prints the exact PCB-skill command:

```bash
python3 ../pcb-design-skill/scripts/design_pipeline.py all \
  out/<name>-pcb.json \
  --ltspice out/<name>.cir \
  --pin-map out/<name>-pinmap.json \
  --node-aliases out/<name>-node-aliases.json
```

Without `--no-ask`, the PCB skill asks for any missing footprint, package
pinout or placement. With `--no-ask`, it fails instead of asking, so only use
it when the handoff says `Ready for non-interactive --no-ask: yes`.

## Pin maps

A pin map maps the SPICE element's pins to **physical footprint pads**. It is
not a renaming of the circuit's logical pins.

For two-terminal parts the handoff supplies conservative defaults:

```json
{"defaults": {"R": ["1", "2"], "C": ["1", "2"], "L": ["1", "2"], "D": ["1", "2"]}}
```

For multi-pin parts, use the datasheet:

```json
{
  "refs": {
    "Q1": {"c": "3", "b": "1", "e": "2"},
    "M1": {"d": "3", "g": "1", "s": "2", "b": "2"},
    "U1": ["1", "2", "8", "4", "3"]
  }
}
```

- `Q`: semantic keys `c`, `b`, `e`, and `s` only if the model has a substrate
  node. This skill emits a 3-node BJT.
- `M`: `d`, `g`, `s`, and `b`. The SPICE MOSFET line has four nodes
  (`d g s b`); this skill connects the bulk node to the source net. For a
  3-pad package, map `b` to the same pad as `s` (for example
  `{"d": "3", "g": "1", "s": "2", "b": "2"}`); for a 4-pad package, map `b`
  to its own pad.
- `X` (op-amp): an ordered list in the subcircuit order `inp, inn, vp, vn,
  out`, or a port-name object using those names (or `IN+`, `IN-`, `V+`, `V-`,
  `OUT`).
- `S` (ideal switch): an ordered 4-pad list. Prefer replacing it with a real
  NMOS/PMOS.

An ordered list must have one entry per SPICE node. For a 3-pad MOSFET, the
bulk and source share a pad, so a valid list is `["3", "1", "2", "2"]`.

The circuit reference is `U1`; the SPICE subcircuit element is `XU1`. The
handoff accepts a pin map keyed by either name and writes it keyed by the SPICE
element. The schematic and BOM keep `U1`.

If a multi-pin part has no pin map, the handoff still writes the overlay and
lists the part under "Pin maps the PCB skill will ask for". It does not invent
a package pinout.

## Node aliases

SPICE ground is node `0`. The handoff maps it to the circuit's ground name:

```json
{"0": "GND"}
```

It also maps sanitized node names back to the original net names, for example
`5V` becomes SPICE node `N5V` and is aliased back to `5V`. Net names must not
contain whitespace; two nets that collapse to the same SPICE node (including
case-insensitive collisions) are rejected by spec validation.

## Simulation-only parts

Voltage sources, current sources, controlled sources and ideal switches are
excluded from the board. They are listed in the handoff and in the pin map's
`exclude` list.

This matters most for the buck scaffold: its `S1` is an ideal switch, not a
transistor. A real buck board needs a MOSFET, gate driver, controller and
feedback network. The scaffold verifies the power-stage equations; it is not a
complete converter schematic. Replace the ideal parts before layout.

Extra board-only parts (connectors, mounting holes, test points) go in
`pcb.extra_components`. They are added to the pin map's `exclude` so the PCB
importer keeps them even though they are absent from the SPICE netlist. A
mechanical part such as a mounting hole declares `"pads": {}` because it has no
electrical connection; every pad that does exist must name a net.

## What the handoff does not do

- It does not choose board dimensions, stackup, connectors or mounting holes.
- It does not guess a footprint from a part value.
- It does not assume logical pin 1 is package pin 1.
- It does not run DRC, DFM, ERC, impedance or thermal sign-off.
- It does not turn a generic SPICE model into a verified real part.
- It does not silently widen a power netclass. If the PCB skill warns that the
  `Power` class is too narrow for the estimated current, compute the width with
  `circuit_calc.py trace` and set `pcb.netclasses.Power.track_width_mm`.

The report and handoff document state the remaining gaps. Read them before
ordering boards.
