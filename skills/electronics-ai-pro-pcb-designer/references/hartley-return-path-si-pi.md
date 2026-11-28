# Rick Hartley & Eric Bogatin Return-Path, Stackup & SI/PI Physics

This reference encodes the electromagnetic return-path principles of Rick Hartley and Eric Bogatin and maps each physical law directly to Electronics AI 2.0's stackup policy, `S6a` critical routing, and `F6` electrical lint (`eval/lint.py`).

---

## 1. The Core Law of High-Speed PCB Design

> **Signals do not travel solely in copper traces—electromagnetic energy travels in the dielectric space between the signal trace and its reference plane.**

- At **DC and very low frequencies**, return current follows the path of **least resistance** (straight line between endpoints).
- At **digital edge rates (even slow 1–5 ns MCU/USB/SPI edges, regardless of clock frequency!)**, return current follows the path of **least inductance**—concentrated tightly in the **adjacent reference plane directly underneath the signal trace**.
- **Consequence:** Whenever a trace crosses a gap, slot, split, or via-antipad perforation in its adjacent reference plane, the return current must detour around the void. The resulting loop area spikes dramatically, increasing:
  - Trace characteristic impedance discontinuity (reflections / ringing),
  - Common-mode radiated EMI (slot antenna effect),
  - Mutual inductive crosstalk into neighboring signals sharing that detour.

---

## 2. Stackup Engineering (`stackup_policy`: `locked` vs `needs_decision` vs `propose`)

Never silently change a board's layer count or invent planes. When `placer__inspect` reports `stackup_policy == "needs_decision"`, present these physics-based choices to the user:

### 2-Layer Boards
- **Preferred topology:** `F.Cu = Signals + Power` / `B.Cu = Maximum-Continuity GND Pour`.
- **Physics:** Because the core dielectric is thick (`~1.5–1.6 mm`), keeping `B.Cu` as unbroken ground as possible minimizes return loop area. Route short jumpers/crossings on `B.Cu` cleanly rather than slicing `B.Cu` into long disconnected ground islands.
- **Measured by F6 Lint (`two_layer_gnd_continuity` & `two_layer_return_gap`):**
  - `gnd_fill_fraction`, `largest_island_fraction` (target `>= 0.85`, warns `< 0.75`), and `island_count` (pieces `>= 1.0 mm^2`).
  - Narrow perpendicular crossings (`~0.4–0.6 mm`) are physically unavoidable on 2-layer boards; wider return gaps (`> max(1.0 mm, 4 * width)`) on fast nets are flagged as `two_layer_return_gap` warnings.

### 4-Layer Boards (Three Proven Stackups — Choose From Real Geometry, Never Hardcode One)
| Stackup | Layer Allocation | Electromagnetic Advantage | Routing Trade-Off |
|---|---|---|---|
| **Option A (Hartley Dual-GND)** | `L1: SIG+PWR` / `L2: Solid GND` / `L3: Solid GND` / `L4: SIG+PWR` | Every `L1 <-> L4` signal transition stays referenced to **GND <-> GND**: a single adjacent GND return via (`<= 1.5 mm`) closes the return path cleanly at all frequencies. Excellent EMI shielding. | Power rails share `L1`/`L4` routing space with signals (requires disciplined power floorplanning and short local power pours). |
| **Option B (Classic Split Reference)** | `L1: SIG` / `L2: Solid GND` / `L3: Solid PWR` / `L4: SIG` | Dedicated low-impedance internal power plane frees outer layers for dense signal routing (common on dense 4-layer MCU/bus boards). | `L1 <-> L4` transitions change reference from **GND to PWR**: high-frequency return current must jump across the thick core via a **stitching capacitor (`<= 3.0 mm`)** connecting `GND` and `PWR`. Fast signals should stay on `L1` (referenced to `L2 GND`) whenever possible. |
| **Option C (4L HDI BGA Escape + Solid GND Shield)** | `L1: SIG+PWR` / `L2: Signal (L1-L2 Microvia Escape)` / `L3: Solid GND` / `L4: SIG+PWR` | Keeps `L2 (In1.Cu)` open for `L1-L2` microvia-in-pad fanout of `<= 0.65 mm` WLCSP/BGAs on `F.Cu`, while `L3 (In2.Cu)` provides an unbroken Solid `GND` reference plane & shield between `L1/L2` digital signals and `L4` analog/optical/sensor circuits. `S6a` taps connect SMD `GND` pads vertically to `L3`, eliminating messy point-to-point `GND` traces. | `L1` signals are slightly farther from `L3 GND` than from `L2`; keep high-speed/sensitive traces tight, minimize `L2` routing directly under noisy switching nodes, and stitch `GND` vias near layer transitions. |

