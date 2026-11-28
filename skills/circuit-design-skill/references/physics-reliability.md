# Physics and reliability

The simulator solves ideal equations. Real hardware fails at the edges:
tolerance, temperature, parasitics, derating and transients. Check these
before calling a design finished.

## Passive components

### Resistors

- Power: `P = I^2*R = V^2/R`. Derate to about 50–60% of the rated power at
  the maximum ambient.
- Voltage: check the maximum working voltage, especially for high-value
  resistors.
- Temperature coefficient: 100 ppm/°C over a 100 °C rise is a 1% change.
- Pulse energy: thick-film and thin-film parts have different surge ratings.
- Noise: `en = sqrt(4*k*T*R)`; high-value resistors are noisy.
- Self-heating changes the value; a 0.1% resistor is not 0.1% at 2 W.

### Capacitors

- Derate voltage to about 60–80% of rated, more for ceramics with a
  voltage-dependent dielectric.
- Class II ceramics (X5R, X7R) lose capacitance under DC bias, temperature and
  aging. A 10 µF 6.3 V X5R at 5 V may be 5 µF or less.
- Class I (C0G/NP0) is stable but limited in density.
- Electrolytics have ESR, ripple-current and lifetime limits:
  `life ~= L0 * 2^((T0 - Ta)/10)` (roughly, per 10 °C rule).
- Tantalum needs voltage derating (often 50%) and a current-limit or fuse.
- Dielectric absorption affects sample-and-hold and integrator circuits.
- ESL limits high-frequency decoupling; use small packages close to the pin.

### Inductors

- Saturation current must exceed the peak current with margin. Inductance
  falls sharply above saturation.
- RMS current sets the copper loss; core loss rises with frequency and ripple.
- Self-resonance limits the usable frequency.
- Shielded vs unshielded affects radiated fields.
- DCR adds to the converter loss and the output voltage error.

### Diodes

- Forward current and reverse voltage; derate both.
- Reverse recovery causes switching loss and ringing in rectifiers; use a
  Schottky or a fast-recovery part where appropriate.
- Leakage rises with temperature; a high-value pull-up may be affected.
- Zener tolerance and dynamic resistance; check the knee at low current.
- LEDs: `Vf` has a negative temperature coefficient and a steep current
  sensitivity.

## Thermal

```
Tj = Ta + P*(theta_jc + theta_cs + theta_sa)
```

- `theta_ja` from a datasheet is board-dependent; do not use it as a universal
  constant.
- Thermal vias and copper area dominate `theta_sa` for SMD power parts.
- Derate to about 50–60% of rated power and `Tj` below about 80% of the
  rated maximum.
- Check the thermal cycling and the solder-joint fatigue for large parts.
- A hot component changes nearby references and sensors; keep them apart.

## Printed-board limits (IPC-2221)

External trace, `k = 0.048`; internal trace, `k = 0.024`:

```
A_mils2 = (I / (k * dT^0.44))^(1/0.725)
```

This sizes for a temperature rise, not for voltage drop or impedance. Also
check:

- `I*R` drop and the allowed voltage error.
- Connector and via current ratings.
- Creepage and clearance for the working voltage (IEC 60664 / IPC-2221).
- Thermal relief on pads connected to planes.
- Fusing and fault current.

For high voltage, the clearance is set by the standard and the pollution
degree, not by a rule of thumb. Ask for the applicable standard.

## Electromigration and current density

On-chip and in fine traces, high current density causes electromigration.
Follow the manufacturer's current-density limit. For PCB copper, the IPC
temperature-rise rule is usually the binding constraint, but check the
voltage-drop and the connector.

## ESD and transients

- The IC's internal ESD diodes are for handling, not for repeated external
  events.
- Add a TVS at the connector, with a short return path.
- Check the clamp voltage at the rated surge current, not the standoff
  voltage.
- A series resistor or ferrite improves the protection but changes the signal.
- Common-mode transients need a common-mode choke or an isolated interface.

## Tolerance

- Worst-case adds absolute tolerances; RSS combines independent random
  errors as `sqrt(sum(sigma_i^2))`.
- A 1% resistor is 1% at 25 °C; add the temperature coefficient and the
  load-life drift.
- Use the sensitivity `S_i = (dF/dx_i)*(x_i/F)` to find which component
  dominates. The tolerance calculator reports these.
- Monte Carlo is the honest way to estimate a yield; RSS is an approximation
  that assumes independent Gaussian errors.

## Reliability derating

| Stress | Typical derating |
|---|---|
| Resistor power | 50–60% of rated |
| Capacitor voltage | 60–80% of rated (more for ceramics) |
| Tantalum voltage | 50% of rated |
| Semiconductor voltage | 70–80% of rated |
| Semiconductor current | 70–80% of rated |
| Junction temperature | ≤ 80% of rated `Tj_max` |
| Connector current | 50–70% of rated |

These are starting points, not a substitute for the datasheet and the
applicable standard.

## Sources

- IPC-2221B, *Generic Standard on Printed Board Design*.
- IPC-9592, *Requirements for Power Conversion Devices*.
- IEC 60664, *Insulation coordination for equipment within low-voltage supply
  systems*.
- Vendor application notes on capacitor derating, inductor saturation and
  thermal design.
