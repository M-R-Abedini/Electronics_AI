#!/usr/bin/env python3
"""Circuit calculations for the circuit-design skill.

Every number the skill reports is produced here, in Python, from an explicit
formula.  The module is deliberately dependency-free (standard library only)
so it runs on Termux and in minimal CI containers.

Design rules
------------
* Inputs are validated; impossible combinations return ``ok: false`` instead
  of a plausible-looking number.
* Every result carries the formula used and the assumptions behind it.
* Semiconductor results from generic models are flagged as estimates.  They
  are not a substitute for a datasheet or a vendor SPICE model.
* SPICE suffix rules apply (``m`` = milli, ``meg`` = mega).

Run ``python3 circuit_calc.py list`` for the command list and
``python3 circuit_calc.py <command> --help`` for parameters.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from circuit_common import (  # noqa: E402
    ValueParseError,
    divider_ratio,
    emit,
    eseries_values,
    fail,
    nearest_eseries,
    parallel,
    parse_value,
    result,
    si_number,
    spice_number,
)

# Physical constants
K_BOLTZMANN = 1.380649e-23       # J/K
Q_ELECTRON = 1.602176634e-19     # C
T_NOMINAL = 300.0                # K (27 C), used for VT unless overridden
VT_300K = K_BOLTZMANN * T_NOMINAL / Q_ELECTRON  # ~25.85 mV
OZ_TO_MIL = 1.378                # 1 oz copper = 35 um = 1.378 mil


class CalcError(ValueError):
    """A user-facing calculation error."""


def _need(p: dict[str, Any], *names: str) -> None:
    missing = [n for n in names if p.get(n) is None]
    if missing:
        raise CalcError("missing required parameter(s): " + ", ".join(missing))


def _pos(value: float, name: str) -> float:
    if value is None or not math.isfinite(value) or value <= 0:
        raise CalcError(f"{name} must be a positive finite number")
    return float(value)


def _nonneg(value: float, name: str) -> float:
    if value is None or not math.isfinite(value) or value < 0:
        raise CalcError(f"{name} must be a non-negative finite number")
    return float(value)


def _series_resistance(r1: float, r2: float) -> float:
    return r1 + r2


# ---------------------------------------------------------------------------
# Basic electrical laws
# ---------------------------------------------------------------------------

def calc_ohms(p: dict[str, Any]) -> dict[str, Any]:
    known = {k: p.get(k) for k in ("v", "i", "r", "p") if p.get(k) is not None}
    if len(known) < 2:
        raise CalcError("provide any two of v, i, r, p")
    v, i, r, pw = (known.get(k) for k in ("v", "i", "r", "p"))
    if v is not None and i is not None:
        r = v / i if i != 0 else math.inf
        pw = v * i
    elif v is not None and r is not None:
        i = v / r
        pw = v * i
    elif i is not None and r is not None:
        v = i * r
        pw = v * i
    elif pw is not None and r is not None:
        v = math.sqrt(pw * r)
        i = v / r
    elif pw is not None and v is not None:
        i = pw / v
        r = v / i
    elif pw is not None and i is not None:
        v = pw / i
        r = v / i
    return result(
        inputs=known,
        results={
            "voltage_V": v,
            "current_A": i,
            "resistance_ohm": r,
            "power_W": pw,
        },
        formulas=["V = I*R", "P = V*I = I^2*R = V^2/R"],
        assumptions=["ideal linear element, no temperature coefficient"],
    )


def calc_divider(p: dict[str, Any]) -> dict[str, Any]:
    vin = _pos(p.get("vin"), "vin")
    r1 = p.get("r1")
    r2 = p.get("r2")
    vout = p.get("vout")

    if r1 is None and r2 is not None and vout is not None:
        if not 0 < vout < vin:
            raise CalcError("vout must be between 0 and vin")
        r1 = r2 * (vin - vout) / vout
    elif r2 is None and r1 is not None and vout is not None:
        if not 0 < vout < vin:
            raise CalcError("vout must be between 0 and vin")
        r2 = r1 * vout / (vin - vout)
    _need({"r1": r1, "r2": r2}, "r1", "r2")
    r1 = _pos(r1, "r1")
    r2 = _pos(r2, "r2")

    vout = vin * divider_ratio(r1, r2)
    current = vin / (r1 + r2)
    rout = parallel(r1, r2)
    p_r1 = current ** 2 * r1
    p_r2 = current ** 2 * r2
    out: dict[str, Any] = {
        "vout_V": vout,
        "divider_current_A": current,
        "thevenin_resistance_ohm": rout,
        "p_r1_W": p_r1,
        "p_r2_W": p_r2,
        "p_total_W": p_r1 + p_r2,
        "ratio": vout / vin,
    }
    rload = p.get("rload")
    if rload is not None:
        rload = _pos(rload, "rload")
        rp = parallel(r2, rload)
        vloaded = vin * rp / (r1 + rp)
        out["vout_loaded_V"] = vloaded
        out["loading_error_percent"] = (vloaded - vout) / vout * 100.0
        out["load_current_A"] = vloaded / rload
    return result(
        inputs={"vin": vin, "r1": r1, "r2": r2, "rload": p.get("rload")},
        results=out,
        formulas=[
            "Vout = Vin * R2/(R1+R2)",
            "Rout = R1 || R2",
            "I = Vin/(R1+R2)",
        ],
        assumptions=["ideal resistors; source impedance is zero unless stated"],
        warnings=(
            ["divider current is high; check resistor power ratings"]
            if p_r1 > 0.25 or p_r2 > 0.25 else []
        ),
    )


def calc_rc(p: dict[str, Any]) -> dict[str, Any]:
    mode = (p.get("mode") or "lowpass").lower()
    if mode not in ("lowpass", "highpass"):
        raise CalcError("mode must be lowpass or highpass")
    r, c, fc, f = p.get("r"), p.get("c"), p.get("fc"), p.get("f")
    if r is not None and c is not None:
        r, c = _pos(r, "r"), _pos(c, "c")
        fc = 1.0 / (2 * math.pi * r * c)
    elif fc is not None and c is not None:
        fc, c = _pos(fc, "fc"), _pos(c, "c")
        r = 1.0 / (2 * math.pi * fc * c)
    elif fc is not None and r is not None:
        fc, r = _pos(fc, "fc"), _pos(r, "r")
        c = 1.0 / (2 * math.pi * fc * r)
    else:
        raise CalcError("provide r and c, or fc with one of r/c")
    tau = r * c
    out: dict[str, Any] = {
        "r_ohm": r,
        "c_F": c,
        "fc_Hz": fc,
        "tau_s": tau,
        "rise_time_10_90_s": 2.2 * tau,
        "time_constant_bandwidth_Hz": 1.0 / (2 * math.pi * tau),
        "dc_gain": 1.0 if mode == "lowpass" else 0.0,
        "hf_gain": 0.0 if mode == "lowpass" else 1.0,
    }
    if f is not None:
        f = _pos(f, "f")
        ratio = f / fc
        if mode == "lowpass":
            mag = 1.0 / math.sqrt(1 + ratio ** 2)
            phase = -math.degrees(math.atan(ratio))
        else:
            mag = ratio / math.sqrt(1 + ratio ** 2)
            phase = 90.0 - math.degrees(math.atan(ratio))
        out["evaluated_frequency_Hz"] = f
        out["magnitude"] = mag
        out["magnitude_dB"] = 20 * math.log10(mag) if mag > 0 else -math.inf
        out["phase_deg"] = phase
        out["attenuation_dB"] = -out["magnitude_dB"]
    return result(
        inputs={"r": r, "c": c, "mode": mode, "f": f},
        results=out,
        formulas=[
            "fc = 1/(2*pi*R*C)",
            "tau = R*C",
            "tr(10-90%) = 2.2*tau",
            "|H_LP| = 1/sqrt(1+(f/fc)^2)",
            "|H_HP| = (f/fc)/sqrt(1+(f/fc)^2)",
        ],
        assumptions=["single-pole RC, ideal source and load impedances"],
    )


def calc_rl(p: dict[str, Any]) -> dict[str, Any]:
    mode = (p.get("mode") or "lowpass").lower()
    if mode not in ("lowpass", "highpass"):
        raise CalcError("mode must be lowpass or highpass")
    r, l, fc, f = p.get("r"), p.get("l"), p.get("fc"), p.get("f")
    if r is not None and l is not None:
        r, l = _pos(r, "r"), _pos(l, "l")
        fc = r / (2 * math.pi * l)
    elif fc is not None and l is not None:
        fc, l = _pos(fc, "fc"), _pos(l, "l")
        r = 2 * math.pi * fc * l
    elif fc is not None and r is not None:
        fc, r = _pos(fc, "fc"), _pos(r, "r")
        l = r / (2 * math.pi * fc)
    else:
        raise CalcError("provide r and l, or fc with one of r/l")
    tau = l / r
    out: dict[str, Any] = {
        "r_ohm": r, "l_H": l, "fc_Hz": fc, "tau_s": tau,
        "rise_time_10_90_s": 2.2 * tau,
        "dc_gain": 1.0 if mode == "lowpass" else 0.0,
    }
    if f is not None:
        f = _pos(f, "f")
        ratio = f / fc
        if mode == "lowpass":
            mag = 1.0 / math.sqrt(1 + ratio ** 2)
            phase = -math.degrees(math.atan(ratio))
        else:
            mag = ratio / math.sqrt(1 + ratio ** 2)
            phase = 90.0 - math.degrees(math.atan(ratio))
        out.update({
            "evaluated_frequency_Hz": f,
            "magnitude": mag,
            "magnitude_dB": 20 * math.log10(mag) if mag > 0 else -math.inf,
            "phase_deg": phase,
        })
    return result(
        inputs={"r": r, "l": l, "mode": mode, "f": f},
        results=out,
        formulas=["fc = R/(2*pi*L)", "tau = L/R", "tr = 2.2*tau"],
        assumptions=["ideal inductor with zero series resistance"],
    )


def calc_rlc(p: dict[str, Any]) -> dict[str, Any]:
    topology = (p.get("topology") or "series").lower()
    if topology not in ("series", "parallel"):
        raise CalcError("topology must be series or parallel")
    r, l, c = p.get("r"), p.get("l"), p.get("c")
    f0 = p.get("f0")
    if l is not None and c is not None:
        l, c = _pos(l, "l"), _pos(c, "c")
        f0 = 1.0 / (2 * math.pi * math.sqrt(l * c))
    elif f0 is not None and c is not None:
        f0, c = _pos(f0, "f0"), _pos(c, "c")
        l = 1.0 / ((2 * math.pi * f0) ** 2 * c)
    elif f0 is not None and l is not None:
        f0, l = _pos(f0, "f0"), _pos(l, "l")
        c = 1.0 / ((2 * math.pi * f0) ** 2 * l)
    else:
        raise CalcError("provide l and c, or f0 with one of l/c")
    r = _nonneg(r, "r") if r is not None else 0.0
    z0 = math.sqrt(l / c)
    if topology == "series":
        q = (1.0 / r) * z0 if r > 0 else math.inf
        bw = f0 / q if q not in (0, math.inf) else 0.0
        damping = 1.0 / (2 * q) if q > 0 else math.inf
    else:
        q = r * math.sqrt(c / l) if r > 0 else 0.0
        bw = f0 / q if q > 0 else math.inf
        damping = 1.0 / (2 * q) if q > 0 else math.inf
    out: dict[str, Any] = {
        "r_ohm": r, "l_H": l, "c_F": c, "f0_Hz": f0,
        "characteristic_impedance_ohm": z0,
        "q": q, "bandwidth_Hz": bw, "damping_ratio": damping,
    }
    if p.get("f") is not None:
        f = _pos(p["f"], "f")
        w = 2 * math.pi * f
        if topology == "series":
            z = complex(r, w * l - 1.0 / (w * c))
            out["impedance_magnitude_ohm"] = abs(z)
            out["impedance_phase_deg"] = math.degrees(math.atan2(z.imag, z.real))
        else:
            y = complex(1.0 / r if r > 0 else 0.0, w * c - 1.0 / (w * l))
            z = 1.0 / y if y != 0 else complex(math.inf, math.inf)
            out["impedance_magnitude_ohm"] = abs(z)
            out["impedance_phase_deg"] = math.degrees(math.atan2(z.imag, z.real))
        out["evaluated_frequency_Hz"] = f
    return result(
        inputs={"r": r, "l": l, "c": c, "topology": topology},
        results=out,
        formulas=[
            "f0 = 1/(2*pi*sqrt(L*C))",
            "Z0 = sqrt(L/C)",
            "series: Q = (1/R)*sqrt(L/C), BW = f0/Q",
            "parallel: Q = R*sqrt(C/L), BW = f0/Q",
        ],
        assumptions=["ideal L and C; Q excludes component ESR unless R includes it"],
        warnings=["Q is very high; component tolerances and ESR will dominate"] if q == math.inf or q > 100 else [],
    )


def _quadratic_positive(a: float, b: float, c: float) -> float | None:
    """Smallest positive root of a*x^2 + b*x + c = 0."""
    if abs(a) < 1e-15:
        if abs(b) < 1e-15:
            return None
        x = -c / b
        return x if x > 0 else None
    disc = b * b - 4 * a * c
    if disc < 0:
        return None
    roots = [(-b - math.sqrt(disc)) / (2 * a), (-b + math.sqrt(disc)) / (2 * a)]
    positive = [x for x in roots if x > 0]
    return min(positive) if positive else None


def calc_sallen_key(p: dict[str, Any]) -> dict[str, Any]:
    """Unity-gain and gain-of-K Sallen-Key LP/HP design and analysis.

    Low-pass (R1=R2=R, K=1):
        fc = 1/(2*pi*R*sqrt(C1*C2)),  Q = 0.5*sqrt(C1/C2)
    General low-pass with gain K:
        w0 = 1/sqrt(R1*R2*C1*C2)
        Q  = sqrt(R1*R2*C1*C2) / (C2*(R1+R2) + R1*C1*(1-K))
    High-pass with gain K (C1, C2 series; R1 from the C1/C2 node to
    the output; R2 from the op-amp + input to ground):
        Q  = sqrt(R1*R2*C1*C2) / (R1*(C1+C2) + R2*C2*(1-K))
    The high-pass is *not* obtained by naively swapping labels in the
    low-pass Q formula; the R1 term multiplies (C1+C2).
    """
    topology = (p.get("topology") or "lowpass").lower()
    if topology not in ("lowpass", "highpass"):
        raise CalcError("topology must be lowpass or highpass")
    k = float(p.get("gain", 1.0))
    if k <= 0:
        raise CalcError("gain must be positive")
    fc = p.get("fc")
    q_target = p.get("q")
    c1, c2 = p.get("c1"), p.get("c2")
    r1, r2 = p.get("r1"), p.get("r2")

    # Analysis mode: all four components supplied.
    if all(x is not None for x in (r1, r2, c1, c2)):
        r1, r2 = _pos(r1, "r1"), _pos(r2, "r2")
        c1, c2 = _pos(c1, "c1"), _pos(c2, "c2")
        w0 = 1.0 / math.sqrt(r1 * r2 * c1 * c2)
        if topology == "lowpass":
            denom = c2 * (r1 + r2) + r1 * c1 * (1 - k)
        else:
            denom = r1 * (c1 + c2) + r2 * c2 * (1 - k)
        q = math.sqrt(r1 * r2 * c1 * c2) / denom if denom > 0 else math.inf
        fc = w0 / (2 * math.pi)
        return result(
            inputs={"topology": topology, "gain": k, "r1": r1, "r2": r2, "c1": c1, "c2": c2},
            results={
                "fc_Hz": fc, "q": q,
                "bandwidth_Hz": fc / q if q > 0 else math.inf,
                "peaking_dB": 20 * math.log10(q / math.sqrt(1 - 1 / (4 * q * q))) if q > 1 / math.sqrt(2) else 0.0,
            },
            formulas=["w0 = 1/sqrt(R1*R2*C1*C2)", "Q formula depends on topology and gain K"],
            assumptions=["ideal op-amp, unity-gain or non-inverting gain K", "R1/R2 are the series resistors for LP"],
        )

    _need(p, "fc", "q")
    fc = _pos(fc, "fc")
    q_target = _pos(q_target, "q")
    w0 = 2 * math.pi * fc

    if topology == "lowpass":
        c2 = _pos(c2, "c2") if c2 is not None else 10e-9
        # Solve x = C1/C2 from Q*(2 + x*(1-K)) = sqrt(x).
        a = q_target * (1 - k)
        b = -1.0
        cc = 2 * q_target
        y = _quadratic_positive(a, b, cc)
        if y is None:
            raise CalcError("no real C1/C2 ratio for this gain and Q; lower the gain or Q")
        # y = sqrt(C1/C2), so C1/C2 = y^2.
        c1 = (y ** 2) * c2
        r = 1.0 / (w0 * math.sqrt(c1 * c2))
        r1 = r2 = r
    else:
        c = p.get("c")
        c = _pos(c, "c") if c is not None else 10e-9
        c1 = c2 = c
        # Solve y = sqrt(R1/R2) from 2*Q*y^2 - y + Q*(1-K) = 0.
        a = 2 * q_target
        b = -1.0
        cc = q_target * (1 - k)
        y = _quadratic_positive(a, b, cc)
        if y is None:
            raise CalcError("no real R1/R2 ratio for this gain and Q; lower the gain or Q")
        # w0 = 1/(R2*y*C) -> R2 = 1/(w0*C*y), R1 = y^2*R2.
        r2 = 1.0 / (w0 * c * y)
        r1 = (y ** 2) * r2

    out = {
        "topology": topology,
        "gain": k,
        "fc_Hz": fc,
        "q": q_target,
        "r1_ohm": r1,
        "r2_ohm": r2,
        "c1_F": c1,
        "c2_F": c2,
        "c1_over_c2": c1 / c2,
        "r1_over_r2": r1 / r2,
        "bandwidth_Hz": fc / q_target,
    }
    warnings: list[str] = []
    if k != 1:
        warnings.append(
            "gain K != 1 changes Q; the op-amp feedback network must set K exactly"
        )
    if q_target > 10:
        warnings.append("Q > 10 is sensitive to component tolerance; consider a biquad")
    return result(
        inputs={"topology": topology, "gain": k, "fc": fc, "q": q_target},
        results=out,
        formulas=[
            "w0 = 1/sqrt(R1*R2*C1*C2)",
            "LP: Q = sqrt(R1*R2*C1*C2)/(C2*(R1+R2)+R1*C1*(1-K))",
            "HP: Q = sqrt(R1*R2*C1*C2)/(R1*(C1+C2)+R2*C2*(1-K))",
            "Butterworth: Q = 0.7071 (LP C1 = 2*C2; HP R2 = 2*R1 at K=1)",
        ],
        assumptions=[
            "ideal op-amp with rail-to-rail output and sufficient GBW",
            "equal resistors for LP and equal capacitors for HP in design mode",
        ],
        warnings=warnings,
        notes=[f"nearest E24 C1 = {spice_number(nearest_eseries(c1)['value'])}",
               f"nearest E24 R1 = {spice_number(nearest_eseries(r1)['value'])}"],
    )


# ---------------------------------------------------------------------------
# Device bias and amplifiers
# ---------------------------------------------------------------------------

def calc_bjt_bias(p: dict[str, Any]) -> dict[str, Any]:
    vcc = _pos(p.get("vcc"), "vcc")
    ic = _pos(p.get("ic"), "ic")
    beta = _pos(p.get("beta", 100), "beta")
    vbe = float(p.get("vbe", 0.7))
    re = _pos(p.get("re"), "re")
    rc = p.get("rc")
    vce_target = p.get("vce")
    if rc is None and vce_target is None:
        raise CalcError("provide rc or vce")
    if rc is None:
        vce_target = _pos(vce_target, "vce")
        rc = (vcc - vce_target - ic * re) / ic
    rc = _pos(rc, "rc")
    stiffness = _pos(p.get("stiffness", 10.0), "stiffness")
    rload = p.get("rload")
    bypassed = bool(p.get("bypassed", False))

    ve = ic * re
    vb = ve + vbe
    if vb >= vcc:
        raise CalcError("Vb >= Vcc: reduce Re or Ic, or raise Vcc")
    idiv = stiffness * ic
    ib = ic / beta
    r2 = vb / idiv
    r1 = (vcc - vb) / (idiv + ib)

    # Actual operating point with the finite divider impedance.
    vbb = vcc * r2 / (r1 + r2)
    rb = parallel(r1, r2)
    ib_actual = (vbb - vbe) / (rb + (beta + 1) * re)
    if ib_actual <= 0:
        raise CalcError("divider cannot forward-bias the base; increase stiffness or Vcc")
    ic_actual = beta * ib_actual
    ie_actual = (beta + 1) * ib_actual
    ve_actual = ie_actual * re
    vc_actual = vcc - ic_actual * rc
    vce_actual = vc_actual - ve_actual

    gm = ic_actual / VT_300K
    rpi = beta / gm
    rload_eff = parallel(rc, rload) if rload is not None else rc
    av = -gm * rload_eff / (1 + gm * re) if not bypassed else -gm * rload_eff
    rin = parallel(rb, rpi)
    rout = rc
    warnings: list[str] = []
    if vce_actual < 0.3:
        warnings.append(
            f"transistor is in saturation (Vce={vce_actual:.3f} V); reduce Rc or Ic"
        )
    if r1 <= 0 or r2 <= 0:
        raise CalcError("computed bias resistors are not positive; change the design targets")
    if p.get("p_r1") is None:
        pass
    return result(
        inputs={"vcc": vcc, "ic": ic, "beta": beta, "vbe": vbe, "re": re,
                "rc": rc, "stiffness": stiffness, "rload": rload, "bypassed": bypassed},
        results={
            "r1_ohm": r1, "r2_ohm": r2,
            "ve_V": ve_actual, "vb_V": ve_actual + vbe, "vc_V": vc_actual,
            "vce_V": vce_actual, "ic_A": ic_actual, "ie_A": ie_actual, "ib_A": ib_actual,
            "divider_current_A": idiv,
            "gm_S": gm, "rpi_ohm": rpi,
            "voltage_gain": av, "input_impedance_ohm": rin, "output_impedance_ohm": rout,
            "p_transistor_W": vce_actual * ic_actual,
            "p_rc_W": ic_actual ** 2 * rc,
            "p_re_W": ie_actual ** 2 * re,
            "p_r1_W": (vcc - (ve_actual + vbe)) ** 2 / r1,
            "p_r2_W": (ve_actual + vbe) ** 2 / r2,
        },
        formulas=[
            "Ve = Ie*Re, Vb = Ve + Vbe",
            "R2 = Vb/(k*Ic), R1 = (Vcc-Vb)/(k*Ic + Ic/beta)",
            "Ib = (Vbb-Vbe)/(Rb+(beta+1)*Re)",
            "gm = Ic/VT, rpi = beta/gm, Av = -gm*(Rc||Rload)/(1+gm*Re)",
        ],
        assumptions=[
            f"Vbe = {vbe} V, VT = {VT_300K*1000:.2f} mV at 300 K",
            f"divider stiffness k = {stiffness} (I_R2 / Ic)",
            "generic hybrid-pi model; Early effect and capacitances ignored",
            "bypassed emitter assumes an ideal bypass capacitor at signal frequency"
            if bypassed else "emitter resistor is not bypassed",
        ],
        warnings=warnings,
        notes=["verify with a vendor SPICE model and the datasheet Safe Operating Area"],
    )


def calc_mosfet_bias(p: dict[str, Any]) -> dict[str, Any]:
    vdd = _pos(p.get("vdd"), "vdd")
    idd = _pos(p.get("id"), "id")
    vth = float(p.get("vth", 2.0))
    kn = p.get("kn")
    vov = p.get("vov")
    if kn is not None:
        kn = _pos(kn, "kn")
        vov = math.sqrt(2 * idd / kn)
    elif vov is not None:
        vov = _pos(vov, "vov")
        kn = 2 * idd / (vov ** 2)
    else:
        raise CalcError("provide kn (A/V^2) or vov (V)")
    rd = _pos(p.get("rd"), "rd")
    rs = _pos(p.get("rs", 0.0), "rs") if p.get("rs") is not None else 0.0
    lam = float(p.get("lambda", 0.0))
    rload = p.get("rload")
    vgs = vth + vov
    vs = idd * rs
    vg = vs + vgs
    vd = vdd - idd * rd
    vds = vd - vs
    gm = 2 * idd / vov
    ro = 1.0 / (lam * idd) if lam > 0 else None
    ro_eff = ro if ro is not None else math.inf
    rload_eff = parallel(rd, rload, ro_eff) if rload is not None else parallel(rd, ro_eff)
    av = -gm * rload_eff
    warnings: list[str] = []
    if vds < vov:
        warnings.append(
            f"MOSFET is not in saturation (Vds={vds:.3f} V < Vov={vov:.3f} V)"
        )
    if vg >= vdd:
        warnings.append("gate bias is at or above Vdd; check the divider")
    return result(
        inputs={"vdd": vdd, "id": idd, "vth": vth, "kn": kn, "rd": rd, "rs": rs,
                "lambda": lam, "rload": rload},
        results={
            "vov_V": vov, "vgs_V": vgs, "vg_V": vg, "vs_V": vs, "vd_V": vd,
            "vds_V": vds, "gm_S": gm, "ro_ohm": ro,
            "voltage_gain": av, "p_mosfet_W": vds * idd,
            "p_rd_W": idd ** 2 * rd, "p_rs_W": idd ** 2 * rs,
        },
        formulas=[
            "Vov = sqrt(2*Id/kn), Vgs = Vth + Vov",
            "Vs = Id*Rs, Vg = Vs + Vgs, Vd = Vdd - Id*Rd",
            "gm = 2*Id/Vov, ro = 1/(lambda*Id), Av = -gm*(Rd||Rload||ro)",
        ],
        assumptions=["square-law saturation model; no body effect or subthreshold conduction"],
        warnings=warnings,
        notes=["verify against the datasheet and a vendor model; gate charge and SOA are not modelled"],
    )


def calc_opamp(p: dict[str, Any]) -> dict[str, Any]:
    gain = p.get("gain")
    gbw = p.get("gbw")
    sr = p.get("sr")
    f = _pos(p.get("f", 1e3), "f")
    vpeak = _pos(p.get("vpeak", 1.0), "vpeak")
    margin = _pos(p.get("margin", 10.0), "margin")
    sr_margin = _pos(p.get("sr_margin", 2.0), "sr_margin")
    rload = p.get("rload")
    supply = p.get("supply")
    rf, rg, rin = p.get("rf"), p.get("rg"), p.get("rin")
    mode = (p.get("mode") or "noninverting").lower()
    if mode not in ("noninverting", "inverting"):
        raise CalcError("mode must be noninverting or inverting")

    if gain is None:
        if mode == "noninverting" and rf is not None and rg is not None:
            gain = 1 + rf / rg
        elif mode == "inverting" and rf is not None and rin is not None:
            gain = -rf / rin
        else:
            raise CalcError("provide gain, or rf with rg/rin")
    gain = float(gain)
    ag = abs(gain)
    if ag == 0:
        raise CalcError("gain must be non-zero")

    out: dict[str, Any] = {
        "mode": mode, "gain": gain, "signal_frequency_Hz": f,
        "required_gbw_Hz": margin * ag * f,
        "required_slew_rate_V_per_s": sr_margin * 2 * math.pi * f * vpeak,
        "full_power_bandwidth_Hz_at_slew": (sr / (2 * math.pi * vpeak)) if sr else None,
    }
    warnings: list[str] = []
    if gbw is not None:
        gbw = _pos(gbw, "gbw")
        out["closed_loop_bandwidth_Hz"] = gbw / ag
        out["gbw_margin"] = (gbw / ag) / f
        if gbw < margin * ag * f:
            warnings.append(
                f"GBW {si_number(gbw,'Hz')} is below the {margin:g}x margin; "
                f"closed-loop bandwidth is only {si_number(gbw/ag,'Hz')}"
            )
    if sr is not None:
        sr = _pos(sr, "sr")
        need_sr = 2 * math.pi * f * vpeak
        out["slew_rate_margin"] = sr / need_sr if need_sr else math.inf
        if sr < sr_margin * need_sr:
            warnings.append(
                f"slew rate {si_number(sr,'V/s')} is below {sr_margin:g}x the "
                f"required {si_number(need_sr,'V/s')}"
            )
    if rload is not None:
        rload = _pos(rload, "rload")
        out["output_current_A"] = vpeak / rload
    if supply is not None:
        out["headroom_margin_V"] = supply / 2 - vpeak
        if vpeak > supply / 2:
            warnings.append(
                "peak output exceeds half the supply rail; a mid-rail-biased "
                "output cannot swing that far"
            )
    return result(
        inputs={"gain": gain, "gbw": gbw, "sr": sr, "f": f, "vpeak": vpeak,
                "mode": mode, "rload": rload, "supply": supply},
        results=out,
        formulas=[
            "non-inverting: G = 1 + Rf/Rg; inverting: G = -Rf/Rin",
            "closed-loop BW = GBW/|G|",
            "SR >= 2*pi*f*Vpeak (use 2x-3x margin)",
            "full-power bandwidth = SR/(2*pi*Vpeak)",
        ],
        assumptions=[
            "op-amp is compensated and unity-gain stable",
            "GBW and SR are datasheet typical values at the intended supply",
            "slew requirement uses a sinusoidal output at Vpeak",
            "headroom assumes a mid-rail-biased output; check the datasheet swing at the load",
        ],
        warnings=warnings,
        notes=["also check input common-mode range, output swing, offset and noise"],
    )


def calc_ldo(p: dict[str, Any]) -> dict[str, Any]:
    vin = _pos(p.get("vin"), "vin")
    vout = _pos(p.get("vout"), "vout")
    iout = _pos(p.get("iout"), "iout")
    iq = _nonneg(p.get("iq", 0.0), "iq")
    if vout > vin:
        raise CalcError("vout must not exceed vin for an LDO")
    dropout = p.get("dropout")
    theta_ja = p.get("theta_ja")
    theta_jc, theta_cs, theta_sa = p.get("theta_jc"), p.get("theta_cs"), p.get("theta_sa")
    if theta_ja is None and any(x is not None for x in (theta_jc, theta_cs, theta_sa)):
        theta_ja = sum(x or 0.0 for x in (theta_jc, theta_cs, theta_sa))
    ta = float(p.get("ta", 25.0))
    tj_max = float(p.get("tj_max", 125.0))
    p_diss = (vin - vout) * iout + vin * iq
    out: dict[str, Any] = {
        "power_dissipation_W": p_diss,
        "efficiency": (vout * iout) / (vin * (iout + iq)) if iout + iq > 0 else 0.0,
        "quiescent_power_W": vin * iq,
    }
    warnings: list[str] = []
    if dropout is not None:
        dropout = _nonneg(dropout, "dropout")
        out["headroom_V"] = vin - vout
        out["dropout_margin_V"] = (vin - vout) - dropout
        if vin - vout < dropout:
            warnings.append("input-output headroom is below the dropout voltage")
    if theta_ja is not None:
        theta_ja = _pos(theta_ja, "theta_ja")
        tj = ta + p_diss * theta_ja
        out.update({
            "theta_ja_C_per_W": theta_ja, "tj_C": tj,
            "thermal_margin_C": tj_max - tj,
            "max_power_at_tj_max_W": (tj_max - ta) / theta_ja,
        })
        if tj > tj_max:
            warnings.append(
                f"junction temperature {tj:.1f} C exceeds Tj(max) {tj_max:.1f} C"
            )
        if tj > 0.8 * tj_max:
            warnings.append("junction temperature exceeds 80% of Tj(max) derating")
    return result(
        inputs={"vin": vin, "vout": vout, "iout": iout, "iq": iq, "dropout": dropout,
                "theta_ja": theta_ja, "ta": ta, "tj_max": tj_max},
        results=out,
        formulas=[
            "P = (Vin-Vout)*Iout + Vin*Iq",
            "efficiency = Vout*Iout/(Vin*(Iout+Iq))",
            "Tj = Ta + P*theta_ja",
        ],
        assumptions=["linear regulator; Iq is the quiescent current at the operating point"],
        warnings=warnings,
        notes=["verify dropout at the actual load current and temperature; check output capacitor ESR"],
    )


# ---------------------------------------------------------------------------
# Switching converters
# ---------------------------------------------------------------------------

def calc_buck(p: dict[str, Any]) -> dict[str, Any]:
    vin = _pos(p.get("vin"), "vin")
    vout = _pos(p.get("vout"), "vout")
    iout = _pos(p.get("iout"), "iout")
    fsw = _pos(p.get("fsw"), "fsw")
    if vout >= vin:
        raise CalcError("buck requires vout < vin")
    ripple = _pos(p.get("ripple", 0.30), "ripple")
    vf = _nonneg(p.get("vf", 0.0), "vf")
    d = (vout + vf) / (vin + vf)
    l_req = (vin - vout) * d / (ripple * iout * fsw)
    l = p.get("l")
    if l is not None:
        l = _pos(l, "l")
        ripple_actual = (vin - vout) * d / (l * fsw * iout)
    else:
        l = l_req
        ripple_actual = ripple
    ipk = iout * (1 + ripple_actual / 2)
    ivalley = iout * (1 - ripple_actual / 2)
    lcrit = (vin - vout) * d / (2 * iout * fsw)
    cout_req = ripple_actual * iout / (8 * fsw * max(p.get("vout_ripple", 0.01) or 0.01, 1e-9))
    cin_rms = iout * math.sqrt(d * (1 - d))
    out: dict[str, Any] = {
        "duty_cycle": d,
        "inductor_required_H": l_req,
        "inductor_used_H": l,
        "inductor_ripple_ratio": ripple_actual,
        "inductor_ripple_A": ripple_actual * iout,
        "inductor_peak_A": ipk,
        "inductor_valley_A": ivalley,
        "inductor_rms_A": math.sqrt(iout ** 2 + (ripple_actual * iout) ** 2 / 12),
        "inductor_ccm_boundary_H": lcrit,
        "ccm": l >= lcrit,
        "output_cap_required_F": cout_req,
        "input_cap_rms_A": cin_rms,
        "switch_rms_A": iout * math.sqrt(d),
        "diode_rms_A": iout * math.sqrt(1 - d),
        "switch_peak_A": ipk,
    }
    warnings: list[str] = []
    if l < lcrit:
        warnings.append(
            "inductor is below the CCM boundary; the converter enters DCM at this load"
        )
    if d > 0.9:
        warnings.append("duty cycle is very high; check the switch minimum off-time")
    if d < 0.1:
        warnings.append("duty cycle is very low; check the switch minimum on-time")
    vds_rating = p.get("vds_rating")
    if vds_rating is not None and vin * 1.3 > vds_rating:
        warnings.append("switch voltage rating is below Vin plus margin")
    return result(
        inputs={"vin": vin, "vout": vout, "iout": iout, "fsw": fsw,
                "ripple": ripple, "vf": vf, "l": p.get("l")},
        results=out,
        formulas=[
            "D = (Vout+Vf)/(Vin+Vf)",
            "L = (Vin-Vout)*D/(r*Iout*fsw)",
            "Ipk = Iout*(1+r/2)",
            "Cout >= r*Iout/(8*fsw*dVout)",
            "Lcrit = (Vin-Vout)*D/(2*Iout*fsw)",
        ],
        assumptions=[
            "CCM, ideal switches, diode drop Vf included, no switch/inductor losses",
            f"output ripple target {si_number(float(p.get('vout_ripple',0.01) or 0.01),'V')}",
        ],
        warnings=warnings,
        notes=["verify with the regulator datasheet and a vendor simulation; check thermal and loop stability"],
    )


def calc_boost(p: dict[str, Any]) -> dict[str, Any]:
    vin = _pos(p.get("vin"), "vin")
    vout = _pos(p.get("vout"), "vout")
    iout = _pos(p.get("iout"), "iout")
    fsw = _pos(p.get("fsw"), "fsw")
    if vout <= vin:
        raise CalcError("boost requires vout > vin")
    ripple = _pos(p.get("ripple", 0.30), "ripple")
    vf = _nonneg(p.get("vf", 0.0), "vf")
    d = 1 - vin / (vout + vf)
    il = iout / (1 - d)
    l_req = vin * d / (ripple * il * fsw)
    l = p.get("l")
    if l is not None:
        l = _pos(l, "l")
        ripple_actual = vin * d / (l * fsw * il)
    else:
        l = l_req
        ripple_actual = ripple
    ipk = il * (1 + ripple_actual / 2)
    lcrit = vin * d / (2 * il * fsw)
    cout_req = iout * d / (fsw * max(p.get("vout_ripple", 0.01) or 0.01, 1e-9))
    out: dict[str, Any] = {
        "duty_cycle": d,
        "input_current_A": il,
        "inductor_required_H": l_req,
        "inductor_used_H": l,
        "inductor_ripple_ratio": ripple_actual,
        "inductor_ripple_A": ripple_actual * il,
        "inductor_peak_A": ipk,
        "inductor_ccm_boundary_H": lcrit,
        "ccm": l >= lcrit,
        "output_cap_required_F": cout_req,
        "switch_rms_A": il * math.sqrt(d),
        "diode_rms_A": il * math.sqrt(1 - d),
        "switch_peak_A": ipk,
    }
    warnings: list[str] = []
    if l < lcrit:
        warnings.append("inductor is below the CCM boundary; the converter enters DCM")
    if d > 0.9:
        warnings.append("duty cycle is very high; check switch and diode ratings")
    return result(
        inputs={"vin": vin, "vout": vout, "iout": iout, "fsw": fsw,
                "ripple": ripple, "vf": vf, "l": p.get("l")},
        results=out,
        formulas=[
            "D = 1 - Vin/(Vout+Vf)",
            "IL = Iout/(1-D)",
            "L = Vin*D/(r*IL*fsw)",
            "Cout >= Iout*D/(fsw*dVout)",
        ],
        assumptions=["CCM, ideal switch, diode drop Vf included, no parasitic losses"],
        warnings=warnings,
        notes=["boost converters have a right-half-plane zero; loop compensation must be checked"],
    )


# ---------------------------------------------------------------------------
# Components and interfaces
# ---------------------------------------------------------------------------

def calc_led(p: dict[str, Any]) -> dict[str, Any]:
    vs = _pos(p.get("vs"), "vs")
    vf = _pos(p.get("vf"), "vf")
    n = int(p.get("n", 1) or 1)
    if n < 1:
        raise CalcError("n must be >= 1")
    total_vf = n * vf
    if total_vf >= vs:
        raise CalcError(f"supply {vs} V cannot drive {n} LED(s) with Vf={vf} V")
    r = p.get("r")
    if r is not None:
        r = _pos(r, "r")
        current = (vs - total_vf) / r
    else:
        current = _pos(p.get("if"), "if")
        r = (vs - total_vf) / current
    out: dict[str, Any] = {
        "series_resistance_ohm": r,
        "led_current_A": current,
        "led_voltage_V": total_vf,
        "resistor_power_W": current ** 2 * r,
        "led_power_W": total_vf * current,
        "efficiency": total_vf / vs,
    }
    if p.get("r_rating") is not None:
        rating = _pos(p["r_rating"], "r_rating")
        out["resistor_derating"] = out["resistor_power_W"] / rating
    warnings: list[str] = []
    if out.get("resistor_derating", 0.0) > 0.6:
        warnings.append("resistor dissipation exceeds 60% of its rating")
    if vs - total_vf < 0.1 * vs:
        warnings.append("very little headroom across the resistor; current varies strongly with Vf")
    return result(
        inputs={"vs": vs, "vf": vf, "n": n, "if": current, "r": r},
        results=out,
        formulas=["R = (Vs - n*Vf)/If", "P_R = If^2*R"],
        assumptions=["constant Vf at the operating current; LED Vf has a strong temperature coefficient"],
        warnings=warnings,
        notes=["use the datasheet Vf at the intended current; check LED thermal derating"],
    )


def calc_zener(p: dict[str, Any]) -> dict[str, Any]:
    vin_min = _pos(p.get("vin_min"), "vin_min")
    vin_max = _pos(p.get("vin_max"), "vin_max")
    vz = _pos(p.get("vz"), "vz")
    iz_min = _pos(p.get("iz_min"), "iz_min")
    iz_max = _pos(p.get("iz_max"), "iz_max")
    il_min = _nonneg(p.get("il_min", 0.0), "il_min")
    il_max = _nonneg(p.get("il_max", 0.0), "il_max")
    if vin_min <= vz:
        raise CalcError("vin_min must exceed vz")
    rs_max = (vin_min - vz) / (iz_min + il_max)
    rs_min = (vin_max - vz) / (iz_max + il_min)
    if rs_min > rs_max:
        raise CalcError("no series resistor satisfies the zener current window")
    rs = p.get("rs")
    if rs is not None:
        rs = _pos(rs, "rs")
    else:
        rs = nearest_eseries((rs_min + rs_max) / 2, "E24", "nearest")["value"]
        if not (rs_min <= rs <= rs_max):
            rs = nearest_eseries(rs_max, "E24", "down")["value"]
    iz_at_min = (vin_min - vz) / rs - il_max
    iz_at_max = (vin_max - vz) / rs - il_min
    rz = p.get("rz")
    out: dict[str, Any] = {
        "rs_ohm": rs, "rs_min_ohm": rs_min, "rs_max_ohm": rs_max,
        "iz_min_actual_A": iz_at_min, "iz_max_actual_A": iz_at_max,
        "p_zener_max_W": vz * max(iz_at_max, 0.0),
        "p_rs_max_W": (vin_max - vz) ** 2 / rs,
        "vout_min_V": vz, "vout_max_V": vz,
    }
    if rz is not None:
        rz = _nonneg(rz, "rz")
        out["vout_min_V"] = vz + rz * iz_at_min
        out["vout_max_V"] = vz + rz * iz_at_max
        out["load_regulation_ohm"] = rz
    warnings: list[str] = []
    if iz_at_min < iz_min:
        warnings.append("zener current falls below the regulation minimum at low line/high load")
    if iz_at_max > iz_max:
        warnings.append("zener current exceeds the design maximum at high line/light load")
    return result(
        inputs={"vin_min": vin_min, "vin_max": vin_max, "vz": vz, "iz_min": iz_min,
                "iz_max": iz_max, "il_min": il_min, "il_max": il_max, "rs": rs},
        results=out,
        formulas=[
            "Rs_max = (Vin_min - Vz)/(Iz_min + Iload_max)",
            "Rs_min = (Vin_max - Vz)/(Iz_max + Iload_min)",
            "Pz = Vz*Iz",
        ],
        assumptions=["zener is biased in reverse breakdown; Vz is specified at the test current"],
        warnings=warnings,
        notes=["zener tolerance and temperature coefficient are not included; use a shunt reference for precision"],
    )


def calc_i2c(p: dict[str, Any]) -> dict[str, Any]:
    vdd = _pos(p.get("vdd"), "vdd")
    vol = _nonneg(p.get("vol", 0.4), "vol")
    iol = _pos(p.get("iol", 3e-3), "iol")
    cb = _pos(p.get("cb"), "cb")
    tr = _pos(p.get("tr", 300e-9), "tr")
    rp_min = (vdd - vol) / iol
    rp_max = tr / (0.8473 * cb)
    if rp_min > rp_max:
        raise CalcError("no pull-up resistor satisfies both VOL and rise-time limits")
    rp = p.get("rp")
    if rp is not None:
        rp = _pos(rp, "rp")
    else:
        rp = nearest_eseries((rp_min + rp_max) / 2, "E24", "nearest")["value"]
        if not (rp_min <= rp <= rp_max):
            rp = nearest_eseries(rp_max, "E24", "down")["value"]
    tr_actual = 0.8473 * rp * cb
    out: dict[str, Any] = {
        "rp_min_ohm": rp_min, "rp_max_ohm": rp_max, "rp_ohm": rp,
        "rise_time_s": tr_actual,
        "power_per_pullup_W": vdd ** 2 / rp,
        "max_bus_capacitance_F": tr / (0.8473 * rp),
        "sink_current_A": (vdd - vol) / rp,
    }
    warnings: list[str] = []
    if tr_actual > tr:
        warnings.append("rise time with the selected pull-up exceeds the target")
    if rp < 1000:
        warnings.append("pull-up below 1 kOhm increases bus current significantly")
    return result(
        inputs={"vdd": vdd, "vol": vol, "iol": iol, "cb": cb, "tr": tr, "rp": rp},
        results=out,
        formulas=[
            "Rp(min) = (VDD - VOL)/IOL",
            "Rp(max) = tr/(0.8473*Cb)",
            "tr = 0.8473*Rp*Cb",
        ],
        assumptions=["standard-mode/fast-mode I2C bus; Cb includes all devices, traces and connectors"],
        warnings=warnings,
        notes=["verify against NXP UM10204 and the specific device's VOL/IOL limits"],
    )


def calc_adc(p: dict[str, Any]) -> dict[str, Any]:
    bits = int(p.get("bits", 12))
    if bits < 1:
        raise CalcError("bits must be >= 1")
    vref = _pos(p.get("vref"), "vref")
    full_scale = vref if p.get("single_ended", True) else 2 * vref
    lsb = full_scale / (2 ** bits)
    snr_ideal = 6.02 * bits + 1.76
    sinad = p.get("sinad")
    out: dict[str, Any] = {
        "bits": bits, "codes": 2 ** bits, "vref_V": vref,
        "full_scale_V": full_scale, "lsb_V": lsb,
        "snr_ideal_dB": snr_ideal,
        "quantization_noise_Vrms": lsb / math.sqrt(12),
    }
    if sinad is not None:
        sinad = float(sinad)
        out["sinad_dB"] = sinad
        out["enob_bits"] = (sinad - 1.76) / 6.02
    if p.get("vin") is not None:
        vin = float(p["vin"])
        code = math.floor(vin / lsb)
        code = min(max(code, 0), 2 ** bits - 1)
        out["input_V"] = vin
        out["code"] = code
        out["code_hex"] = f"0x{code:0{(bits + 3)//4}X}"
        out["reconstruction_error_V"] = vin - code * lsb
    osr = int(p.get("osr", 1) or 1)
    if osr > 1:
        out["oversampling_ratio"] = osr
        out["snr_gain_dB"] = 10 * math.log10(osr)
        out["enob_gain_bits"] = 0.5 * math.log2(osr)
    return result(
        inputs={"bits": bits, "vref": vref, "vin": p.get("vin"), "sinad": sinad, "osr": osr},
        results=out,
        formulas=[
            "LSB = Vref/2^N",
            "SNR_ideal = 6.02*N + 1.76 dB",
            "ENOB = (SINAD - 1.76)/6.02",
            "oversampling SNR gain = 10*log10(OSR) for white noise",
        ],
        assumptions=["ideal quantizer; SINAD includes all converter noise and distortion"],
        notes=["real ENOB is limited by reference noise, clock jitter, INL/DNL and driver settling"],
    )


def calc_logic(p: dict[str, Any]) -> dict[str, Any]:
    vdd = float(p.get("vdd", 3.3))
    voh = float(p.get("voh", vdd))
    vol = float(p.get("vol", 0.0))
    vih = float(p.get("vih", 0.7 * vdd))
    vil = float(p.get("vil", 0.3 * vdd))
    nmh = voh - vih
    nml = vil - vol
    out: dict[str, Any] = {
        "vdd_V": vdd, "noise_margin_high_V": nmh, "noise_margin_low_V": nml,
    }
    warnings: list[str] = []
    if nmh <= 0 or nml <= 0:
        warnings.append("logic levels do not provide positive noise margin")
    ioh, iih = p.get("ioh"), p.get("iih")
    iol, iil = p.get("iol"), p.get("iil")
    if all(x is not None for x in (ioh, iih, iol, iil)):
        fanout_h = ioh / iih if iih else math.inf
        fanout_l = iol / iil if iil else math.inf
        out["fanout_high"] = fanout_h
        out["fanout_low"] = fanout_l
        out["fanout"] = min(fanout_h, fanout_l)
        if out["fanout"] < 1:
            warnings.append("fanout is below 1; the driver cannot meet the DC load")
    tpd, tcomb, tsu, th, tskew = (p.get("tpd"), p.get("tcomb"), p.get("tsu"), p.get("th"), p.get("tskew"))
    if tpd is not None and tcomb is not None and tsu is not None:
        skew = tskew or 0.0
        period = tpd + tcomb + tsu + skew
        out["min_clock_period_s"] = period
        out["max_clock_frequency_Hz"] = 1.0 / period if period > 0 else math.inf
        if th is not None:
            out["hold_margin_s"] = (tpd + tcomb) - th - skew
    cl, f = p.get("c_load"), p.get("f")
    if cl is not None and f is not None:
        out["dynamic_power_W"] = cl * vdd ** 2 * f
        out["dynamic_current_A"] = cl * vdd * f
    return result(
        inputs={"vdd": vdd, "voh": voh, "vol": vol, "vih": vih, "vil": vil,
                "ioh": ioh, "iih": iih, "iol": iol, "iil": iil,
                "tpd": tpd, "tcomb": tcomb, "tsu": tsu, "th": th, "tskew": tskew},
        results=out,
        formulas=[
            "NMH = VOH - VIH; NML = VIL - VOL",
            "fanout = min(IOH/IIH, IOL/IIL)",
            "Tmin = tpd + tcomb + tsu + skew",
            "P_dyn = C*V^2*f",
        ],
        assumptions=[
            "DC noise margins use the worst-case datasheet levels at the same supply and temperature",
            "setup uses maximum delays; hold should use minimum delays (the same values are used here)",
        ],
        warnings=warnings,
        notes=["include clock jitter, crosstalk, ground bounce and input capacitance in a real timing budget"],
    )


# ---------------------------------------------------------------------------
# Thermal, PCB and reliability
# ---------------------------------------------------------------------------

def calc_thermal(p: dict[str, Any]) -> dict[str, Any]:
    power = _nonneg(p.get("p"), "p")
    ta = float(p.get("ta", 25.0))
    tj_max = float(p.get("tj_max", 125.0))
    theta_jc = p.get("theta_jc")
    theta_cs = p.get("theta_cs")
    theta_sa = p.get("theta_sa")
    theta_ja = p.get("theta_ja")
    if theta_ja is None:
        if theta_jc is None:
            raise CalcError("provide theta_ja, or theta_jc with theta_cs/theta_sa")
        theta_ja = (theta_jc or 0.0) + (theta_cs or 0.0) + (theta_sa or 0.0)
    theta_ja = _pos(theta_ja, "theta_ja")
    tj = ta + power * theta_ja
    derating = float(p.get("derating", 0.80))
    max_allowed = derating * tj_max
    required_theta = (max_allowed - ta) / power if power > 0 else math.inf
    required_sa = None
    if theta_jc is not None:
        required_sa = required_theta - (theta_jc or 0.0) - (theta_cs or 0.0)
    out: dict[str, Any] = {
        "power_W": power, "ta_C": ta, "tj_C": tj,
        "theta_ja_C_per_W": theta_ja,
        "thermal_margin_C": tj_max - tj,
        "derating_limit_C": max_allowed,
        "max_power_at_tj_max_W": (tj_max - ta) / theta_ja,
        "max_power_at_derating_W": (max_allowed - ta) / theta_ja,
        "required_theta_ja_C_per_W": required_theta,
        "required_theta_sa_C_per_W": required_sa,
    }
    warnings: list[str] = []
    if tj > tj_max:
        warnings.append(f"Tj {tj:.1f} C exceeds Tj(max) {tj_max:.1f} C")
    elif tj > max_allowed:
        warnings.append(f"Tj {tj:.1f} C exceeds the {derating*100:.0f}% derating limit {max_allowed:.1f} C")
    if required_sa is not None and required_sa <= 0:
        warnings.append("no heatsink is required at this power, but verify the package theta_jc")
    return result(
        inputs={"p": power, "ta": ta, "tj_max": tj_max, "theta_jc": theta_jc,
                "theta_cs": theta_cs, "theta_sa": theta_sa, "theta_ja": theta_ja},
        results=out,
        formulas=[
            "Tj = Ta + P*(theta_jc + theta_cs + theta_sa)",
            "theta_sa <= (Tj_limit-Ta)/P - theta_jc - theta_cs",
        ],
        assumptions=[
            f"steady-state thermal resistance; derating limit = {derating*100:.0f}% of Tj(max)",
            "theta values are for the stated airflow, copper area and mounting",
        ],
        warnings=warnings,
        notes=["thermal resistance is not a datasheet constant in real boards; verify in the enclosure"],
    )


def calc_trace(p: dict[str, Any]) -> dict[str, Any]:
    current = p.get("current")
    delta_t = p.get("delta_t")
    width = p.get("width_mil")
    oz = _pos(p.get("oz", 1.0), "oz")
    internal = bool(p.get("internal", False))
    k = 0.024 if internal else 0.048
    thickness_mil = oz * OZ_TO_MIL
    out: dict[str, Any] = {
        "copper_oz": oz, "thickness_mil": thickness_mil,
        "thickness_mm": thickness_mil * 0.0254,
        "internal": internal, "k": k,
    }
    warnings: list[str] = []
    if current is not None and delta_t is not None:
        current = _pos(current, "current")
        delta_t = _pos(delta_t, "delta_t")
        area_mil2 = (current / (k * delta_t ** 0.44)) ** (1 / 0.725)
        width_mil = area_mil2 / thickness_mil
        out.update({
            "current_A": current, "delta_t_C": delta_t,
            "area_mil2": area_mil2, "width_mil": width_mil,
            "width_mm": width_mil * 0.0254,
        })
        if width_mil < 6:
            warnings.append("trace is very narrow; fabrication minimum width may dominate")
        if delta_t > 30:
            warnings.append("temperature rise above 30 C is aggressive for continuous copper")
    elif width is not None and delta_t is not None:
        width = _pos(width, "width_mil")
        delta_t = _pos(delta_t, "delta_t")
        area_mil2 = width * thickness_mil
        imax = k * delta_t ** 0.44 * area_mil2 ** 0.725
        out.update({
            "width_mil": width, "width_mm": width * 0.0254,
            "delta_t_C": delta_t, "area_mil2": area_mil2,
            "max_current_A": imax,
        })
    else:
        raise CalcError("provide current and delta_t, or width_mil and delta_t")
    return result(
        inputs={"current": p.get("current"), "delta_t": p.get("delta_t"),
                "width_mil": p.get("width_mil"), "oz": oz, "internal": internal},
        results=out,
        formulas=[
            "IPC-2221: I = k*dT^0.44*A^0.725",
            "A in mil^2, k=0.048 external / 0.024 internal",
            "width = A/(oz*1.378 mil)",
        ],
        assumptions=["IPC-2221 chart fit; still air, single isolated trace, 10 C-30 C rise"],
        warnings=warnings,
        notes=["IPC-2152 supersedes IPC-2221 for tight designs; verify with the fabricator's calculator"],
    )


def calc_battery(p: dict[str, Any]) -> dict[str, Any]:
    capacity = _pos(p.get("capacity_mah"), "capacity_mah")
    load = _pos(p.get("load_ma"), "load_ma")
    derate = _pos(p.get("derate", 0.80), "derate")
    efficiency = float(p.get("efficiency", 1.0))
    if not 0 < efficiency <= 1:
        raise CalcError("efficiency must be in (0, 1]")
    rated_hours = _pos(p.get("rated_hours", 20.0), "rated_hours")
    battery_load = load / efficiency
    peukert = p.get("peukert")
    if peukert is not None:
        peukert = _pos(peukert, "peukert")
        # Peukert: t = H*(C/(I*H))^k.  C is the capacity at the rated H-hour rate.
        hours = rated_hours * (capacity / (battery_load * rated_hours)) ** peukert * derate
    else:
        hours = capacity * derate / battery_load
    out: dict[str, Any] = {
        "runtime_h": hours,
        "runtime_min": hours * 60,
        "usable_capacity_mah": capacity * derate,
        "load_ma": load,
        "battery_load_ma": battery_load,
        "energy_wh": capacity / 1000 * float(p.get("cell_voltage", 3.7)),
    }
    cells_s = int(p.get("cells_series", 1) or 1)
    cells_p = int(p.get("cells_parallel", 1) or 1)
    cell_v = float(p.get("cell_voltage", 3.7))
    out.update({
        "pack_voltage_V": cell_v * cells_s,
        "pack_capacity_mah": capacity * cells_p,
        "pack_energy_wh": cell_v * cells_s * capacity * cells_p / 1000,
    })
    return result(
        inputs={"capacity_mah": capacity, "load_ma": load, "derate": derate,
                "efficiency": efficiency, "peukert": peukert,
                "rated_hours": rated_hours,
                "cells_series": cells_s, "cells_parallel": cells_p},
        results=out,
        formulas=[
            "runtime = capacity*derate/(load/efficiency)",
            "Peukert: t = H*(C/(I*H))^k",
            "pack energy = V_cell*S * C*P",
        ],
        assumptions=[
            "constant load current; efficiency is the DC/DC conversion efficiency",
            "Peukert capacity is specified at the rated_hours rate (default 20 h)",
        ],
        notes=["battery capacity falls with temperature, age and high discharge rate; verify with the cell datasheet"],
    )


# ---------------------------------------------------------------------------
# Selection and tolerance
# ---------------------------------------------------------------------------

def calc_eseries(p: dict[str, Any]) -> dict[str, Any]:
    value = _pos(p.get("value"), "value")
    series = (p.get("series") or "E24").upper()
    direction = (p.get("direction") or "nearest").lower()
    pick = nearest_eseries(value, series, direction)
    return result(
        inputs={"value": value, "series": series, "direction": direction},
        results={
            "selected_value": pick["value"],
            "error_percent": pick["error_percent"],
            "series": pick["series"],
            "available_values": eseries_values(series)[:6] + ["..."],
        },
        formulas=["choose the E-series value nearest (or above/below) the target"],
        assumptions=["standard IEC 60063 E-series values"],
    )


def calc_tolerance(p: dict[str, Any]) -> dict[str, Any]:
    """Calculate first-order sensitivity and exact independent tolerance corners.

    Exact corner enumeration is deterministic and exhaustive for the supported
    scalar equations. It is not a substitute for nonlinear circuit simulation
    or correlated component/temperature models.
    """
    mode = (p.get("mode") or "divider").lower()
    comps = p.get("components") or {}
    if not isinstance(comps, dict) or not comps:
        raise CalcError("provide components as a JSON object: {'R1': {'value':..., 'tol':...}}")
    if len(comps) > 16:
        raise CalcError("exact corner analysis supports at most 16 variables (65,536 corners)")

    nominal: dict[str, float] = {}
    tol: dict[str, float] = {}
    for name, spec in comps.items():
        if isinstance(spec, (int, float)):
            value, tolerance = float(spec), 0.0
        elif isinstance(spec, dict) and "value" in spec:
            value = float(spec["value"])
            tolerance = float(spec.get("tol", spec.get("tol_percent", spec.get("tolerance_percent", 0.0)))) / 100.0
        else:
            raise CalcError(f"{name}: expected a number or an object with a value")
        if not math.isfinite(value) or not math.isfinite(tolerance):
            raise CalcError(f"{name}: value and tolerance must be finite")
        if tolerance < 0 or tolerance >= 1:
            raise CalcError(f"{name}: tolerance must be >= 0 and < 100 percent")
        nominal[name], tol[name] = value, tolerance

    def evaluate(values: dict[str, float]) -> float:
        if mode == "sum":
            return sum(values.values())
        if mode == "product":
            out = 1.0
            for v in values.values():
                out *= v
            return out
        if mode == "parallel":
            return parallel(*values.values())
        if mode == "divider":
            _need(values, "vin", "r1", "r2")
            if values["r1"] <= 0 or values["r2"] <= 0:
                raise CalcError("divider resistances must remain positive")
            return values["vin"] * values["r2"] / (values["r1"] + values["r2"])
        if mode == "rc_fc":
            _need(values, "r", "c")
            if values["r"] <= 0 or values["c"] <= 0:
                raise CalcError("R and C must remain positive")
            return 1.0 / (2 * math.pi * values["r"] * values["c"])
        if mode == "custom":
            coeffs = p.get("coefficients") or {}
            return sum(float(coeffs.get(k, 0.0)) * v for k, v in values.items())
        raise CalcError(f"unknown tolerance mode {mode!r}")

    base = evaluate(nominal)
    if not math.isfinite(base):
        raise CalcError("nominal result is not finite")

    sens: dict[str, float] = {}
    for name, value in nominal.items():
        delta = max(abs(value) * 1e-5, 1e-12)
        up = dict(nominal)
        up[name] = value + delta
        sens[name] = (evaluate(up) - base) / delta * value

    linear_worst = sum(abs(sens[k] * tol[k]) for k in nominal)
    rss = math.sqrt(sum((sens[k] * tol[k]) ** 2 for k in nominal))

    names = list(nominal)
    corner_results: list[tuple[float, dict[str, str]]] = []
    for signs in itertools.product((-1, 1), repeat=len(names)):
        corner = {
            name: nominal[name] * (1.0 + signs[i] * tol[name])
            for i, name in enumerate(names)
        }
        value = evaluate(corner)
        if not math.isfinite(value):
            raise CalcError("a tolerance corner produced a non-finite result")
        corner_results.append((value, {
            name: "min" if signs[i] < 0 else "max"
            for i, name in enumerate(names) if tol[name] > 0
        }))
    low = min(corner_results, key=lambda item: item[0])
    high = max(corner_results, key=lambda item: item[0])

    return result(
        inputs={"mode": mode, "components": comps},
        results={
            "nominal": base,
            "worst_case_absolute": max(abs(base - low[0]), abs(high[0] - base)),
            "worst_case_percent": max(abs(base - low[0]), abs(high[0] - base)) / abs(base) * 100 if base else None,
            "worst_case_range": [low[0], high[0]],
            "worst_case_corners": {"minimum": low[1], "maximum": high[1]},
            "corners_evaluated": len(corner_results),
            "linearized_worst_case_range": [base - linear_worst, base + linear_worst],
            "rss_absolute": rss,
            "rss_percent": rss / abs(base) * 100 if base else None,
            "rss_range": [base - rss, base + rss],
            "sensitivities": sens,
        },
        formulas=[
            "exact independent corners: evaluate all 2^N combinations of min/max component values",
            "linearized sensitivity S_i = dF/dx_i * x_i",
            "linearized worst case = sum |S_i|*tol_i",
            "RSS = sqrt(sum (S_i*tol_i)^2)",
        ],
        assumptions=[
            "component tolerances are independent and symmetric",
            "exact corner range applies only to the selected scalar equation and stated tolerances",
            "temperature coefficients, aging, voltage coefficients and correlations are excluded unless explicitly folded into the input limits",
        ],
        notes=[
            "Exact corners are deterministic for the supported equations; they are not Monte Carlo yield predictions",
            "RSS is a statistical estimate, not a guaranteed bound; use exact worst case for limit checks",
            "For nonlinear semiconductor circuits, run corner simulations with vendor models and justified model/temperature parameters",
        ],
    )


def calc_db(p: dict[str, Any]) -> dict[str, Any]:
    db = p.get("db")
    ratio = p.get("ratio")
    kind = (p.get("kind") or "voltage").lower()
    if kind not in ("voltage", "power"):
        raise CalcError("kind must be voltage or power")
    if db is None and ratio is None:
        raise CalcError("provide db or ratio")
    out: dict[str, Any] = {"kind": kind}
    if db is not None:
        db = float(db)
        out["db"] = db
        if kind == "voltage":
            out["ratio"] = 10 ** (db / 20)
            out["amplitude_ratio"] = out["ratio"]
            out["power_ratio"] = 10 ** (db / 10)
        else:
            out["ratio"] = 10 ** (db / 10)
            out["power_ratio"] = out["ratio"]
            out["amplitude_ratio"] = 10 ** (db / 20)
    else:
        ratio = _pos(ratio, "ratio")
        out["ratio"] = ratio
        out["db"] = 20 * math.log10(ratio) if kind == "voltage" else 10 * math.log10(ratio)
    ref = p.get("ref")
    if ref is not None and db is not None:
        out["dbm"] = db if ref == 1e-3 else None
        out["watts"] = 10 ** ((db - 30) / 10) if ref == 1e-3 else 10 ** (db / 10)
    return result(
        inputs={"db": db, "ratio": ratio, "kind": kind},
        results=out,
        formulas=[
            "voltage: dB = 20*log10(V2/V1)",
            "power: dB = 10*log10(P2/P1)",
            "dBm = 10*log10(P/1mW)",
        ],
        assumptions=["same impedance for voltage and power ratios unless stated"],
    )


def calc_noise(p: dict[str, Any]) -> dict[str, Any]:
    r = p.get("r")
    bandwidth = _pos(p.get("bandwidth", 1.0), "bandwidth")
    temp = float(p.get("temp", T_NOMINAL))
    if temp <= 0:
        raise CalcError("temp must be in kelvin and positive")
    out: dict[str, Any] = {"bandwidth_Hz": bandwidth, "temp_K": temp}
    terms: list[tuple[str, float]] = []
    if r is not None:
        r = _pos(r, "r")
        vn = math.sqrt(4 * K_BOLTZMANN * temp * r * bandwidth)
        out["resistor_thermal_noise_Vrms"] = vn
        terms.append(("resistor", vn))
    en = p.get("en")
    if en is not None:
        en = _pos(en, "en")
        vn = en * math.sqrt(bandwidth)
        out["amplifier_voltage_noise_Vrms"] = vn
        terms.append(("amplifier_voltage", vn))
    inn = p.get("in")
    if inn is not None:
        inn = _pos(inn, "in")
        if r is None:
            raise CalcError("current noise requires a source resistance r")
        vn = inn * r * math.sqrt(bandwidth)
        out["amplifier_current_noise_Vrms"] = vn
        terms.append(("amplifier_current", vn))
    shot = p.get("shot_current")
    if shot is not None:
        shot = _pos(shot, "shot_current")
        in_shot = math.sqrt(2 * Q_ELECTRON * shot * bandwidth)
        out["shot_noise_Arms"] = in_shot
        if r is not None:
            vn = in_shot * r
            out["shot_noise_Vrms"] = vn
            terms.append(("shot", vn))
    total = math.sqrt(sum(v * v for _, v in terms)) if terms else 0.0
    out["total_noise_Vrms"] = total
    if r is not None:
        out["noise_spectral_density_V_per_sqrtHz"] = total / math.sqrt(bandwidth)
    gain = p.get("gain")
    if gain is not None:
        out["output_noise_Vrms"] = total * abs(float(gain))
    return result(
        inputs={"r": r, "bandwidth": bandwidth, "temp": temp, "en": en, "in": inn,
                "shot_current": shot, "gain": gain},
        results=out,
        formulas=[
            "Vn,R = sqrt(4*k*T*R*BW)",
            "Vn,amp = en*sqrt(BW)",
            "Vn,current = in*R*sqrt(BW)",
            "In,shot = sqrt(2*q*I*BW); Vn,shot = In,shot*R",
            "total = sqrt(sum of squares)",
        ],
        assumptions=["white noise over the stated bandwidth; noise sources are uncorrelated"],
        notes=["1/f noise is not included; use the datasheet noise density at the relevant frequency"],
    )


def calc_current_mirror(p: dict[str, Any]) -> dict[str, Any]:
    iref = _pos(p.get("iref"), "iref")
    beta = _pos(p.get("beta", 100), "beta")
    va = p.get("va")
    ratio = float(p.get("ratio", 1.0))
    iout = iref * ratio / (1 + (1 + ratio) / beta)
    error = (iout - iref * ratio) / (iref * ratio) * 100
    out: dict[str, Any] = {
        "iout_A": iout, "ratio": ratio, "error_percent": error,
    }
    if va is not None:
        va = _pos(va, "va")
        out["output_resistance_ohm"] = va / iout
    return result(
        inputs={"iref": iref, "beta": beta, "va": va, "ratio": ratio},
        results=out,
        formulas=[
            "simple mirror: Iout = Iref*ratio/(1+(1+ratio)/beta)",
            "ro = VA/Ic",
        ],
        assumptions=["matched devices at the same temperature; active-region operation"],
        notes=["add emitter degeneration or a cascode for a higher output resistance"],
    )


def calc_crystal(p: dict[str, Any]) -> dict[str, Any]:
    cl = p.get("cl")
    c1, c2, cstray = p.get("c1"), p.get("c2"), p.get("cstray")
    f0 = p.get("f0")
    c0, cm = p.get("c0"), p.get("cm")
    if cl is None:
        if c1 is not None and c2 is not None:
            cstray = float(cstray or 0.0)
            cl = (c1 * c2) / (c1 + c2) + cstray
        else:
            raise CalcError("provide cl, or c1 and c2 (with optional cstray)")
    cl = _pos(cl, "cl")
    out: dict[str, Any] = {"cl_F": cl}
    if c1 is not None and c2 is not None:
        cstray = float(cstray or 0.0)
        cl_check = (c1 * c2) / (c1 + c2) + cstray
        out["cl_check_F"] = cl_check
    if f0 is not None and c0 is not None and cm is not None:
        f0 = _pos(f0, "f0")
        c0, cm = _pos(c0, "c0"), _pos(cm, "cm")
        pull = cm / (2 * (c0 + cl))
        out["frequency_pull_ppm"] = pull * 1e6
        out["frequency_with_cl_Hz"] = f0 * (1 + pull)
    return result(
        inputs={"cl": cl, "c1": c1, "c2": c2, "cstray": cstray, "f0": f0, "c0": c0, "cm": cm},
        results=out,
        formulas=[
            "CL = (C1*C2)/(C1+C2) + Cstray",
            "frequency pull ~ Cm/(2*(C0+CL))",
        ],
        assumptions=["crystal specified for the stated CL; stray capacitance includes pins and traces"],
        notes=["verify the crystal datasheet CL and the oscillator's negative resistance margin"],
    )


# ---------------------------------------------------------------------------
# Command registry
# ---------------------------------------------------------------------------

CommandFn = Callable[[dict[str, Any]], dict[str, Any]]

# kind: value | int | float | str | bool
COMMANDS: dict[str, dict[str, Any]] = {
    "ohms": {
        "fn": calc_ohms,
        "help": "Ohm's law: any two of V, I, R, P",
        "params": {
            "v": ("value", None, "voltage (V)"),
            "i": ("value", None, "current (A)"),
            "r": ("value", None, "resistance (ohm)"),
            "p": ("value", None, "power (W)"),
        },
    },
    "divider": {
        "fn": calc_divider,
        "help": "Resistive voltage divider, loading and Thevenin resistance",
        "params": {
            "vin": ("value", None, "input voltage (V)"),
            "r1": ("value", None, "top resistor (ohm)"),
            "r2": ("value", None, "bottom resistor (ohm)"),
            "vout": ("value", None, "target output voltage (solves a missing resistor)"),
            "rload": ("value", None, "load resistance (ohm)"),
        },
    },
    "rc": {
        "fn": calc_rc,
        "help": "RC low-pass/high-pass corner, time constant and response",
        "params": {
            "r": ("value", None, "resistance (ohm)"),
            "c": ("value", None, "capacitance (F)"),
            "fc": ("value", None, "target corner frequency (Hz)"),
            "f": ("value", None, "frequency to evaluate (Hz)"),
            "mode": ("str", "lowpass", "lowpass or highpass"),
        },
    },
    "rl": {
        "fn": calc_rl,
        "help": "RL low-pass/high-pass corner and response",
        "params": {
            "r": ("value", None, "resistance (ohm)"),
            "l": ("value", None, "inductance (H)"),
            "fc": ("value", None, "target corner frequency (Hz)"),
            "f": ("value", None, "frequency to evaluate (Hz)"),
            "mode": ("str", "lowpass", "lowpass or highpass"),
        },
    },
    "rlc": {
        "fn": calc_rlc,
        "help": "Series/parallel RLC resonance, Q, bandwidth and impedance",
        "params": {
            "r": ("value", None, "resistance (ohm)"),
            "l": ("value", None, "inductance (H)"),
            "c": ("value", None, "capacitance (F)"),
            "f0": ("value", None, "target resonant frequency (Hz)"),
            "f": ("value", None, "frequency to evaluate (Hz)"),
            "topology": ("str", "series", "series or parallel"),
        },
    },
    "sallen-key": {
        "fn": calc_sallen_key,
        "help": "Sallen-Key low-pass/high-pass design and analysis",
        "params": {
            "topology": ("str", "lowpass", "lowpass or highpass"),
            "fc": ("value", None, "corner frequency (Hz)"),
            "q": ("value", None, "target Q (0.7071 for Butterworth)"),
            "gain": ("float", 1.0, "non-inverting gain K"),
            "r1": ("value", None, "R1 (ohm, analysis mode)"),
            "r2": ("value", None, "R2 (ohm, analysis mode)"),
            "c1": ("value", None, "C1 (F, analysis or design mode)"),
            "c2": ("value", None, "C2 (F, design mode)"),
            "c": ("value", None, "C1=C2 for high-pass design (F)"),
        },
    },
    "bjt-bias": {
        "fn": calc_bjt_bias,
        "help": "BJT common-emitter voltage-divider bias and small signal",
        "params": {
            "vcc": ("value", None, "supply (V)"),
            "ic": ("value", None, "target collector current (A)"),
            "beta": ("float", 100.0, "DC current gain"),
            "vbe": ("float", 0.7, "base-emitter voltage (V)"),
            "re": ("value", None, "emitter resistor (ohm)"),
            "rc": ("value", None, "collector resistor (ohm)"),
            "vce": ("value", None, "target Vce (solves Rc if rc is absent)"),
            "stiffness": ("float", 10.0, "divider current / Ic"),
            "rload": ("value", None, "AC load (ohm)"),
            "bypassed": ("bool", False, "emitter resistor bypassed at signal frequency"),
        },
    },
    "mosfet-bias": {
        "fn": calc_mosfet_bias,
        "help": "MOSFET common-source bias and small signal",
        "params": {
            "vdd": ("value", None, "supply (V)"),
            "id": ("value", None, "drain current (A)"),
            "vth": ("float", 2.0, "threshold voltage (V)"),
            "kn": ("value", None, "transconductance parameter (A/V^2)"),
            "vov": ("value", None, "overdrive voltage (V)"),
            "rd": ("value", None, "drain resistor (ohm)"),
            "rs": ("value", None, "source resistor (ohm)"),
            "lambda": ("float", 0.0, "channel-length modulation (1/V)"),
            "rload": ("value", None, "AC load (ohm)"),
        },
    },
    "opamp": {
        "fn": calc_opamp,
        "help": "Op-amp gain, bandwidth, slew rate and headroom checks",
        "params": {
            "gain": ("float", None, "closed-loop gain"),
            "gbw": ("value", None, "gain-bandwidth product (Hz)"),
            "sr": ("value", None, "slew rate (V/s)"),
            "f": ("value", 1e3, "signal frequency (Hz)"),
            "vpeak": ("value", 1.0, "peak output voltage (V)"),
            "margin": ("float", 10.0, "required bandwidth margin"),
            "sr_margin": ("float", 2.0, "required slew-rate margin"),
            "rload": ("value", None, "load resistance (ohm)"),
            "supply": ("value", None, "single supply rail (V)"),
            "mode": ("str", "noninverting", "noninverting or inverting"),
            "rf": ("value", None, "feedback resistor (ohm)"),
            "rg": ("value", None, "ground resistor (ohm)"),
            "rin": ("value", None, "input resistor for inverting mode (ohm)"),
        },
    },
    "ldo": {
        "fn": calc_ldo,
        "help": "LDO dissipation, efficiency, dropout and junction temperature",
        "params": {
            "vin": ("value", None, "input voltage (V)"),
            "vout": ("value", None, "output voltage (V)"),
            "iout": ("value", None, "load current (A)"),
            "iq": ("value", 0.0, "quiescent current (A)"),
            "dropout": ("value", None, "dropout voltage (V)"),
            "theta_ja": ("value", None, "junction-to-ambient thermal resistance (C/W)"),
            "theta_jc": ("value", None, "junction-to-case (C/W)"),
            "theta_cs": ("value", None, "case-to-heatsink (C/W)"),
            "theta_sa": ("value", None, "heatsink-to-ambient (C/W)"),
            "ta": ("float", 25.0, "ambient temperature (C)"),
            "tj_max": ("float", 125.0, "maximum junction temperature (C)"),
        },
    },
    "buck": {
        "fn": calc_buck,
        "help": "Buck converter duty, inductor, capacitors and RMS currents",
        "params": {
            "vin": ("value", None, "input voltage (V)"),
            "vout": ("value", None, "output voltage (V)"),
            "iout": ("value", None, "output current (A)"),
            "fsw": ("value", None, "switching frequency (Hz)"),
            "ripple": ("float", 0.30, "inductor ripple ratio"),
            "vf": ("value", 0.0, "diode forward drop (V)"),
            "l": ("value", None, "selected inductance (H)"),
            "vout_ripple": ("value", 0.01, "output ripple target (V)"),
            "vds_rating": ("value", None, "switch voltage rating (V)"),
        },
    },
    "boost": {
        "fn": calc_boost,
        "help": "Boost converter duty, inductor, capacitors and RMS currents",
        "params": {
            "vin": ("value", None, "input voltage (V)"),
            "vout": ("value", None, "output voltage (V)"),
            "iout": ("value", None, "output current (A)"),
            "fsw": ("value", None, "switching frequency (Hz)"),
            "ripple": ("float", 0.30, "inductor ripple ratio"),
            "vf": ("value", 0.0, "diode forward drop (V)"),
            "l": ("value", None, "selected inductance (H)"),
            "vout_ripple": ("value", 0.01, "output ripple target (V)"),
        },
    },
    "led": {
        "fn": calc_led,
        "help": "LED series resistor and power",
        "params": {
            "vs": ("value", None, "supply voltage (V)"),
            "vf": ("value", None, "LED forward voltage (V)"),
            "n": ("int", 1, "number of LEDs in series"),
            "if": ("value", None, "target LED current (A)"),
            "r": ("value", None, "selected resistor (ohm)"),
            "r_rating": ("value", None, "resistor power rating (W)"),
        },
    },
    "zener": {
        "fn": calc_zener,
        "help": "Zener shunt regulator resistor and power",
        "params": {
            "vin_min": ("value", None, "minimum input voltage (V)"),
            "vin_max": ("value", None, "maximum input voltage (V)"),
            "vz": ("value", None, "zener voltage (V)"),
            "iz_min": ("value", None, "minimum zener current (A)"),
            "iz_max": ("value", None, "maximum zener current (A)"),
            "il_min": ("value", 0.0, "minimum load current (A)"),
            "il_max": ("value", 0.0, "maximum load current (A)"),
            "rs": ("value", None, "selected series resistor (ohm)"),
            "rz": ("value", None, "zener dynamic resistance (ohm)"),
        },
    },
    "i2c": {
        "fn": calc_i2c,
        "help": "I2C pull-up resistor range and rise time",
        "params": {
            "vdd": ("value", None, "bus supply (V)"),
            "vol": ("value", 0.4, "output low voltage (V)"),
            "iol": ("value", 3e-3, "output low sink current (A)"),
            "cb": ("value", None, "total bus capacitance (F)"),
            "tr": ("value", 300e-9, "rise time target (s)"),
            "rp": ("value", None, "selected pull-up (ohm)"),
        },
    },
    "adc": {
        "fn": calc_adc,
        "help": "ADC LSB, SNR, ENOB and oversampling",
        "params": {
            "bits": ("int", 12, "resolution (bits)"),
            "vref": ("value", None, "reference voltage (V)"),
            "vin": ("value", None, "input voltage to convert (V)"),
            "sinad": ("float", None, "measured SINAD (dB)"),
            "osr": ("int", 1, "oversampling ratio"),
            "single_ended": ("bool", True, "single-ended full scale = Vref"),
        },
    },
    "logic": {
        "fn": calc_logic,
        "help": "Logic noise margin, fanout, timing and dynamic power",
        "params": {
            "vdd": ("float", 3.3, "supply (V)"),
            "voh": ("float", None, "minimum output high (V)"),
            "vol": ("float", 0.0, "maximum output low (V)"),
            "vih": ("float", None, "minimum input high (V)"),
            "vil": ("float", None, "maximum input low (V)"),
            "ioh": ("value", None, "output high current (A)"),
            "iih": ("value", None, "input high current (A)"),
            "iol": ("value", None, "output low current (A)"),
            "iil": ("value", None, "input low current (A)"),
            "tpd": ("value", None, "propagation delay (s)"),
            "tcomb": ("value", None, "combinational delay (s)"),
            "tsu": ("value", None, "setup time (s)"),
            "th": ("value", None, "hold time (s)"),
            "tskew": ("value", None, "clock skew (s)"),
            "c_load": ("value", None, "load capacitance (F)"),
            "f": ("value", None, "switching frequency (Hz)"),
        },
    },
    "thermal": {
        "fn": calc_thermal,
        "help": "Junction temperature, heatsink sizing and derating",
        "params": {
            "p": ("value", None, "power dissipation (W)"),
            "ta": ("float", 25.0, "ambient temperature (C)"),
            "tj_max": ("float", 125.0, "maximum junction temperature (C)"),
            "theta_jc": ("value", None, "junction-to-case (C/W)"),
            "theta_cs": ("value", None, "case-to-heatsink (C/W)"),
            "theta_sa": ("value", None, "heatsink-to-ambient (C/W)"),
            "theta_ja": ("value", None, "junction-to-ambient (C/W)"),
            "derating": ("float", 0.80, "fraction of Tj(max) allowed"),
        },
    },
    "trace": {
        "fn": calc_trace,
        "help": "IPC-2221 PCB trace width or current capacity",
        "params": {
            "current": ("value", None, "current (A)"),
            "delta_t": ("value", None, "allowed temperature rise (C)"),
            "width_mil": ("value", None, "trace width (mil)"),
            "oz": ("float", 1.0, "copper weight (oz)"),
            "internal": ("bool", False, "internal layer"),
        },
    },
    "battery": {
        "fn": calc_battery,
        "help": "Battery runtime, pack configuration and energy",
        "params": {
            "capacity_mah": ("value", None, "cell capacity (mAh)"),
            "load_ma": ("value", None, "load current (mA)"),
            "derate": ("float", 0.80, "usable capacity fraction"),
            "efficiency": ("float", 1.0, "conversion efficiency"),
            "peukert": ("float", None, "Peukert exponent"),
            "rated_hours": ("value", 20.0, "hours at which capacity is rated (Peukert)"),
            "cells_series": ("int", 1, "series cell count"),
            "cells_parallel": ("int", 1, "parallel cell count"),
            "cell_voltage": ("float", 3.7, "nominal cell voltage (V)"),
        },
    },
    "eseries": {
        "fn": calc_eseries,
        "help": "Nearest E-series component value",
        "params": {
            "value": ("value", None, "target value"),
            "series": ("str", "E24", "E3/E6/E12/E24/E48/E96"),
            "direction": ("str", "nearest", "nearest/up/down"),
        },
    },
    "tolerance": {
        "fn": calc_tolerance,
        "help": "Worst-case and RSS tolerance analysis",
        "params": {
            "mode": ("str", "divider", "divider/sum/product/parallel/rc_fc/custom"),
            "components": ("json", None, "JSON object of {name: {value, tol_percent}}"),
            "coefficients": ("json", None, "coefficients for custom mode"),
        },
    },
    "db": {
        "fn": calc_db,
        "help": "dB, voltage/power ratio and dBm conversion",
        "params": {
            "db": ("float", None, "decibels"),
            "ratio": ("value", None, "linear ratio"),
            "kind": ("str", "voltage", "voltage or power"),
            "ref": ("float", None, "reference: 0.001 for dBm"),
        },
    },
    "noise": {
        "fn": calc_noise,
        "help": "Thermal, shot and amplifier noise RMS",
        "params": {
            "r": ("value", None, "source resistance (ohm)"),
            "bandwidth": ("value", 1.0, "noise bandwidth (Hz)"),
            "temp": ("float", 300.0, "temperature (K)"),
            "en": ("value", None, "voltage noise density (V/sqrt(Hz))"),
            "in": ("value", None, "current noise density (A/sqrt(Hz))"),
            "shot_current": ("value", None, "DC current for shot noise (A)"),
            "gain": ("float", None, "gain to output"),
        },
    },
    "current-mirror": {
        "fn": calc_current_mirror,
        "help": "BJT current mirror ratio, beta error and output resistance",
        "params": {
            "iref": ("value", None, "reference current (A)"),
            "beta": ("float", 100.0, "DC current gain"),
            "va": ("value", None, "Early voltage (V)"),
            "ratio": ("float", 1.0, "desired output/reference ratio"),
        },
    },
    "crystal": {
        "fn": calc_crystal,
        "help": "Crystal load capacitance and frequency pulling",
        "params": {
            "cl": ("value", None, "specified load capacitance (F)"),
            "c1": ("value", None, "load capacitor 1 (F)"),
            "c2": ("value", None, "load capacitor 2 (F)"),
            "cstray": ("value", None, "stray capacitance (F)"),
            "f0": ("value", None, "nominal frequency (Hz)"),
            "c0": ("value", None, "shunt capacitance (F)"),
            "cm": ("value", None, "motional capacitance (F)"),
        },
    },
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _coerce(kind: str, raw: Any) -> Any:
    if raw is None:
        return None
    if kind == "value":
        return parse_value(raw)[0]
    if kind == "float":
        return float(raw)
    if kind == "int":
        return int(raw)
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).lower() in ("1", "true", "yes", "on")
    if kind == "json":
        if isinstance(raw, (dict, list)):
            return raw
        return json.loads(raw)
    return str(raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="circuit_calc.py",
        description="Dependency-free circuit calculations (SPICE suffix rules).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 circuit_calc.py divider --vin 5 --r1 10k --r2 4.7k\n"
            "  python3 circuit_calc.py rc --r 1k --c 100n --f 10k --mode lowpass\n"
            "  python3 circuit_calc.py sallen-key --topology lowpass --fc 1k --q 0.7071\n"
            "  python3 circuit_calc.py list\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    list_parser = sub.add_parser("list", help="list all calculation commands")
    list_parser.add_argument("--json", action="store_true", help="machine-readable output")

    for name, spec in COMMANDS.items():
        sp = sub.add_parser(name, help=spec["help"], description=spec["help"])
        sp.add_argument(
            "--args-json",
            metavar="JSON|@FILE",
            help="JSON object of parameters (merged with flags; flags win)",
        )
        sp.add_argument("--json", action="store_true", help="machine-readable output")
        for pname, (kind, default, help_text) in spec["params"].items():
            flag = "--" + pname.replace("_", "-")
            if kind == "bool":
                sp.add_argument(flag, dest=pname, action="store_true", default=None,
                                help=help_text)
                sp.add_argument("--no-" + pname.replace("_", "-"), dest=pname,
                                action="store_false", help=f"disable {pname}")
            elif kind == "json":
                sp.add_argument(flag, dest=pname, metavar="JSON", default=None, help=help_text)
            else:
                sp.add_argument(flag, dest=pname, default=None,
                                metavar="VALUE", help=help_text)
    return parser


def _load_args_json(text: str) -> dict[str, Any]:
    if text.startswith("@"):
        return json.loads(Path(text[1:]).read_text(encoding="utf-8"))
    return json.loads(text)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)
    if not ns.command:
        parser.print_help()
        return 2
    if ns.command == "list":
        if ns.json:
            print(json.dumps({k: v["help"] for k, v in COMMANDS.items()}, indent=2))
        else:
            width = max(len(k) for k in COMMANDS)
            for name, spec in COMMANDS.items():
                print(f"{name:<{width}}  {spec['help']}")
        return 0

    spec = COMMANDS[ns.command]
    raw: dict[str, Any] = {}
    if getattr(ns, "args_json", None):
        try:
            raw.update(_load_args_json(ns.args_json))
        except (OSError, json.JSONDecodeError) as exc:
            emit(fail(f"cannot read --args-json: {exc}"), as_json=ns.json)
            return 2
    for pname, (kind, default, _help) in spec["params"].items():
        value = getattr(ns, pname, None)
        if value is not None:
            raw[pname] = value
        elif pname not in raw and default is not None:
            raw[pname] = default
    params: dict[str, Any] = {}
    warnings: list[str] = []
    try:
        for pname, (kind, _default, _help) in spec["params"].items():
            if pname not in raw:
                continue
            if kind == "value":
                val, warns = parse_value(raw[pname], name=pname)
                params[pname] = val
                warnings.extend(warns)
            else:
                params[pname] = _coerce(kind, raw[pname])
    except (ValueParseError, ValueError, TypeError, json.JSONDecodeError) as exc:
        emit(fail(f"invalid parameter: {exc}"), as_json=ns.json)
        return 2

    try:
        out = spec["fn"](params)
    except (CalcError, ValueError, ZeroDivisionError) as exc:
        out = fail(str(exc), warnings=warnings)
    except Exception as exc:  # pragma: no cover - defensive
        out = fail(f"internal error: {exc}", warnings=warnings)
    if warnings:
        out.setdefault("warnings", []).extend(warnings)
    emit(out, as_json=ns.json)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
