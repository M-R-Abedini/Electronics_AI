# Topologies and equations

Every equation here is implemented in `scripts/circuit_calc.py` and exercised
by `tests/`. Symbols: `R` in ohms, `C` in farads, `L` in henries, `f` in hertz,
`V` in volts, `I` in amperes, `T` in kelvin unless stated.

## Passive networks

### Resistive divider

```
Vout = Vin * R2 / (R1 + R2)
Rout = R1 || R2
I    = Vin / (R1 + R2)
P_R1 = I^2 * R1
```

Errors come from resistor tolerance and load current. Keep the divider current
at least 10x the load current, or buffer it. For a 1% divider the ratio error is
approximately the sum of the two resistor tolerances.

### RC low-pass

```
fc   = 1 / (2*pi*R*C)
tau  = R*C
|H(f)| = 1 / sqrt(1 + (f/fc)^2)
phase  = -atan(f/fc)
```

Source impedance adds to `R`; load resistance appears in parallel with `C`.
A 10x rule keeps loading error under ~10%; 100x keeps it under ~1%.

### RC high-pass

```
fc = 1 / (2*pi*R*C)
|H(f)| = (f/fc) / sqrt(1 + (f/fc)^2)
```

### RL

```
fc = R / (2*pi*L)
tau = L / R
```

### Series/parallel RLC

```
f0 = 1 / (2*pi*sqrt(L*C))
Z0 = sqrt(L/C)
series: Q = (1/R)*sqrt(L/C),  BW = f0/Q
parallel: Q = R*sqrt(C/L)
```

At resonance the series impedance is `R` and the parallel impedance is `R`
(ideal components). Real inductors have winding resistance and self-capacitance;
real capacitors have ESR and ESL.

## Amplifiers

### Non-inverting op-amp

```
Gain = 1 + Rf/Rg
Rin  = very high (op-amp input)
Rout = op-amp Rout / loop gain
```

Choose `Rf || Rg` near the source impedance to balance bias-current errors.
The DC output error from input bias current is `Ib * (Rf || Rg)`.

### Inverting op-amp

```
Gain = -Rf/Rin
Rin  = Rin
Rout = op-amp Rout / loop gain
```

The inverting input is a virtual ground, so the source must drive `Rin`.
Add a resistor equal to `Rf || Rin` from the non-inverting input to ground to
balance bias currents.

### Differential amplifier (four resistors)

```
Vout = (Rf/Rin) * (V+ - V-)     when R1=Rin, R2=Rf, R3=Rin, R4=Rf
CMRR depends on resistor matching; 0.1% resistors give roughly 60 dB
```

### Instrumentation amplifier

Three-op-amp in-amps set gain with one resistor:

```
Gain = 1 + 2*Rf/Rg
```

CMRR and input impedance are set by the input buffers, not the difference
stage. Check the input common-mode range and the output swing separately.

### Op-amp bandwidth and slew rate

```
GBW >= 10 * Gain * f_max        (20 dB loop-gain margin, rule of thumb)
SR  >= 2*pi*f_max*Vpeak          (full-power bandwidth)
f_3dB_closed_loop ~= GBW / Gain
```

The 10x factor is a stability/linearity margin, not a law. For a
Butterworth response or a fast settling requirement, verify with a SPICE AC
and transient analysis. A rail-to-rail output still has a finite output swing
and a load-dependent drop.

### Op-amp stability checklist

- Noise gain, not signal gain, sets the loop.
- A capacitive load directly on the output can oscillate; add `Riso` (10–100 Ω)
  in series and/or a feedback capacitor.
- The feedback pole from `Rf || C_in` can destabilise a high-speed part.
- Check phase margin at the actual load and supply.
- Do not use an op-amp below its minimum stable gain without a datasheet
  guarantee.

## Active filters

### Sallen-Key low-pass, unity gain

```
w0 = 1 / sqrt(R1*R2*C1*C2)
Q  = sqrt(R1*R2*C1*C2) / (C2*(R1+R2) + R1*C1*(1-K))
K  = 1 for the unity-gain follower
```

