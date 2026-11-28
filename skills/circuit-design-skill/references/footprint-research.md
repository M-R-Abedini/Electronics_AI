# Footprint research workflow

Footprint selection is research-first. Do not choose a footprint merely because
it is a familiar KiCad default or because the symbol has a familiar name.

## Research sequence

For each physical component that will appear on a PCB:

1. Identify the exact manufacturer part number (MPN). If it is unknown, use the
   component's electrical class and stated ratings, but mark the package as
   provisional and ask for the MPN when package compatibility matters.
2. Search the web using the MPN plus `datasheet package dimensions lead spacing`.
   For generic parts, use queries such as:
   - `KiCad common footprint 0.5 W axial resistor through hole`
   - `0.5W resistor DIN0309 body dimensions lead pitch datasheet`
   - `KiCad [part number] footprint package`
3. Prefer sources in this order:
   - Manufacturer datasheet and official mechanical drawing for the exact MPN.
   - Official KiCad footprint library and KiCad Library Convention (KLC).
   - Distributor drawings or reputable application notes.
   - Community discussions only to understand common practice; never treat a
     forum post as proof of package dimensions.
4. Search the official KiCad library for the footprint whose body dimensions,
   lead pitch, pad/drill sizes and orientation match the source drawing.
5. Record the selected footprint on that component in the design spec. Include
   the MPN, source URL, relevant dimensions/rating, and why the footprint
   matches in the design decision or research log. Preserve source URLs.
6. If sources disagree, prefer the manufacturer drawing for the exact part.
   If no exact match can be established, do not silently claim a match: select
   a clearly labelled provisional footprint or leave it unassigned and ask.
7. Before fabrication, verify the footprint's pad numbering against the
   datasheet pinout and inspect the KiCad 3D/mechanical view.

## Semiconductor and IC package selection: DIY-first

For every IC, transistor, MOSFET, diode, LED, regulator, or other
semiconductor, do not assign a footprint from its electrical class alone.
**First research the package options actually offered for the exact manufacturer
part number.** A generic TO-92, TO-220, DIP, or SOIC guess is not acceptable.

1. Search the exact MPN with `datasheet package options`, `ordering information`,
   and `package dimensions`. Open the manufacturer's own datasheet/product
   page and inspect the ordering table, package suffixes, and mechanical
   drawings. Enumerate all orderable package variants for that exact device.
2. Check lifecycle and availability: exclude package variants that are not
   orderable, obsolete, or only listed for a different electrical grade or
   device suffix. Do not assume two suffixes share the same pinout.
3. Rank the manufacturer's available packages for practical DIY assembly:
   - **First choice:** through-hole with accessible leads, e.g. DIP, TO-92,
     TO-220, TO-247, axial/radial leaded packages, if the exact MPN is offered
     in that package.
   - **Next:** large, widely spaced gull-wing SMD leads, e.g. SOIC, SOP,
     TSSOP where pitch and body size are manageable with hand soldering.
   - **Next:** larger leaded SMD packages such as SOT-223 or DPAK/TO-252,
     depending on thermal and soldering needs.
   - **Last:** fine-pitch or leadless packages such as QFN, DFN, LGA, BGA,
     CSP, and tiny SOT variants. Use these only when no more DIY-friendly
     manufacturer option meets electrical, thermal, cost, and availability
     requirements, or the user explicitly prefers them.
4. Confirm the chosen package is electrically and thermally adequate. For
   power devices, compare exposed tab/pad requirements, copper area, thermal
   resistance, voltage/current ratings and heat-sinking. A bigger package is
   not automatically electrically interchangeable.
5. Match the manufacturer's package drawing to an official KiCad footprint:
   body dimensions, lead pitch, lead span, pad numbering, exposed pad, pin-1
   mark, tab connection, and recommended land pattern. Prefer the exact
   manufacturer-recommended land pattern when available.
6. If the part is only sold in a non-DIY package, say so and offer an
   alternative part that has a suitable through-hole or hand-solderable package
   only if electrical and functional requirements permit. Do not silently
   substitute a different part or package.
7. Record the MPN, all considered package options, chosen package, why it is
   easiest to assemble, the manufacturer source URL/revision, and the matching
   KiCad footprint in the design decision/research log. If package options or
   pinout cannot be verified, leave the footprint unassigned and ask.

For a generic design without an MPN, choose the semiconductor part first based
on electrical requirements and availability, then apply this package research
workflow. Do not infer that a generic NPN means TO-92 or that a generic op-amp
means DIP-8. The assembly preference (through-hole) is a preference, not
permission to invent a package the manufacturer does not offer.

## Example: 0.5 W through-hole axial resistor

A useful search is `KiCad common footprint for 0.5W axial resistor`.
KiCad's `Resistor_THT:R_Axial_DIN0309_L9.0mm_D3.2mm_P12.70mm_Horizontal`
footprint describes a DIN0309 body of 9.0 x 3.2 mm, 12.70 mm lead pitch and
0.5 W (1/2 W) resistor use. This is a sensible starting candidate for a
typical DIN0309 0.5 W resistor, not a universal fit for every 0.5 W part:
verify the exact manufacturer's body dimensions and lead spacing first.

A smaller DIN0207 footprint is not interchangeable merely because it is also
an axial resistor. Power rating, body series and lead pitch must match the
actual component and assembly needs.

## Recording the result

Prefer a component-level explicit footprint in the normalized spec, for example:

```json
{
  "ref": "R1",
  "type": "R",
  "value": "10k",
  "power_w": 0.5,
  "manufacturer_part": "exact MPN if known",
  "footprint": "Resistor_THT:R_Axial_DIN0309_L9.0mm_D3.2mm_P12.70mm_Horizontal"
}
```

The `power_w` and `manufacturer_part` fields are descriptive metadata; the
`footprint` field is the explicit, research-backed selection consumed by the
generator. The assembly wrapper preserves it. It supplies offline defaults for common
passives only; semiconductor footprints intentionally remain unassigned until
manufacturer package research is completed.

## Source examples

- [KiCad Library Conventions: axial resistor naming](https://klc.kicad.org/footprint/f3.2/)
- [KiCad footprint library example: DIN0309 0.5 W, 12.70 mm pitch](https://github.com/KiCad/kicad-footprints/blob/master/Resistor_THT.pretty/R_Axial_DIN0309_L9.0mm_D3.2mm_P12.70mm_Horizontal.kicad_mod)
- [KiCad forum discussion: DIN0207 and common 10.16 mm pitch](https://forum.kicad.info/t/beginner-question-selecting-resistor-footprint/62620)

These examples illustrate library conventions and common practice; they do
not replace the exact component manufacturer's drawing.
