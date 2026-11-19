# Feranec (FEDEVEL) PCB Floorplanning & Placement Methodology

This reference translates Robert Feranec's proven industrial PCB layout workflow into concrete `design_intent.json` decisions, block constraints, and visual inspection criteria for Electronics AI 2.0.

---

## 1. The Order of Design Decisions (Never Start by Scattering Passives)

A professional layout is solved **outside-in and critical-first**:

1. **Mechanical & Enclosure Reality First (`mechanical` constraints):**
   - Mounting holes, board cutouts, keepouts, card-edge fingers, antennas, displays, buttons, LEDs aligned to light pipes, and external I/O connectors (USB, Ethernet, power jack, headers) define the immutable skeleton of the board.
   - In re-layouts, preserve existing external connector poses (`{"mode": "preserve"}`).
   - In cold-start designs, constrain edge-bound parts to explicit board `rule_areas` and permitted orientations (`{"mode": "region", "region": "<rule_area_name>", "angles_deg": [0, 90, ...]}`).
   - Always check **3D overhang, height clearance, and cable/tool access** (e.g., lying electrolytic capacitors near USB tabs, tall inductors near enclosures, heatsink keepouts). If height/3D data is absent, record it as `unknown` (P5).

2. **Macro Signal-Flow Floorplanning (`blocks` & hierarchical sheets):**
   - Group components by **electrical function and hierarchical schematic sheet** (MCU/SoC, PMIC/buck converters, USB/Ethernet PHY, RF/antenna, analog front-end, memory, connectors/ESD).
   - Trace the primary energy and data flow across the board:
     - **Power flow:** Input connector -> input protection/TVS -> bulk/EMI filter -> switching regulator / PMIC -> LDOs -> load ICs.
     - **High-speed data flow:** External connector -> ESD/TVS -> series termination/common-mode choke -> PHY/SoC pins -> memory/peripherals, minimizing bus crossings (`ratsnest` crossings) and backtracking.
   - **Domain separation:** Keep high-di/dt switching converters (`SW_NODE`, `POWER_HIGH_CURRENT`), digital clocks/buses (`HIGH_SPEED`, `DIFF_PAIR`), RF sections, and sensitive low-level analog (`SENSITIVE_ANALOG`) in distinct physical zones rather than interleaving them.
   - **IC orientation by pin affinity:** Orient multi-pin ICs (QFN, QFP, BGA, SOIC) so major buses and high-speed pairs face their destination connectors/ICs directly—avoiding 180-degree wrap-arounds that inflate crossings and congestion.

3. **Critical Local Loops (Highest Electrical Priority — Must Sit Tight Against Target Pins):**

   | Functional Block | Physical Priority & Intent Mapping | What Visual & Metric Review Must Confirm |
   |---|---|---|
   | **Switching Regulator (Buck / Boost / PMIC)** | Label `converter_ic`, `converter_inductor`, `converter_diode`, `regulator_input_cap`, `regulator_output_cap`, `converter_control`; classify `SW_NODE` and `POWER_HIGH_CURRENT`. | Minimal **hot loop area** (`Cin` -> high-side switch -> low-side switch/diode -> `Cin GND`, target `<= 80–120 mm^2`). Short, wide `SW` copper polygon/pour (never long thin tracks). Feedback divider (`converter_control`) placed right at the IC's `FB` pin, routed away from `SW` (`>= 2.0 mm`) and **never beneath the inductor**. |
   | **High-Frequency Decoupling (`decap`)** | Resolve each `decap` to its exact `{ref, net, pins}` target IC supply pin (from schematic local branch or datasheet pin table). Distinguish shared rail bulk caps (`bulk_cap`, `scope: "rail"`). | Median decap-to-supply-pin distance `<= 1.5–2.0 mm`; tight p90 tail; electrical path order **`IC pin -> decap pad -> via to plane`** (no via sitting between a same-side decap and its IC pin). |
   | **Crystal / Oscillator (`crystal`, `crystal_load_cap`, `crystal_aux`)** | Bind `crystal` -> target IC, and `crystal_load_cap`/`crystal_aux` -> `crystal`. | Tight, symmetric loop right at IC `XTAL_IN`/`XTAL_OUT` pins; **zero unrelated signal tracks passing inside the crystal body keepout** on unshielded layers; local GND guard/reference under the oscillator. |
   | **ESD / TVS Protection (`esd`)** | Bind `esd` -> target `connector` on the protected signal nets. | Placed immediately behind the connector (`<= 5.0 mm` copper path) **in-line** (`connector -> ESD -> IC`), never on a long stub off to the side. |
   | **Differential Pairs (`diff_pairs`)** | Confirm `positive`/`negative`, `kind` (`usb`, `ethernet`, `mipi`, `lvds`, `analog_audio`), and `routing_eligible: true` only when netclass rules match the interface impedance. | Series resistors / AC-coupling caps (`series_resistor`, `coupling_cap`) placed symmetrically as matched pairs with tight spacing along the natural pair corridor; unobstructed escape from IC pins to connector. |