For `R1 = R2 = R`, `C1 = 2*C2` gives a Butterworth `Q = 0.7071` and
`fc = 1/(2*pi*R*sqrt(C1*C2))`.

### Sallen-Key high-pass, unity gain

Topology: `C1` from input to node A, `C2` from A to the op-amp `+` input,
`R1` from A to the output, `R2` from `+` to ground.

```
w0 = 1 / sqrt(R1*R2*C1*C2)
Q  = sqrt(R1*R2*C1*C2) / (R1*(C1+C2) + R2*C2*(1-K))
```

For `C1 = C2 = C`, `R2 = 2*R1` gives Butterworth. Do **not** reuse the
low-pass Q formula with labels swapped: the `R1*(C1+C2)` term is not
symmetric.

### General second-order response

```
H(s) = K*w0^2 / (s^2 + (w0/Q)*s + w0^2)     low-pass
H(s) = K*s^2   / (s^2 + (w0/Q)*s + w0^2)     high-pass
```

Butterworth `Q = 0.7071`; Bessel `Q = 0.5774` (best step response);
Chebyshev ripple depends on `Q` and order. `Q > 5` is very sensitive to
component tolerance.

### Higher order

Cascade second-order sections. A 4th-order Butterworth is two sections with
`Q = 0.5412` and `Q = 1.3066`. Do not cascade identical `Q = 0.707` sections
and call it Butterworth.

## Transistors

### BJT bias (common emitter, emitter degeneration)

```
Ve   = Ie*Re ~= Ic*Re
Vb   = Ve + Vbe,  Vbe ~= 0.65...0.75 V
Vc   = Vcc - Ic*Rc
Vce  = Vc - Ve
Ib   = Ic/beta
Divider: I_div = k*Ic, k ~ 10 (stiffness); Ib = Ic/beta
Av   ~= -Rc / (Re + re),  re = Vt/Ic ~= 26 mV/Ic at room temperature
```

Bias stability: the emitter resistor sets the DC operating point; the bypass
capacitor sets the AC gain. A larger divider stiffness `k` makes `Vb` less
sensitive to `beta` but wastes supply current. `Vce` should sit near `Vcc/2`
for maximum swing. Check `Pc = Vce*Ic` against the package thermal limit.

### MOSFET bias

```
Triode:    Id = k*( (Vgs-Vth)*Vds - Vds^2/2 )
Saturation: Id = (k/2)*(Vgs-Vth)^2*(1 + lambda*Vds)
```

For a switching MOSFET, gate charge and `Rds(on)` matter more than the
square-law. Check `Vgs(th)`, `Qg`, `Ciss`, `Rds(on)` at the actual gate drive,
and the SOA for inductive loads.

### Current mirror

```
Iout/Iref ~= (W/L)out / (W/L)ref
```

Add source degeneration for matching, and cascode for output impedance.
BJT mirrors have a `Vbe` mismatch of a few mV and a beta error.

## Power converters

### Buck (CCM)

```
D    = (Vout + Vf) / (Vin + Vf)
L    = (Vin - Vout)*D / (r*Iout*fsw)
Ipk  = Iout*(1 + r/2)
Cout >= r*Iout / (8*fsw*dVout)
Lcrit = (Vin - Vout)*D / (2*Iout*fsw)
```

`r` is the peak-to-peak ripple ratio (typically 0.2–0.4). Below `Lcrit` the
converter enters DCM. Input capacitor RMS current is approximately
`Iout*sqrt(D*(1-D))`. The high-side switch sees `Vin` plus ringing; derate
the voltage rating by at least 20–30%.

### Boost (CCM)

```
D    = 1 - Vin/(Vout + Vf)
IL   = Iout/(1-D)
L    = Vin*D / (r*IL*fsw)
Cout >= Iout*D / (fsw*dVout)
```

A boost has a right-half-plane zero at approximately
`f_RHP = (1-D)^2*Rload / (2*pi*L)`, which limits achievable loop bandwidth.
It also has no output current limit when the switch fails short: add a fuse
or a protection switch.

### LDO

