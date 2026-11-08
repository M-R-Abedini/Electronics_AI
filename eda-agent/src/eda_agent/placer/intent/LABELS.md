# Part roles in design_intent

Definitions used by the inference engine, the dev labels and the client model. A role says what a part does for placement. The target says which part it serves.

| Role | Definition | Target |
|---|---|---|
| `ic` | Active multi-pin part: IC or module. | — |
| `converter_ic` | Switching regulator or PMIC: it has a switch-node pin that drives an inductor. | — |
| `linear_regulator` | Linear regulator (78xx, LDO). | — |
| `power_switch` | Load switch, ideal diode or eFuse. | — |
| `converter_inductor` | Power inductor on a converter's switch node. | the converter |
| `converter_diode` | Catch or boost diode on a converter's switch node. | the converter |
| `converter_control` | Feedback divider, compensation, soft-start or set resistor of a converter. | the converter |
| `regulator_input_cap` | Capacitor on a regulator's or power switch's input pin. | the regulator |
| `regulator_output_cap` | Capacitor on a regulator's output pin, including the internal regulator output of an IC (for example `3V3_OUT`). | the regulator or IC |
| `decap` | Capacitor from a supply rail to ground that serves one part's supply pin. On the dev labels, the target is the part whose pin the expert placed it at. | the IC or connector |
| `bulk_cap` | Rail capacitor that serves no single pin: large, polarized, or one of many. | — (net) |
| `pin_cap` | Capacitor on one IC's non-supply pin (reference, timing, slew, filter), with its other end on a rail or ground. | the IC |
| `coupling_cap` | Capacitor in series between two signal nets. | — |
| `crystal` | Quartz crystal or resonator. | the IC it clocks |
| `oscillator` | Active oscillator. | — |
| `crystal_load_cap` | Capacitor from a crystal net to ground. | the crystal |
| `crystal_aux` | Feedback or series resistor on a crystal net. | the crystal |
| `esd` | TVS diode or ESD array that protects connector lines or a supply input. | the connector |
| `connector` | Connector, socket, module socket or edge connector. | — |
| `series_resistor` | Resistor in series between two signal nets, including a base resistor and a back-termination toward a jack. | — |
| `pull_up` / `pull_down` | Resistor from a signal net that reaches an IC, connector or transistor base or grid, to a supply rail or to ground. Resistor arrays with a common pin on a rail count. | — |
| `termination` | Low-value resistor (≤ 150 Ω) from a connector line to ground, or across a pair. | — |
| `bias_resistor` | Resistor on an IC's reference or set pin (REXT, RSET, IREF, CREF), or between two pins of one IC. | the IC |
| `divider_resistor` | One of two resistors that form a divider tap read by an IC or a capacitor. | — |
| `led` | LED. | — |
| `led_resistor` | Current-limit resistor of an LED. | the LED |
| `rail_filter` | Inductor, ferrite or resistor between two supply rails. | — |
| `signal_passive` | Any other passive in an analog or signal network: filters, emitter or cathode resistors, timing parts, bleeders. | — |
| `transistor` | Discrete active device: BJT, FET or valve. | — |
| `diode` | Discrete diode that is not ESD and not a converter diode: rectifier, clamp or signal diode. | — |
| `switch`, `jumper`, `test_point`, `mechanical` | As named. Mechanical covers holes, logos and fiducials. A single-pin header is a test point. | — |

**Critical roles (plan F2 gate ≥ 95%):** `decap` (role and target), `crystal`, `crystal_load_cap`, `converter_ic`, `converter_inductor`, `converter_diode`, `converter_control`, and `esd`. `regulator_input_cap` and `regulator_output_cap` also count as critical when their target is a `converter_ic`. Differential pairs are scored separately.

**Uncertain:** a claim with confidence below 0.6, or a target given only as candidates. Uncertain claims are reported separately and become questions for the client model or the user. They become hard constraints only once the user confirms them.
