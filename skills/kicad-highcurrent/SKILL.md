---
name: kicad-highcurrent
description: Hardware engineering rules for high-current power distribution (PDN), ASIC Bitcoin miners, multi-phase synchronous buck converters, domino/chained power topologies, 2oz/3oz/4oz copper sizing, thermal via matrices, and input protection (TVS, eFuse).
---

# KiCad High-Current & Power Electronics Engineering Pack

This pack governs high-power DC distribution, ASIC mining boards (Bitaxe, NerdQAxe, custom ASICs), point-of-load buck converters, and thermal management.

---

## 1. Input Protection & Power Highway Architecture

```
[XT60PW-M 60A Input] ──► [SMCJ15A / SMDJ15A TVS] ──► [TI TPS25982 eFuse] ──► [+12V_PROTECTED] ──► Synchronous Bucks
                                                              ▲
                                                    (IMON Current Telemetry)
```

### Mandatory Input Protection Standards (ASIC Miners & High-Power Boards):
1. **Connector Standard:**
   * For continuous currents $> 5.0\,\text{A}$, upgrade vertical XT30 to **Right-Angle XT60 (`Amass XT60PW-M`, JLCPCB C98732)**.
   * Contact resistance $< 0.5\,\text{m}\Omega$, cuts connector thermal dissipation by $>40\%$, accepts 10–12 AWG silicone wire cleanly, and routes cables horizontally away from fan shrouds.
2. **Transient Voltage Suppressor (TVS) Clamping:**
   * Place an **SMCJ15A** (1500W surge) or **SMDJ15A** (3000W surge) unidirectional TVS diode immediately adjacent to input solder cups.
   * Server power supply cables have $0.5\text{--}1.5\,\mu\text{H}$ of parasitic inductance. Hot-plugging or abrupt load shedding causes inductive kicks ($V = L \cdot di/dt$) exceeding 25V–35V, blowing high-side FETs on buck regulators with 18V absolute maximum ratings.
   * *Reverse Crowbar Action:* In unidirectional TVS diodes, accidental reverse polarity forward-biases the diode ($V_F \approx 0.8\,\text{V}$), clamping the rail and immediately tripping the upstream series breaker.
3. **Electronic Fuse (eFuse) vs. Mechanical/Polyfuses:**
   * **TI TPS25982 eFuse (Standard):** $2.7\,\text{m}\Omega$ insertion loss ($0.27\,\text{W}$ at 10A), sub-microsecond short-circuit disconnect, active inrush soft-start (eliminates XT60 plug-in sparks), programmable 14.5V overvoltage cutoff, and integrated IMON current telemetry feeding the MCU ADC.
   * **Mini Blade Fuse (Passive Fallback):** 15A Automotive Mini Blade Fuse (`Littelfuse 0297015.WXNV`, C151095) with Keystone SMT clips (`C917390`). Insertion resistance $4.6\,\text{m}\Omega$.
   * **PPTC Resettable Polyfuses STRICTLY BANNED on $>5\text{A}$ Continuous Rails:** PPTC internal resistance ($15\text{--}30\,\text{m}\Omega$) dissipates 2W–3W of continuous waste heat at 10A DC and drops $>0.3\,\text{V}$ on the supply bus.

---

## 2. High-Current Copper Sizing (IPC-2152 Standard)

For standard outer copper at permissible temperature rise $\Delta T = 10\,^\circ\text{C}$:

| Continuous Current | 1 oz (35 µm) Min Width | 2 oz (70 µm) Min Width | 3 oz (105 µm) Min Width | Recommended Practical Geometry |
| :---: | :---: | :---: | :---: | :--- |
| **1.0 A** | 0.35 mm | 0.20 mm | 0.15 mm | 0.40 mm track |
| **3.0 A** | 1.20 mm | 0.65 mm | 0.45 mm | 1.30 mm track |
| **5.0 A** | 2.10 mm | 1.15 mm | 0.80 mm | 2.20 mm track (or dual-layer route) |
| **10.0 A** | 5.20 mm | 2.80 mm | 1.90 mm | Solid polygon plane (Top + Bottom parallel) |
| **20.0 A** | 12.5 mm | 6.80 mm | 4.60 mm | Dedicated polygon pour with stitched via arrays |
| **40.0 A+** | Polygon | Polygon | Polygon | Dual 2oz/3oz pours + stitched plane sandwich |

---

## 3. Chained / Domino ASIC Power Distribution

When routing power down an array of identical ASIC chips (e.g. Bitaxe multi-chip, NerdQAxe):
1. **Domino Rail Sizing & IR Drop Budgeting:**
   * Core supply rails ($0.8\text{--}1.2\,\text{V}$) operate at extreme current density. Maximum allowable IR drop across the chip string is **$< 15\,\text{mV}$**.
   * Calculate cumulative resistance: $R_{\text{trace}} = \rho \cdot \frac{L}{W \cdot t_{\text{copper}}}$.
