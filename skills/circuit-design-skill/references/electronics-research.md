# Research and evidence in electronics

The model's internal knowledge of electronics and physics is useful for
generating hypotheses. It is not a measurement, a datasheet, or a standard. This
file defines how to turn a hypothesis into evidence before it reaches a
schematic, a simulation claim, or a customer.

## The evidence hierarchy

Use the highest available source, and prefer two independent ones for a
critical number.

| Rank | Source | What it is good for | Limits |
| --- | --- | --- | --- |
| 1 | Datasheet, errata, current revision | Limits, conditions, pinout, thermal, timing | Can contain errors; check errata and revision |
| 2 | Vendor application note / design guide | Design procedure, layout, worked examples | Specific to the vendor's conditions |
| 3 | Standard (IEC, IPC, ISO, MIL, ...) | Test methods, creepage, safety, qualification | You must obtain the actual document; clause numbers are not optional |
| 4 | Reference design / evaluation board | A known-good starting point | Their requirements may differ from yours |
| 5 | Textbook / peer-reviewed paper | Theory, derivations, sanity checks | Not a part-specific truth |
| 6 | Reputable community / forum | Hints and failure modes | Unverified; trace back to 1-3 |
| 7 | Model memory | Hypothesis generation | Never cite as evidence |

When sources conflict, prefer the more specific and more recent one, and write
down the conflict. A datasheet's "typical" graph and a text value can disagree;
the conditions explain why. Do not average them silently.

## Search protocol

Start from the exact part number and the document type, not from a generic
topic. Useful queries:

```text
<part> datasheet filetype:pdf site:<vendor>.com
<part> errata
<part> application note filetype:pdf
<part> reference design schematic
<part> spice model
<part> IBIS model
<topology> design guide site:ti.com OR site:analog.com OR site:infineon.com
<topic> standard <number>
<topic> app note site:<vendor>.com
```

Rules:

- Prefer the vendor's own domain and the document number.
- Note the revision and date. If the page does not show one, look inside the PDF.
- Check errata before trusting a feature.
- For a model, record where it came from and its hash (`models.md`).
- For a formula, find a derivation or a datasheet example, not just a number.
- Save the URL and the access date; links rot.

## Reading a datasheet

Read in this order:

1. **Absolute maximum ratings** — the survival limit, not the operating range.
2. **Recommended operating conditions** — where the part is specified.
3. **Electrical characteristics** — min/typ/max, with the test conditions.
4. **Thermal information** — θJA/θJC, board size, airflow, thermal pad.
5. **Typical performance curves** — read the axes and the conditions; a curve
   is not a guaranteed limit.
6. **Application information** — layout, decoupling, stability, protection.
7. **Package and pinout** — footprint, pitch, thermal pad, pin 1, creepage.
8. **Errata** — known deviations from the datasheet.

Common mistakes:

- Treating a typical value as a maximum.
- Using a 25 °C value at 85 °C.
- Ignoring the test condition (load, airflow, frequency, DC bias).
- Reading a graph beyond its plotted range.
- Confusing absolute maximum with recommended operating.
- Forgetting the tolerance of the source and the reference.
- Assuming a package's θJA is a property of the die alone.

## Verify a calculation

Every critical number gets the same treatment:

1. **Write the equation with units.** If the units do not cancel, stop.
2. **Check the limits.** Does the result pass through zero, exceed a rail, or
   assume a negative resistance?
3. **Estimate the order of magnitude** before the exact calculation.
4. **Compute in Python** (`circuit_calc.py`, `verify.py`, or a short script),
   not in your head.
5. **Cross-check** with a second method: a datasheet example, a graph, a
   different formula, or a measurement.
6. **State the assumptions and the margin.** A number without both is not a
   result.

Example: a divider ratio is `Vout = Vin × R2 / (R1 + R2)`. Check the open-circuit
case, the loaded case, the tolerance extremes, and the input bias current of the
next stage. The nominal answer is the least interesting one.

## Simulation is evidence about the model

A simulation proves that the netlist and the model behave as written. It does
not prove the physical board. Before quoting a simulation result:

- state whether the model is generic, declared, or vendor-backed
  (`models.md`);
- state the analysis, the conditions, and the version of the simulator;
- check convergence and the initial operating point;
- compare at least one point with a hand calculation or a datasheet curve;
- keep the netlist and the verification JSON with the result.

If the model is generic, the honest statement is "smoke test", not "validated".

## Citation format

Use enough detail that a reviewer can find the exact document:

```text
Vendor. Part number datasheet. Document SBVS123C, revision C, March 2024.
Section 8.3, Figure 8-4. https://... (accessed 2026-11-08).
```

For a standard: number, edition/year, clause, and the test level. For a
measurement: instrument, range, bandwidth, ambient, sample size, and date.

Record sources in the spec's `design.sources` array and in the research log. A
source that exists only in a chat message is not part of the design record.

## Red flags

- A number with no source and no conditions.
- A part recommendation with no datasheet revision.
- A formula copied without a derivation or a units check.
- A simulation presented as a bench result.
- A vendor "typical application" treated as a verified design.
- A standard cited without the edition and clause.
- A model whose vendor, part, source, or hash is missing.
- A claim that a check passed when the tool was not installed or crashed.
- A result that is suspiciously close to the target with no tolerance analysis.

## When you cannot find an answer

Say so. Options, in order:

1. Find a more specific document (errata, app note, reference design).
2. Measure the part or the board.
3. Choose a part or topology with published data.
4. Add a margin and a verification step, and record the assumption.

Do not invent a datasheet value. Do not present a guess as a calculation. The
skill's value is not that it knows everything; it is that it shows its evidence
and its limits.