### GND Routing Strategy: Dedicated Plane + `S6a` Taps + Final Refill (Never Point-to-Point `GND` Traces on `>= 4L`)
- **Do NOT** route `GND` as long point-to-point traces on `>= 4L` boards—it clogs routing channels and degrades return-path inductance.
- **Do NOT** route signals across all layers first and then pour `GND` only into leftover scraps—that creates slotted/fragmented reference planes (`reference_split_crossing`).
- **DO** define the dedicated `Solid GND` plane zone **before** calling `placer__place_board(..., route=true)` (using `stackup.guidance` from `placer__inspect`), let `S6a` drop short vertical `GND` tap vias from SMD pads to the plane, let `Freerouting` route only non-plane signals/power cleanly, and let post-route `ZONE_FILLER` refill the plane and close all `GND` connections with zero clutter.

### 6+ Layer Boards
- Pair every signal layer closely with at least one unbroken solid `GND` plane across a thin prepreg (`<= 0.1–0.2 mm`).
- Export pure inner planes as DSN `(type power)` so the autorouter never routes foreign signals across a solid reference plane.

---

## 3. Mapping Every Hartley/Fedvel Rule to `eval/lint.py` (Architecture §8.3)

| Lint Rule (`eval/lint.py`) | Physical Mechanism | Declared Threshold & Severity | Corrective Action |
|---|---|---|---|
| **`decap_loop`** | Inductance is proportional to loop perimeter (`IC supply pin -> decap -> GND plane -> IC GND pin`). A via between an IC pin and its same-side decap adds via inductance in series *before* the capacitor can filter HF current. | Same-side via between IC & decap -> `warning`. Path `> 6.0 mm` -> `warning`; `> 12.0 mm` -> `critical`. (Rails with dedicated inner planes connect vertically via vias.) | Move decap closer to the exact target supply pin (`placer__refine` or verify `design_intent.json` target); route `IC pin -> decap pad -> via` in `S6a`. |
| **`reference_split_crossing`** | Fast/critical signal (`DIFF_PAIR`, `HIGH_SPEED`, `SW_NODE`, `SENSITIVE_ANALOG`, `crystal`) crossing an interior void/split in its adjacent dedicated plane (`>= 4` layers). | Interior uncovered span `> max(0.35 mm, 2.5 * width)` -> **`critical`**. | Re-route signal onto a layer with continuous GND reference, move the offending plane split, or re-place the crossing block. |
| **`plane_perforation`** (Plan §4.2 F5) | Dense rows of through-vias under a multi-pin IC (`>= 16` pads, excluding edge connectors) merge their antipads ("Swiss cheese" effect), severing return paths right where di/dt is highest. | Adjacent reference plane `> 40%` perforated inside IC courtyard -> `warning`. | Stagger escape vias, reduce via antipad/diameter within fab rules, or spread breakout fanout channels. |
| **`return_via`** | Signal via transitioning between two layers that share the same reference plane net (e.g., `GND <-> GND`). | Nearest return via on that reference net `> 1.5 mm` for fast/critical nets -> `warning`. | Place a GND stitching via within `1.0–1.5 mm` of every high-speed signal transition via (and symmetric pair of GND vias beside differential-pair transitions). |
| **`reference_change_stitch_cap`** | Signal via transitioning between layers referenced to **different** plane nets (e.g., `L1/GND` <-> `L4/+3V3`). A simple via cannot short `GND` to `+3V3`! | No capacitor bridging those two reference nets within `3.0 mm` for fast/critical nets -> `warning`. | Keep the fast net on the GND-referenced layer, or place a small HF stitching capacitor (`10–100 nF`) bridging `GND` and `PWR` within `3.0 mm` of the transition via. |
| **`signal_under_crystal`** | Unrelated traces passing beneath a quartz crystal body on unshielded layers couple noise directly into the high-impedance oscillator tank and radiate clock harmonics. | Unrelated signal track intersecting crystal inner body on unshielded layer -> **`critical`** (DC power rail -> `warning`). | Keep all unrelated signals outside the crystal keepout; shield with a continuous GND plane on the next inner layer when `>= 4` layers. |
| **`fb_near_sw` & `fb_under_inductor`** | Switching node (`SW_NODE`) swings full input voltage in nanoseconds (high dV/dt); inductor core carries intense pulsating magnetic flux (high dB/dt). Coupling into high-impedance `FB` causes oscillation and output ripple. | `FB` to `SW` copper `< 1.0 mm` -> **`critical`** (`< 2.0 mm` -> `warning`); `FB` passing beneath `converter_inductor` -> **`critical`**. | Place feedback divider tight against IC `FB` pin on the quiet side of the converter, routed away from `SW` and strictly outside the inductor footprint. |
| **`buck_hot_loop`** | Pulsating current in a buck converter flows strictly in the `Cin -> HS FET -> LS FET/Diode -> Cin GND` loop (not through the output inductor!). Loop area directly governs ringing and radiated EMI. | Convex-hull loop area `> 120 mm^2` -> `warning`; `> 250 mm^2` -> **`critical`**. | Compact `regulator_input_cap` directly between `converter_ic` `VIN` and `PGND` pins on the same side with tight return copper. |
| **`esd_to_connector`** | Inductance of the trace between connector and ESD clamp (`~0.8–1 nH/mm`) adds `L * di/dt` overshoot voltage directly onto the protected IC pin during an 8 kV IEC 61000-4-2 ESD strike. | Connector-to-ESD copper path `> 5.0 mm` -> `warning` (`> 10.0 mm` high warning). | Place `esd` immediately adjacent to the connector pins so the surge hits the ESD diode *before* reaching any IC trace. |