4. **Fanout & Escape Budget (Plan Before Packing Tightly):**
   - Never pack passives flush against fine-pitch QFN/QFP/BGA pin rows where signal and power escapes need vias.
   - Leave escape channels for active-IC power/GND vias and differential pair breakouts (`floorplan=true` enables selective escape reservations and `PathFinder` congestion + guarded Cell Inflation automatically).

5. **Assembly Side Policy (Level 1 / 2 / 3):**
   - **Level 1 (`top_only`):** Default for new designs (lowest SMT assembly cost—single reflow pass). Report clearly if board density (`capacity_warnings`) or fine-pitch BGA/QFN decoupling justifies Level 2.
   - **Level 2 (active top, passives/decaps bottom):** Ideal for dense BGA/QFN designs where decoupling capacitors sit directly under power balls/pins on the bottom side.
   - **Level 3 (full two-sided):** Used when mechanical/form-factor constraints require ICs on both sides. Always verify zero through-hole-to-opposite-pad collisions and no tall parts obstructing opposite-side thermal vias.

6. **DFA Regularity, Banking & Silkscreen Craftsmanship:**
   - Group repeated passives (pull-up rows, LED + current-limit resistors, series termination banks) into aligned rows/columns on consistent 90-degree orientations with uniform polarity direction (all diode cathodes / polarized cap `+` marks aligned along one axis where practical).
   - Keep reference designators (`F.Silkscreen`/`B.Silkscreen`) readable (`0°` or `90°`), strictly inside board outline, off exposed copper pads (`0 silk_over_copper`), and clear of silkscreen outlines (`0 silk_overlap`).
   - Keep movable pads clear of free-standing solder-mask openings (`mask_aperture` logos/windows) to guarantee `0 solder_mask_bridge`.

---

## 2. Visual Gallery Inspection Checklist (Feranec Eye-Test)

When inspecting `placer__gallery(..., include_images=true)` top and bottom renders:
1. **Macro Silhouette:** Are external connectors on the correct edges with unobstructed cable entry? Are functional blocks visibly cohesive rather than exploded across the board?
2. **Ratsnest Topology:** Are buses flowing cleanly in parallel bundles rather than forming tangled "rat nests" across the center of the board?
3. **Decap & Crystal Tightness:** Are decoupling caps clustered directly around their ICs' supply pins? Is every crystal hugged tightly by its load caps right beside its MCU/PHY?
4. **Power Stages:** Do switching converters form compact triangles (`Cin` - `IC` - `L` - `Cout`) away from analog/RF inputs?
5. **Thermal & Mechanical Hazards:** Any high-power regulators stacked back-to-back? Any overhanging electrolytics or connectors colliding with board notches or mounting-hole screw heads?
6. **Silkscreen & Assembly Cleanliness:** Are passives neatly banked, uniformly oriented, and free of unreadable overlapping text?
