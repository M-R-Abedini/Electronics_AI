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

### 4-Layer Boards (Two Proven Stackups — Always Present Trade-Offs Honestly)
| Stackup | Layer Allocation | Electromagnetic Advantage | Routing Trade-Off |
|---|---|---|---|
| **Option A (Hartley Dual-GND)** | `L1: SIG+PWR` / `L2: Solid GND` / `L3: Solid GND` / `L4: SIG+PWR` | Every `L1 <-> L4` signal transition stays referenced to **GND <-> GND**: a single adjacent GND return via (`<= 1.5 mm`) closes the return path cleanly at all frequencies. Excellent EMI shielding. | Power rails share `L1`/`L4` routing space with signals (requires disciplined power floorplanning and short local power pours). |
| **Option B (Classic Split Reference)** | `L1: SIG` / `L2: Solid GND` / `L3: Solid PWR` / `L4: SIG` | Dedicated low-impedance internal power plane frees outer layers for dense signal routing (common on dense 4-layer MCU/bus boards). | `L1 <-> L4` transitions change reference from **GND to PWR**: high-frequency return current must jump across the thick core via a **stitching capacitor (`<= 3.0 mm`)** connecting `GND` and `PWR`. Fast signals should stay on `L1` (referenced to `L2 GND`) whenever possible. |

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

---

## 4. Impedance & Differential Pair Rules (Architecture §7.1 & §8.4)

- Differential pair copper (`S6a`) must satisfy: matched pair width/gap from board netclass (or verified stackup impedance calculation), equal via counts on `+` and `-`, skew `<= 0.15 mm`, and continuous reference plane.
- Never assume default KiCad `0.2 mm / 0.25 mm` rules represent 90-ohm USB or 100-ohm Ethernet/MIPI impedance—check `board.stackup` dielectric thicknesses and `epsilon_r`. If stackup dielectric data is missing, flag impedance as **unverified / unknown** (P5) and ask the user for fab stackup parameters before claiming controlled impedance.
