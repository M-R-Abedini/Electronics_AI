# KiCad file formats

The generator emits:

- `<name>.net` — KiCad netlist (S-expression), always.
- `<name>.kicad_sch` — KiCad 9 schematic (`(version 20250114)`).

The schematic uses an embedded symbol library and a global label at every pin
endpoint. Connectivity is therefore exact even though there are no routed
wires.

## Schematic structure

```
(kicad_sch
  (version 20250114)
  (generator "circuit-design-skill")
  (generator_version "9.0")
  (uuid "...")
  (paper "A4")
  (title_block ...)
  (lib_symbols
    (symbol "circuit:R" ...)
  )
  (symbol (lib_id "circuit:R") (at 127 50.8 0) (unit 1)
    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)
    (uuid "...")
    (property "Reference" "R1" ...)
    (property "Value" "1k" ...)
    (property "Footprint" "" ...)
    (property "Datasheet" "" ...)
    (property "Description" "" ...)
    (pin "1" (uuid "..."))
    (pin "2" (uuid "..."))
    (instances
      (project "design"
        (path "/<root-uuid>" (reference "R1") (unit 1))
      )
    )
  )
  (global_label "IN" (shape input) (at 127 44.45 90)
    (fields_autoplaced yes)
    (effects (font (size 1.27 1.27)) (justify left))
    (uuid "...")
    (property "Intersheetrefs" "${INTERSHEET_REFS}" ...)
  )
  (sheet_instances
    (path "/" (page "1"))
  )
  (embedded_fonts no)
)
```

Key points:

- `version` and `generator_version` must match the dialect. KiCad 9 uses
  `20250114` and `9.0`. Third-party generators should not claim to be
  `eeschema`.
- The root `uuid` is referenced by every symbol instance path.
- `lib_symbols` must contain a definition for every `lib_id` used. A missing
  definition makes the schematic load with broken symbols.
- Each placed symbol needs `(instances (project ... (path "/<root-uuid>" ...)))`.
- `sheet_instances` is required for a complete root schematic.
- `embedded_fonts no` is a KiCad 9 field; keep it.

## Embedded symbols and pin geometry

Pin coordinates are in the symbol's local frame, **Y up**. A pin's `(at x y
angle)` is the connection point; `angle` points from the connection point
toward the body. The placed symbol's rotation and mirror transform the local
point into schematic coordinates (**Y down**):

```
world = (sx + rx, sy - ry)
R0:   (px, py)
R90:  (-py, px)
R180: (-px, -py)
R270: (py, -px)
mirror x: ry = -ry
mirror y: rx = -rx
```

The generator's embedded symbols use these connection points:

| Type | Pin 1 | Pin 2 | Pin 3 | Pin 4 | Pin 5 |
|---|---|---|---|---|---|
| R | (0, 6.35) | (0, -6.35) | | | |
| C | (0, 5.08) | (0, -5.08) | | | |
| L | (0, 5.08) | (0, -5.08) | | | |
| D / LED / Zener | (0, 3.81) | (0, -3.81) | | | |
| NPN / PNP | C (0, 5.08) | B (-5.08, 0) | E (0, -5.08) | | |
| NMOS / PMOS | D (0, 5.08) | G (-5.08, 0) | S (0, -5.08) | | |
| Op-amp | In+ (-7.62, 2.54) | In- (-7.62, -2.54) | V+ (0, 7.62) | V- (0, -7.62) | Out (7.62, 0) |
| V / I / SW / E / G | see `scripts/symbols.py` | | | | |

## Pin numbering is SPICE order, not package order

This is deliberate and important:

| Device | This skill | Standard KiCad `Device:` |
|---|---|---|
| NPN / PNP | 1=C, 2=B, 3=E | 1=B, 2=E, 3=C |
| NMOS / PMOS | 1=D, 2=G, 3=S | 1=G, 2=D, 3=S |
| Diode | 1=A, 2=K | 1=K, 2=A |
| Op-amp | 1=In+, 2=In-, 3=V+, 4=V-, 5=Out | varies by symbol |

The reason is that the generated `.net` and the SPICE netlist must agree. If
the schematic used package pin numbers while the netlist used SPICE order, the
same design would describe two different circuits.

A MOSFET is a special case: the schematic has three logical pins (D/G/S), but
the SPICE line has a fourth bulk node. The generator ties bulk to the source
net. The PCB pin map must therefore provide `d`, `g`, `s` and `b`; for a
three-pad package, map `b` to the same pad as `s`. Otherwise the PCB importer
rejects the four-node MOSFET line.

