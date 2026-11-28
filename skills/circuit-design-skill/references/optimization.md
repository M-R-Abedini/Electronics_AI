# Automated design optimization

## Current scope

`scripts/optimize.py` currently optimizes a first-order `rc_lowpass` spec. It
searches discrete E-series values for one resistor and one capacitor and ranks
candidates by the maximum absolute cutoff-frequency error at the exact
independent R/C tolerance corners.

Example:

```bash
python3 scripts/optimize.py examples/rc-lowpass.json --target-fc 1.59k \
  --r-min 1k --r-max 100k --c-min 10n --c-max 1u \
  --r-tol 1 --c-tol 10 --max-error-percent 5 \
  --out-spec out/rc-optimized.json --require-feasible
python3 scripts/verify.py out/rc-optimized.json --out out/optimized/ --require simulate
```

## Search objective

For each candidate pair:

`fc_nom = 1 / (2*pi*R*C)`

`fc_min = 1 / (2*pi*Rmax*Cmax)`

`fc_max = 1 / (2*pi*Rmin*Cmin)`

The score is:

`max(abs(fc_min - fc_target), abs(fc_max - fc_target)) / fc_target`

The report includes the nominal error, min/max cutoff, worst-case error, and
whether the candidate meets `--max-error-percent`. It returns the ranked
candidate list so an agent can compare alternatives rather than hiding the
trade-off.

## Assumptions and limits

- Ideal zero-ohm source and unloaded output.
- Independent symmetric R/C tolerances.
- No temperature coefficients, aging, voltage coefficients, parasitics or
  correlated variation unless explicitly represented by the tolerance inputs.
- One first-order resistor-capacitor low-pass topology; it does not optimize
  loaded filters, active filters, power converters or nonlinear circuits.
- A candidate is not accepted until the actual source/load is included in the
  circuit model, the spec is simulated, and operating stresses are checked
  against current component datasheets.

## Next extensions

1. Loaded RC and divider optimization with source/load constraints.
2. Sallen-Key and active-filter optimization with GBW, slew-rate and stability
   constraints.
3. Power-stage optimization with efficiency, ripple, thermal and SOA limits.
4. Multi-objective Pareto ranking for cost, power, noise, area and margin.
5. Closed-loop candidate generation, simulation, constraint evaluation and
   re-optimization, retaining each candidate and verification report.
