# Digital design notes

## Logic levels and noise margin

Always compute the margin from the actual driver and receiver datasheets, at
the actual supply voltage and temperature.

```
NMH = VOH(driver,min) - VIH(receiver,min)
NML = VIL(receiver,max) - VOL(driver,max)
```

Typical 5 V TTL: `VOH >= 2.4 V`, `VIH >= 2.0 V`, `VIL <= 0.8 V`,
`VOL <= 0.4 V` → `NMH = 0.4 V`, `NML = 0.4 V`.
Typical 3.3 V LVCMOS: `VOH >= 2.4 V`, `VIH >= 2.0 V`, `VIL <= 0.8 V`,
`VOL <= 0.4 V` → the same 0.4 V margins.

Do not connect a 5 V push-pull output directly to a 3.3 V input unless the
input is 5 V tolerant. Use a level shifter or a series resistor plus a clamp
only when the datasheet explicitly allows the clamp current.

## Fanout and drive

```
DC fanout  = min(IOL/IIL, IOH/IIH)
Dynamic load = C_load * V * f
P_dynamic  = C_load * V^2 * f
```

CMOS DC fanout is usually limited by leakage, not by current. The real limit
is often the total load capacitance and the resulting edge rate. A slow edge
spends more time in the linear region and increases crowbar current.

## Timing

```
Setup:  t_co(max) + t_flight(max) + t_setup <= T - t_skew
Hold:   t_co(min) + t_flight(min) >= t_hold + t_skew
```

For source-synchronous interfaces, the clock and data fly together; for a
common clock, the skew between clock and data dominates. Include connector,
package and probe loading. At high speed, treat traces as transmission lines:

```
t_r > 2*length/v  → lumped approximation is adequate
v ~= c/sqrt(er_eff)
```

For FR-4, `v ~= 15 cm/ns` for stripline, a little faster for microstrip.

## Buses

### I2C

- Open-drain, needs pull-ups on SDA and SCL.
- `Rp(min) = (VDD - VOL)/IOL`; `Rp(max) = tr/(0.8473*Cb)`.
- Bus capacitance limit: 400 pF (standard/fast mode), 550 pF (fast-mode plus).
- Clock stretching and arbitration require the master to support them.
- Series resistors are sometimes used for ESD, but they add to the rise time.
- Level shifting between 5 V and 3.3 V is usually a dedicated translator or a
  MOSFET-based shifter; a simple resistor divider is too slow and loads the bus.

### SPI

- Push-pull, point-to-point, four wires (SCLK, MOSI, MISO, CS).
- Mode (CPOL/CPHA) must match exactly.
- Long or fast traces need source-series termination; a 33–100 Ω resistor at
  the driver is a common starting point.
- Multiple slaves need separate CS lines, or a decoder.

### UART

- Asynchronous; the receiver samples at the bit centre.
- Total clock error must be below about ±2–3% for a 10-bit frame.
- Idle high; a start bit is a high-to-low transition.
- Add a series resistor and TVS for external connections.

### USB / Ethernet / CAN

These are standards with hard electrical requirements. Do not improvise
termination, magnetics, ESD or impedance. Use the reference design and the
compliance test.

## Level shifting

| Direction | Method | Caveats |
|---|---|---|
| 5 V → 3.3 V | resistor divider | slow, loads the source, not for high speed |
| 5 V → 3.3 V | series resistor + clamp | only if the input is 5 V tolerant |
| 5 V → 3.3 V | dedicated translator | check direction and enable timing |
| 3.3 V → 5 V | 5 V-tolerant open-drain + pull-up | rise time and bus capacitance |
| Bidirectional | MOSFET translator | body diode and pull-up sizing |
| Isolated | digital isolator | propagation delay, skew, power |

## Power-up and sequencing

- A floating input is not a valid logic level. Add pull-ups or pull-downs.
- Avoid driving a powered-down device through its input protection diodes.
- Check the enable and reset polarity and the required pulse width.
- A slow supply ramp can cause a device to enter an undefined state; use a
  supervisor if the datasheet requires it.
- Bus contention during power-up can destroy push-pull drivers.

## Decoupling and grounding

- One 100 nF ceramic per supply pin, placed close to the pin.
- A bulk capacitor (1–100 µF) per rail or per board section.
- Use a ground plane; do not daisy-chain ground returns.
- Keep high-di/dt loops small (switching regulators, clock drivers).
- Separate analog and digital grounds only when the datasheet or the
  converter topology requires it; otherwise use one plane and control the
  current paths.

## ESD and protection

- Add TVS diodes at external connectors, after the series resistor if possible.
- Check the TVS clamp voltage against the absolute maximum of the protected
  pin.
- Use a common-mode choke or ferrite for cables that leave the enclosure.
- Do not rely on the IC's internal ESD diodes for repeated external events.

## Sources

- NXP, *UM10204 I2C-bus specification and user manual*.
- TI, *Logic Guide* and *A Guide to Voltage Translation*.
- Analog Devices, *Data Conversion Handbook*.
- IPC-2221B, *Generic Standard on Printed Board Design*.
