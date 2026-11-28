# Analog design notes

## Op-amp selection

Pick the part from the requirements, not from the parts drawer.

| Requirement | Parameter to check |
|---|---|
| DC accuracy | `Vos`, `Vos` drift, `Ib`, `Ios`, CMRR, PSRR |
| Bandwidth | GBW, unity-gain stable?, `f_3dB = GBW/Gain` |
| Large-signal speed | slew rate, full-power bandwidth, settling time |
| Noise | input voltage noise density, current noise density, `1/f` corner |
| Output drive | short-circuit current, swing vs load, capacitive-load tolerance |
| Supply | minimum supply, rail-to-rail input/output, quiescent current |
| Robustness | ESD rating, latch-up, input differential clamp current |

### Error budget

```
Vout_error = Vos + Ib*Rsource + Ios*(Rf||Rg) + Vnoise_gain*...
             + PSRR_error + CMRR_error + resistor_tolerance_error
```

Compute each term in volts at the output, then RSS them. Do not add worst-case
and RSS results interchangeably: worst-case is a bound, RSS is a statistical
estimate.

### Noise

Resistor thermal noise:

```
en = sqrt(4*k*T*R)  [V/sqrt(Hz)]
```

At 25 °C, `sqrt(4kT) = 1.28e-10`, so a 1 kΩ resistor has about
`4 nV/sqrt(Hz)`. A 10 kΩ resistor has about `12.8 nV/sqrt(Hz)`.

Total input-referred noise over a bandwidth `BW` (for a single pole, use the
noise equivalent bandwidth `1.57*fc`):

```
Vrms = en * sqrt(BW)
```

For a gain stage, the resistor noise at the input is dominated by the source
and feedback network. A low-noise op-amp cannot rescue a 1 MΩ feedback
resistor.

### Stability

- The loop gain is `Aol * beta`, where `beta` is the feedback factor.
- Phase margin below ~45° gives visible peaking; aim for 45–60°.
- Capacitive loads and input capacitance create extra poles.
- A unity-gain follower is the most demanding configuration for many op-amps.
- Simulate the loop with an AC analysis that breaks the loop and injects a
  test signal, or use the vendor's stability tool.

## References

| Type | Initial accuracy | Tempco | Notes |
|---|---|---|---|
| Zener | ±1–5% | 50–500 ppm/°C | Cheap, noisy, needs bias current |
| Buried zener | ±0.1% | 1–10 ppm/°C | Low noise, higher cost |
| Bandgap | ±0.05–1% | 5–50 ppm/°C | Common, good compromise |
| XFET / floating gate | ±0.05% | 3–20 ppm/°C | Low noise, low power |

A reference's output noise and long-term drift often dominate a 16-bit or
higher measurement. Filter the reference and place it close to the ADC.

## Analog layout and grounding

- Return high-current and digital currents to the supply, not through the
  sensitive analog ground.
- Use a single-point star ground only at low frequency; at high frequency use
  a ground plane and control the current paths.
- Keep the feedback node short and away from switching nodes.
- Guard high-impedance inputs with a guard ring driven at the same potential.
- Place decoupling capacitors at the pin, with a short loop to ground.
- Do not route a reference or a high-impedance node under a switching
  regulator.

## Data converters

### ADC drive

- The sampling capacitor must charge through the source impedance and the
  switch resistance within the acquisition time.
- Add a charge reservoir capacitor and a series resistor to isolate the buffer.
- Check the amplifier's settling to 0.5 LSB, not just its bandwidth.
- The reference input draws a switched current; decouple it locally.

### DAC output

- A string DAC has a code-dependent output impedance; a buffered R-2R is more
  uniform.
- The output glitch at major carry transitions can be significant.
- Reconstruct with a low-pass filter whose corner is below the update rate.

## Measurement practice

- Use a differential probe or a proper ground spring; a long ground lead
  creates false ringing.
- Measure noise with a bandwidth limit and know the oscilloscope's own noise
  floor.
- Verify the supply rails under load, not just at no load.
- Check the circuit at temperature extremes if the specification requires it.

## Sources

- TI, *Analog Engineer's Circuit Cookbook: Amplifiers*.
- TI, *Op Amps for Everyone* (SLOD006).
- Analog Devices, *Linear Circuit Design Handbook*.
- TI, *Sallen-Key Low-Pass Filter Design* (application notes on filter design).
- NXP, *UM10204 I2C-bus specification and user manual*.