2. **Repeated Unit Pattern Replication:**
   * Design the point-of-load decoupling bank, thermal slug via pattern, and feedback loop for Chip 1, then clone the identical layout pattern across all N downstream chip sites to ensure matched impedance.
3. **Kelvin Differential Voltage Sensing:**
   * Never sense buck regulator feedback at the output inductor. Route a dedicated differential Kelvin pair directly from the core bypass capacitor of the furthest critical ASIC back to the buck controller sense pins ($V_{\text{SENSE+}} / V_{\text{SENSE-}}$), completely isolated from high $di/dt$ switching nodes.

---

## 4. Thermal Via Matrices & Plane Stitching

1. **Thermal Slugs (QFNs, Power Stages, ASICs):**
   * Place a dense via matrix directly inside the exposed thermal ground pad: $0.30\,\text{mm}$ drill / $0.60\,\text{mm}$ pad on a $1.0\text{--}1.2\,\text{mm}$ staggered grid.
   * Thermal conductivity: Each filled/plated through-hole via provides $\approx 35\text{--}45\,^\circ\text{C/W}$ of thermal path down to internal and opposite-side copper pours.
2. **Current-Carrying Via Arrays:**
   * Rule of thumb: **1 standard via ($0.3\,\text{mm}$ drill / $0.7\,\text{mm}$ pad) handles $1.5\text{--}2.0\,\text{A}$ DC continuous.**
   * For a 20A power rail transitioning layers, provide an array of at least $12\text{--}15$ stitched vias arranged in a rectangular grid with $1.5\,\text{mm}$ pitch to minimize resistance ($< 1.0\,\text{m}\Omega$).
3. **Thermal Relief Spokes:**
   * Use 8-spoke thermal relief pads (or solid connections if using industrial selective soldering/wave) on XT60 connector pins to balance hand-soldering ease with high DC current capacity.

---

## 5. Offline Mains / High-Voltage Power & IEC 61010-1 / IPC-2152 Layer Discipline

For 4+ layer isolated AC/DC supplies, offline flyback converters (Power Integrations InnoSwitch3 / AN-70 / AN-82, TI TIDA/PMP), and industrial transducers (IEC/EN 61010-1, IEC 62368-1, IPC-2221B, IPC-2152):

1. **Outer-Layer Routing for Mains AC & Primary HV Power (`F.Cu` / `B.Cu`):**
   * Standard 4-layer stackups use **$1\,\text{oz}$ ($35\,\mu\text{m}$) outer copper** vs **$0.5\,\text{oz}$ ($17.5\,\mu\text{m}$) inner copper** with $\approx 2\times$ better convection cooling in air (IPC-2152).
   * Route mains AC (`AC_L`, `AC_N`, `AC_L_FUSED`, `AC_L_RECT`) and primary DC power loops (`HV_BUS`, `HV_RECT`, `PRI_RET`) on **outer layers (`F.Cu` and `B.Cu`)**, keeping inner layers clear except for low-current HV voltage-sense dividers (`V_SENSE`, `FLY_V`) or documented congestion fallbacks.
2. **Pre-Fuse Fusing Margin (IEC 61010-1 / UL 61010-1 / IEC 61000-4-5):**
   * Pre-fuse and mains AC traces MUST have a higher $I^2 t$ fusing rating than the physical fuse ($F_1$) so the fuse always opens before PCB copper vaporizes.
   * Use **$\ge 1.0\,\text{mm}$ outer copper** for low-power ($\le 1\,\text{A}$ fused) mains AC paths (`AC_L`, `AC_L_FUSED`, `AC_N`, `AC_L_RECT`), scaling up with fuse current rating per IPC-2152/Onderdonk.
3. **High-$dV/dt$ Switch-Node EMI Control (`FLY_DRAIN`, `SW`, `RCD_CLAMP`, `PHASE`):**
   * Keep switching nodes **strictly on outer layers** (preferring component-side `F.Cu`) and physically minimal in length/area ($\le 25\text{--}30\,\text{mm}$ on offline flyback; $\le 15\text{--}20\,\text{mm}$ on LV buck).
   * Never bury long switch-node runs on inner layers (`In2.Cu`) or cross them vertically over sensitive feedback/analog traces (`FLY_FB`, `FB`, `ADC_*`) across thin prepreg ($< 0.30\,\text{mm}$).
4. **Solid Insulation vs. Thin Prepreg (IEC 61010-1 Clause 6.7 / IEC 62368-1):**
   * Standard 4-layer outer prepreg is only $\approx 0.21\,\text{mm}$ — below the $0.40\,\text{mm}$ reinforced solid-insulation threshold. Never vertically overlap primary HV copper (`F.Cu`/`In2.Cu`) with secondary/LV copper (`In1.Cu`/`B.Cu`) across thin prepreg; enforce an all-layer isolation moat plus milled slots where needed.
