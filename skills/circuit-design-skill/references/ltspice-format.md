# LTspice file formats

Two files matter:

- `.cir` / `.net` — the SPICE netlist. **This is the simulation source of
  truth.**
- `.asc` — the schematic. It is a drawing plus attributes; LTspice netlists it
  when you press Run.

The generator writes both from the same component list. The `.asc` uses a net
label (`FLAG`) at every pin instead of routed wires, so the drawing cannot
silently connect the wrong nodes.

## Netlist (`.cir`)

```
* comment
V1 IN 0 DC 5 AC 1
R1 IN OUT 1k
C1 OUT 0 100n
.model DMY D(Is=1e-14 N=1 Rs=0.1)
.subckt OPAMP inp inn vp vn out
...
.ends OPAMP
.op
.ac dec 200 10 1Meg
.tran 1u 10m
.end
```

Rules:

- The first line is a comment/title.
- Node `0` is ground. The generator maps the spec ground net to `0`.
- Element letters: `R`, `C`, `L`, `V`, `I`, `D`, `Q`, `M`, `X`, `S`, `E`, `G`.
- BJT order is collector, base, emitter: `Q1 C B E model`.
- MOSFET order is drain, gate, source, bulk: `M1 D G S S model`.
- A subcircuit call is `XU1 inp inn vp vn out OPAMP_U1`.
- Model names are case-insensitive in SPICE but keep them consistent.
- `m` is milli (1e-3); `meg` is mega (1e6); `mil` is 25.4 µm. `1M` is one
  milliohm in SPICE. The value parser warns about this.
- `.end` is required.

### Analyses

```
.op
.ac dec <points> <start> <stop>
.tran <step> <stop> [start]
.dc <source> <start> <stop> <step>
.noise v(<out>) <source> dec <points> <start> <stop>
.tf v(<out>) <source>
```

`.ac dec` sweeps logarithmically; `.ac oct` and `.ac lin` also exist.
For a transient analysis, the maximum timestep defaults to `(stop-start)/50`;
set it explicitly when switching edges matter.

### Sources

```
V1 in 0 DC 5
V2 in 0 DC 0 AC 1
V3 pwm 0 PULSE(0 5 0 1n 1n 5u 10u)
V4 s 0 SIN(0 1 1k)
V5 p 0 PWL(0 0 1u 5 2u 5 3u 0)
```

`PULSE(V1 V2 Tdelay Trise Tfall Ton Tperiod)`.
`SIN(Voffset Vamp Freq Tdelay Theta)`.
`PWL(t1 v1 t2 v2 ...)`.

## Schematic (`.asc`)

```
Version 4
SHEET 1 880 680
SYMBOL res 480 160 R0
SYMATTR InstName R1
SYMATTR Value 1k
SYMBOL cap 760 160 R0
SYMATTR InstName C1
SYMATTR Value 100n
FLAG 496 176 IN
FLAG 496 256 OUT
FLAG 776 160 OUT
FLAG 776 224 0
TEXT -260 160 Left 2 !.include design.models
TEXT -260 200 Left 2 !.op
TEXT -260 224 Left 2 !.ac dec 200 10 1Meg
```

### Coordinates

- `.asc` coordinates are integers on a 16-unit grid; +Y is **down**.
- `SYMBOL <name> <x> <y> <orientation>`.
- Orientation is `R0`, `R90`, `R180`, `R270`, optionally followed by `M`
  (mirror), e.g. `R90M`.
- A `FLAG <x> <y> <net>` at a pin's exact coordinate names that pin's node.
  Flags at every pin are electrically valid without wires.
- `TEXT <x> <y> <alignment> <size> !<directive>` embeds a SPICE directive.
  The `!` prefix is required for a directive.

### Symbol pin geometry (rotation R0)