**Consequence for PCB layout:** these pin numbers are logical, not physical.
Before handing the netlist to a PCB tool, map each logical pin to the real
footprint pad. `pcb-design-skill` accepts an explicit pin map for this. Do not
assume that logical pin 1 is package pin 1.

Generate the handoff with:

```bash
python3 scripts/pcb_handoff.py specs/board.json --out out/ --board 50x40
```

The handoff writes the PCB overlay, pin map and node aliases. It never guesses
a footprint or a package pinout; missing ones are left for the PCB skill's
visual picker. An op-amp drawn as `U1` is a SPICE subcircuit element `XU1`; the
handoff maps the reference and records the mapping. The overlay's
`"version": 1` is the handoff contract; see
[`pcb-handoff.md`](pcb-handoff.md#contract-version-1) for the versioning rules.

## Connectivity by global labels

A global label placed at the exact pin connection point connects to that pin.
The generator:

1. Computes every pin's world coordinate.
2. Places one global label per unique `(coordinate, net)` pair.
3. Names the label with the spec net.

This avoids autorouting bugs and makes the netlist independent of wire
geometry. The trade-off is visual: labels overlap the symbol pins unless the
schematic is cleaned up for presentation. The netlist is still correct.

A label at the wrong coordinate is electrically disconnected even if it looks
close. The verifier recomputes the pin coordinates from the **parsed** library
symbol and checks that a label with the right name exists at each one.

## Netlist (`.net`)

```
(export (version "E")
  (design
    (source "design.kicad_sch")
    (date "2026-01-01")
    (tool "circuit-design-skill")
  )
  (components
    (comp (ref "R1") (value "1k") (footprint "Resistor_SMD:R_0805_2012Metric")
      (description ""))
  )
  (nets
    (net (code "1") (name "GND")
      (node (ref "R1") (pin "2"))
    )
  )
)
```

- `code` is a string in the S-expression, but it is an integer value.
- Every pin in every net must appear as a `node`.
- Net names are the spec names; ground is not renamed in KiCad.
- The netlist is generated directly from the component list, so it cannot
  disagree with the `.kicad_sch` labels unless the schematic is edited.

## Validation

`scripts/verify.py` parses the schematic with `kiutils` when available:

```bash
PYTHONPATH=/path/to/kiutils python3 scripts/verify.py spec.json --out out/
```

It checks:

- the file parses as a KiCad 9 schematic;
- every component has a placed symbol and an embedded library definition;
- every pin endpoint has a global label with the expected net.

`kiutils` is optional. Without it, the check is reported as **skipped**, not
as passing. A structural pass is not a KiCad ERC or a DRC.

## Opening and editing

- KiCad 9 opens the file directly. Older KiCad versions may refuse the
  `20250114` version.
- After editing, run **ERC** in KiCad. Global labels are not power symbols;
  ERC may warn about a missing `PWR_FLAG` on a net driven only by a connector.
- If you convert the labels to wires, re-run the verifier or KiCad's netlist
  export and compare with `<name>.net`.
- To use the design in a PCB, map logical pins to physical pads first (see
  above), or use the generated handoff in `references/pcb-handoff.md`.

## Common KiCad gotchas

- A symbol instance without an `instances` block loads but loses its reference.
- A `lib_id` without a matching `lib_symbols` entry shows as a question mark.
- Pin `(at x y angle)`: the angle points toward the body, not away.
- Schematic Y is down; symbol library Y is up.
- A global label must be at the pin endpoint, not at the end of a wire that
  does not touch the pin.
- Mixing `(version 20231120)` symbols with a `20250114` header can fail to
  load; keep one dialect.
- Do not use `eeschema` as the generator name for third-party output.

## Sources

- KiCad developer documentation, *Schematic file format*:
  <https://dev-docs.kicad.org/en/file-formats/sexpr-schematic/>
- KiCad developer documentation, *Netlist file format*:
  <https://dev-docs.kicad.org/en/file-formats/sexpr-netlist/>
- KiCad, *Importing LTspice schematics*:
  <https://dev-docs.kicad.org/en/import-formats/ltspice/index.html>
- `kiutils` Python library (used only for validation):
  <https://github.com/mvnmgrx/kiutils>


## Generated schematic placement

The design spec may give a component an optional `placement` object with
`x_mm` and `y_mm` coordinates. These are KiCad schematic coordinates, not
PCB coordinates. Values must be positive, unique, and aligned to the 1.27 mm
grid. This lets an agent arrange functional blocks while preserving the
existing embedded symbol geometry and pin-to-net verification. Components
without explicit coordinates use the automatic grid. LTspice layout remains
independent.