### Extended EMC, HV Safety & Precision Instrumentation Rules (`eval/emc_safety.py`, Rules 10–20)

| Lint Rule (`eval/emc_safety.py`) | Physical Mechanism & Standard | Declared Threshold & Severity | Corrective Action |
|---|---|---|---|
| **`mounting_hole_copper_clearance`** (Rule 10) | Screw heads/standoffs bridge nearby copper; HV zones require reinforced standoff clearance (IEC 61010-1). | LV `< 0.50 mm` crit / `< 1.20 mm` warn; HV `< 4.00 mm` crit / `< 6.00 mm` warn. | Expand NPTH keepouts (`>= 6.0 mm` in HV zones) and re-route/re-fill zones. |
| **`pour_under_unfused_mains_emi`** (Rule 11) | Copper pours beneath unfused AC mains / CM-choke / MOV inputs couple surge & CM noise across the EMI filter. | Pour overlap `>= 2.0 mm^2` under unfused mains filter -> `warning`. | Add all-layer pour keepout around AC input, fuse, MOV, X-cap, and CM choke. |
| **`hv_pad_creepage`** (Rule 12) | Surface flashover between different-net HV pads (IEC 61010-1 / IPC-2221B). | Edge-to-edge `< 0.90 mm` crit / `< 1.20 mm` warn. | Separate HV footprints or mill isolation slot per creepage class. |
| **`tvs_ground_return_inductance`** (Rule 13) | Single long ground trace on surge TVS raises clamping overshoot during IEC 61000-4-5 surges. | Large TVS GND pad without close parallel/low-Z GND vias within `3.5 mm` -> `warning`. | Let `S6a` drop dual parallel GND vias directly at the TVS cathode/GND pad. |
| **`thermal_pad_via_wicking`** (Rule 14) | Large open through-vias (`>= 0.35 mm` drill) inside exposed thermal pads wick solder paste away during reflow. | Uncapped large thermal via drill `>= 0.35 mm` on thermal pads `>= 6.0 mm^2` -> `warning`. | Use `0.25–0.30 mm` drill thermal vias or tent/IPC-4761 Type VII fill. |
| **`hv_power_on_inner_layer`** (Rule 15) | Inner layers are `0.5 oz` (`17.5 um`) with ~2x worse cooling than `1 oz` outer layers (IPC-2152 / AN-70). | Mains `> 35%` inner -> `critical`; HV power `> 25%` inner -> `warning`. | Keep HV/mains power on `F.Cu`/`B.Cu` (`route/hv_mains.py` + `route/dsn.py` outer-layer lock). |
| **`switch_node_inner_or_oversized`** (Rule 16) | High-`dV/dt` switch nodes (`FLY_DRAIN`, `SW`, `RCD_CLAMP`) radiate E-field proportional to area and couple capacitively on inner layers. | Inner copper `> 2.0 mm` or excessive MST detour (`> 1.65x`) / length -> `warning`/`critical`. | Route switch nodes strictly on `F.Cu`/`B.Cu` and compact the primary switching loop. |
| **`prefuse_mains_trace_undersized`** (Rule 17) | Pre-fuse / mains AC copper must survive higher `I^2*t` than the physical fuse (IEC 61010-1 / UL 61010-1). | `< 0.45 mm` -> `critical`; `< 0.80 mm` -> `warning` (target `>= 1.0 mm` THT / `0.80 mm` SMD pad cap). | Enforce mains width floor in `dsn.py` and `hv_mains.py` pre-routing. |
| **`hv_vertical_prepreg_overlap`** (Rule 18) | Standard 4L prepreg (`~0.21 mm`) is below IEC 61010-1 Clause 6.7 `0.40 mm` reinforced solid insulation. | HV over secondary `< 0.40 mm` -> `critical`; HV switch over FB/sense `< 0.30 mm` -> `warning`. | Maintain an all-layer primary/secondary isolation moat and keep FB clear of switch-node projections. |
| **`analog_thermal_proximity`** (Rule 19) | Thermal gradients across precision analog solder joints induce `3–40 uV/°C` Seebeck thermal-EMF drift (Precision Instrumentation). | Precision analog IC (`ADC`/`DAC`/`VREF`/`INA`/`OPAMP`) `< 4.0 mm` from hot power part -> `critical` (`< 8.0 mm` -> `warning`). | Let `problem.py` `10.0 mm` thermal repulsion separate hot power stages from precision metrology ICs. |
| **`afe_digital_crosstalk`** (Rule 20) | Capacitive/inductive coupling from fast digital clocks/buses into high-Z sensor/reference lines degrades SNR and ENOB. | Parallel run `> 3.0 mm` at `< 0.50 mm` gap on same layer between AFE (`AIN`/`RTD`/`PT100`/`VREF`/`BRIDGE`) and digital clocks/SPI/UART/PWM -> `warning`. | Route AFE lines on quiet zones with `>= 0.50 mm` spacing or ground-plane shielding. |

---

## 4. Impedance & Differential Pair Rules (Architecture §7.1 & §8.4)

- Differential pair copper (`S6a`) must satisfy: matched pair width/gap from board netclass (or verified stackup impedance calculation), equal via counts on `+` and `-`, skew `<= 0.15 mm`, and continuous reference plane.
- Never assume default KiCad `0.2 mm / 0.25 mm` rules represent 90-ohm USB or 100-ohm Ethernet/MIPI impedance—check `board.stackup` dielectric thicknesses and `epsilon_r`. If stackup dielectric data is missing, flag impedance as **unverified / unknown** (P5) and ask the user for fab stackup parameters before claiming controlled impedance.