These are the verified local pin coordinates used by the generator. `res`,
`cap`, `ind`, `diode`, `LED`, `zener`, `voltage` and `current` are standard
LTspice symbols. The transistor and op-amp coordinates are the vendor
schematic reference values.

| Type | Symbol | Pin 1 | Pin 2 | Pin 3 | Pin 4 | Pin 5 |
|---|---|---|---|---|---|---|
| R | `res` | (16,16) | (16,96) | | | |
| C | `cap` | (16,0) | (16,64) | | | |
| L | `ind` | (16,16) | (16,96) | | | |
| V | `voltage` | (0,16) | (0,96) | | | |
| I | `current` | (0,0) | (0,80) | | | |
| D | `diode` | (16,0) | (16,64) | | | |
| LED | `LED` | (16,0) | (16,64) | | | |
| Zener | `zener` | (16,0) | (16,64) | | | |
| NPN | `npn` | C (64,0) | B (0,48) | E (64,96) | | |
| PNP | `pnp` | C (64,0) | B (0,48) | E (64,96) | | |
| NMOS | `nmos` | D (48,0) | G (0,80) | S (48,96) | | |
| PMOS | `pmos` | D (48,0) | G (0,80) | S (48,96) | | |
| Op-amp | `opamp2` | In+ (-32,80) | In- (-32,48) | V+ (0,32) | V- (0,96) | Out (32,64) |
| Switch | `sw` | (0,16) | (0,96) | (-48,80) | (-48,32) | |

### Orientation transform

For a symbol placed at `(sx, sy)` with local pin `(px, py)`:

```
R0:   (sx + px, sy + py)
R90:  (sx - py, sy + px)
R180: (sx - px, sy - py)
R270: (sx + py, sy - px)
mirror: negate the relative x after rotation
```

A pin that is one grid unit off produces a netlist with an `NC_*` node while
looking almost correct. Recompute, do not eyeball.

### Symbol attributes

```
SYMATTR InstName R1
SYMATTR Value 1k
SYMATTR Value2 AC 1
SYMATTR SpiceLine ...
```

- `Value` is the component value or the model/subcircuit name.
- `Value2 AC 1` sets the small-signal amplitude for a source.
- A semiconductor's `Value` is its model name; the model must be in the
  netlist or in an included file.
- An op-amp drawn with `opamp2` uses `Value` as the subcircuit name. The
  generator emits `OPAMP_<ref>` and includes the matching `.subckt` in the
  `.models` file.

### Includes

`TEXT ... !.include design.models` references a file relative to the `.asc`.
The generator writes `<name>.models` next to the `.asc`. The `.cir` inlines
the same cards so it is self-contained.

## Generic models

Generic cards are placeholders. They let the netlist run and catch gross
errors, but they are **not** the real device. Supply vendor models in the
spec's `models` object for any result that matters. A report that says
"generic models" is not a verified design.

## Common LTspice gotchas

- `1M` is milli, not mega. Use `1Meg`.
- A node label with a space is invalid; the generator sanitises names.
- A floating node with only a capacitor gives "singular matrix" in `.op`.
- An ideal voltage source in parallel with another ideal voltage source is
  singular.
- A `.subckt` pin order must match the `X` call exactly.
- The `opamp2` symbol is a five-pin symbol; the subcircuit order is
  `in+ in- v+ v- out`.
- LTspice's universal op-amp is not the same as a vendor model; do not claim
  vendor-accurate bandwidth or noise from it.
- `Version 4` is the broad-compatibility header. `Version 4.1` enables newer
  features; use it only when needed.

## Sources

- Analog Devices, *LTspice Schematic Reference*:
  <https://analogdevicesinc.github.io/ltspice-reference/ai_ref/SCHEMATIC-REFERENCE.html>
- Analog Devices, *LTspice Help: Circuit Elements* and *Dot Commands*.
- KiCad, *Importing LTspice schematics*:
  <https://dev-docs.kicad.org/en/import-formats/ltspice/index.html>
