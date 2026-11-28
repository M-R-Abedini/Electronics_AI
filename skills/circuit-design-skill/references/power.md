# Power design notes

## Loss and thermal budget

```
P_loss = P_in - P_out
eff    = P_out / P_in
Tj     = Ta + P_loss*(theta_jc + theta_cs + theta_sa)
```

For a linear regulator, `P_loss = (Vin - Vout)*Iout + Vin*Iq`. For a switching
converter, split the loss into conduction, switching, gate drive, inductor
copper/core and quiescent loss. The datasheet efficiency curve is a starting
point, not a guarantee for a different inductor or layout.

Derate to roughly 50–60% of the rated power and keep `Tj` below about 80% of
the rated maximum for long life. Check the SOA for any transient.

## LDO

- Dropout voltage rises at low temperature and high current.
- Many LDOs need a minimum output capacitor ESR for stability; some need a
  maximum. Read the stability section.
- An LDO cannot sink current. If the load can return current, add a dummy load
  or a different topology.
- Reverse current from output to input can damage some LDOs; add a Schottky
  diode when the output can be held up.
- PSRR falls with frequency; add a ferrite and a capacitor for high-frequency
  rejection.

## Buck converter

Design sequence:

1. Choose `fsw` above the audio band, below the EMI limit and within the
   controller's range.
2. Compute duty `D = (Vout + Vf)/(Vin + Vf)`.
3. Choose ripple ratio `r = 0.2...0.4`; compute `L`.
4. Compute peak and RMS currents; check the inductor saturation and thermal
   rating.
5. Compute `Cout` from the ripple and transient requirement; check ESR.
6. Compute the input capacitor RMS current and voltage rating.
7. Check the switch and diode voltage/current ratings with ringing margin.
8. Compensate the loop; verify the crossover and phase margin.
9. Lay out the hot loop (input cap, switch, diode/return) with minimum area.

Notes:

- The inductor saturation current must exceed the peak current plus margin.
- The output capacitor's ESR and ESL set the high-frequency ripple.
- The feedback divider should be close to the controller; keep the node short.
- Do not route the switch node near the feedback or the analog section.
- Light-load efficiency may require pulse-skipping or a low-Iq mode; check the
  resulting ripple.

## Boost converter

- Duty `D = 1 - Vin/(Vout + Vf)`.
- Input current is higher than the output current: `IL = Iout/(1-D)`.
- Right-half-plane zero limits the loop bandwidth:
  `f_RHP ~= (1-D)^2*Rload/(2*pi*L)`.
- A failed short switch connects input to output; add a fuse or a disconnect.
- The output diode and capacitor see the full output voltage plus ringing.
- Start-up inrush can be large; check the input source and the inductor.

## Buck-boost, SEPIC, flyback

These are beyond the built-in calculators. Use the vendor's design tool and
verify the transformer/inductor, the loop and the isolation. Do not extrapolate
buck equations to these topologies.

## Zener shunt

- Choose `Rs` from the current window; check power at high line and no load.
- Zener tolerance is typically ±5%; dynamic resistance degrades regulation.
- A shunt regulator wastes the difference between input and output; it is
  suitable for low current and low power only.

## LED drive

- A series resistor is acceptable for a stable supply and a known `Vf`.
- For high power or wide temperature, use a constant-current driver.
- `Vf` drops about 2 mV/°C for a red LED; current rises sharply with voltage.
- Check the LED thermal pad and the maximum junction temperature.
- PWM dimming frequency should be above the visible flicker range; check the
  driver's minimum on-time.

## Battery

```
Runtime ~= Capacity_Ah / (I_avg / efficiency)
Peukert: t = H*(C/(I*H))^k    (H = rated hours, k = Peukert exponent)
P = I^2*R_internal
```

The simple runtime formula assumes a constant current and a capacity rating at
the same discharge rate. Add the converter efficiency and use Peukert when the
load current differs greatly from the rated current.

- Lithium cells need overvoltage, overcurrent, overdischarge and thermal
  protection. Never charge a lithium cell without a dedicated charger IC.
- The internal resistance rises at low temperature and at end of life.
- A fuel gauge is more accurate than a voltage measurement, especially for
  flat discharge curves.
- Check the connector polarity and add reverse-polarity protection.

## Protection

| Threat | Device | Notes |
|---|---|---|
| ESD | TVS diode | clamp voltage below the protected pin's abs max |
| Reverse polarity | series MOSFET or Schottky | MOSFET has lower drop |
| Overvoltage | TVS, clamp, or crowbar | check energy and response time |
| Overcurrent | fuse, eFuse, current limit | check trip time and I²t |
| Inrush | NTC, active limiter | NTC wastes power at steady state |
| Surge | MOV + TVS + fuse | coordination matters |

## Layout

- Minimise the area of the high-di/dt loop: input capacitor → switch →
  diode/ground → back to the capacitor.
- Use a solid ground plane under the converter.
- Keep the switch node short and away from sensitive traces; it is a good
  antenna.
- Place the feedback divider at the output sense point, not at the inductor.
- Use kelvin connections for current sense resistors.
- Thermal vias under the power pad; check the solder voiding and the copper
  area.

## Sources

- TI, *Basic Calculation of a Buck Converter's Power Stage* (SLVA477).
- TI, *Basic Calculation of a Boost Converter's Power Stage* (SLVA372).
- TI, *Linear Regulator Design Guide*.
- IPC-2221B, *Generic Standard on Printed Board Design*.
- Vendor datasheets for the specific controller, inductor and capacitor.