```
Pdiss = (Vin - Vout)*Iout + Vin*Iq
Tj    = Ta + Pdiss*(theta_ja)
```

Dropout voltage is current- and temperature-dependent. Stability often needs
a specific output capacitor ESR range; check the datasheet. An LDO cannot
step up, and it cannot sink current.

### Zener shunt regulator

```
Rs_max = (Vin_min - Vz) / (Iz_min + Iload_max)
Rs_min = (Vin_max - Vz) / (Iz_max + Iload_min)
Pz     = Vz * Iz
```

Choose `Rs` between the limits, then check power at high line and no load.
Zener tolerance is typically ±5%, and the dynamic resistance degrades
regulation.

### LED

```
Rs = (Vs - n*Vf) / If
P_Rs = If^2 * Rs
```

`Vf` has a strong temperature coefficient (about -2 mV/°C for a red LED) and a
steep current sensitivity. Never drive an LED from a voltage source without a
series resistor or a current regulator.

## Interfaces

### I2C pull-ups (NXP UM10204)

```
Rp(min) = (VDD - VOL) / IOL
Rp(max) = tr / (0.8473 * Cb)
```

`Cb` is the total bus capacitance including traces, connectors and pins.
Standard mode 100 kHz, fast mode 400 kHz, fast-mode-plus 1 MHz. Check the
input leakage current and the bus capacitance limit (400 pF for standard/fast
mode).

### SPI / UART

- SPI is push-pull and point-to-point; source-series termination may be needed
  for fast edges and long traces.
- UART is asynchronous; the receiver samples at the bit centre. Total clock
  error must stay below about ±2–3% for a 10-bit frame.
- Series resistors (22–100 Ω) reduce ringing but also slow the edge; verify
  with the actual driver and load.

### ADC

```
SNR_ideal = 6.02*N + 1.76 dB
ENOB      = (SINAD - 1.76) / 6.02
LSB       = Vref / 2^N
```

The analog input must settle to within 0.5 LSB in the acquisition time. Drive
with a low-impedance buffer and a charge reservoir; check the sampling switch
charge injection. A reference with 1% initial error and 50 ppm/°C drift limits
absolute accuracy regardless of ADC resolution.

## Digital

```
Noise margin high = VOH(min) - VIH(min)
Noise margin low  = VIL(max) - VOL(max)
Fanout = min(IOL/IIL, IOH/IIH)
```

CMOS inputs are capacitive; DC fanout is usually large, but the dynamic load is
`C_load*V*f`. Check the total power dissipation and the package thermal limit.

## Thermal

```
Tj = Ta + P*(theta_jc + theta_cs + theta_sa)
theta_sa = (Tj_max - Ta)/P - theta_jc - theta_cs
```

Use the actual copper area for `theta_sa`; the datasheet value is for a
specific board. Derate power to about 50–60% of the rated maximum and keep
`Tj` below about 80% of the rated maximum for long life.

## Trace width (IPC-2221)

```
A = (I / (k * dT^0.44))^(1/0.725)     [A in mils^2]
k = 0.048 external, 0.024 internal
```

This is a thermal sizing rule, not an impedance or voltage-drop rule. For
high-current or low-voltage rails, also check `I*R` drop and connector rating.


## Inverting op-amp amplifier

For an ideal inverting amplifier:

- Signal input connects to the inverting node through (R_{in}).
- Feedback resistor (R_f) connects output to the inverting node.
- Non-inverting input is referenced to ground (or the intended bias reference).
- Closed-loop gain is (A_v=-R_f/R_{in}).

Scaffold:

```bash
python3 scripts/design_spec.py scaffold --topology opamp_inverting \
  --param gain=10 --param rin=10k --out specs/inverting.json
```

The `gain` parameter is the magnitude; the circuit polarity is inverting. The
scaffold adds explicit positive/negative supply rails and an optional output
load. Check input common-mode range, output swing, GBW, slew rate, input bias
current, offset, noise, resistor noise and output current against the selected
op-amp datasheet. The idealized gain check does not validate those nonideal
limits.
