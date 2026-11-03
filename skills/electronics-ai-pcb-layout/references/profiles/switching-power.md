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
