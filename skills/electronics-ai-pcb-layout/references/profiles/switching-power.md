# Switching power profile

Apply when verified topology and operating states identify switching current
paths. Obtain exact switch, driver, sense, feedback/compensation and capacitor
pin roles plus operating-current, voltage, thermal and manufacturing inputs.
Do not infer a converter's topology from SW/LX names alone.

Derive the commutation loop from the difference between switching-state current
paths. ADI's [Layout for Power Designs #1: Hot Loops](https://www.analog.com/en/resources/technical-articles/layout-for-power-designs-1-hot-loops.html)
(January 2018, figure 1) distinguishes the high-di/dt commutation loop from the
two complete operating paths in its buck example. Its integrated-switch example
focuses on the input-capacitor/switch connection. A fixed
input-capacitor/switch/inductor/ground loop is not universal.

Evaluate local input/drive/bootstrap loops, sense Kelvin branching, feedback
isolation, switch-node area, thermal/current paths and control/power returns
using the actual topology. Keep switch-node copper as small as is compatible
with electrical and thermal requirements. Do not split grounds or create star
connections without an applicable circuit/reference architecture.

Use qualified width/via and stackup models for current requirements. Raw routed
length or geometric area is not a verified inductance or EMI result. Require
real pad bindings and complete geometry for the measured claim; otherwise keep
the result estimated or unknown. Protect nearby analog/clock/interface scopes
and combine the isolation overlay when a barrier is present.

## Offline Mains / High-Voltage Layer & Fusing Discipline (IEC 61010-1 / IPC-2152)

For 4+ layer isolated AC/DC supplies, offline flyback converters (e.g., InnoSwitch /
AN-70 / AN-82), and industrial power stages:

- **Outer-layer copper priority (`F.Cu` / `B.Cu`):** Standard 4-layer boards use
  `1 oz` (`35 um`) outer copper with ~2x better convective cooling vs `0.5 oz`
  (`17.5 um`) inner layers. Keep AC mains nets (`AC_L`, `AC_L_FUSED`, `AC_N`,
  `AC_L_RECT`), primary HV power loops (`HV_BUS`, `HV_RECT`, `PRI_RET`), and
  high-`dV/dt` switching nodes (`FLY_DRAIN`, `RCD_CLAMP`, `SW`, `PHASE`) on outer
  layers (`F.Cu`/`B.Cu`). The engine enforces this automatically via `S6a`
  mains-first pre-routing (`route/hv_mains.py`), Specctra DSN outer-layer locks
  (`route/dsn.py`), and Lint Rules 15–16 (`hv_power_on_inner_layer`,
  `switch_node_inner_or_oversized`).
- **Pre-fuse fusing margin (Rule 17 `prefuse_mains_trace_undersized`):** Size
  mains AC tracks (`>= 1.0 mm` on outer copper for `<= 1 A` fused inputs, safely
  capped to SMD pad narrow width such as `0.80 mm` at SMD bridge rectifiers) so
  the physical fuse opens before PCB copper vaporizes.
- **Thin prepreg solid insulation (Rule 18 `hv_vertical_prepreg_overlap`):**
  Standard 4-layer outer prepreg (`~0.21 mm`) is below the `0.40 mm` IEC 61010-1
  Clause 6.7 solid-insulation floor. Never vertically overlap primary HV copper
  with secondary/LV copper across thin prepreg, and never cross high-`dV/dt`
  switch nodes over sensitive feedback or analog nets across `< 0.30 mm` dielectric.
