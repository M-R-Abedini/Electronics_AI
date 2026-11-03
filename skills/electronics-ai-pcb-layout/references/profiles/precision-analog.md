# Precision analog profile

Apply to verified sensitive input, reference, feedback, sense or excitation
functions. A precision ADC, high-impedance input or low-level differential path
may select different obligations; a part name or designator alone does not.

Bind sensor/protection/filter/amplifier/converter paths to actual pads. Establish
source impedance, bandwidth/settling and error budgets, reference/current
returns, leakage sensitivity and thermal conditions from the selected circuit.
Keep paired input topology and relevant junction/thermal symmetry in view.
Do not trade reference/PGA/compensation access for a cosmetically uniform decap
row. A short center distance with the wrong return remains unresolved.

Guarding requires its intended potential, low-impedance drive and validated
geometry; it is not automatically a ground ring. ADI's
[Layout for Precision Op Amps](https://www.analog.com/en/resources/technical-articles/layout-for-precision-op-amps.html)
(September 2013, Thermal/Leakage Effects) discusses thermal junction errors and
guards near the input potential. Apply the exact amplifier and board process
requirements rather than copying that example as a universal construction.

Counterexample for rule selection, not a placement template: TI
[ADS1262/ADS1263 SBAS661C, section 12.2, page 125](https://www.ti.com/lit/ds/symlink/ads1262.pdf#page=125)
(May 2021) places analog bypass capacitors on the opposite side in its example
to reserve access for reference/PGA capacitors. This disproves an unconditional
same-layer-decap rule; it does not prescribe bottom-side decaps for other parts.
Fetch the exact current part/package source for the design being worked on.

Compose switching/digital and isolation profiles when those domains coexist.
Classify unknown analog roles as hypotheses and retain missing return, leakage,
thermal or model evidence rather than certifying precision from placement.
