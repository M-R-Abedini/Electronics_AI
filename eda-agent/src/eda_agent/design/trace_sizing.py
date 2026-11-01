# Modified 2026-11-01 by M-R-Abedini; see root MODIFICATIONS.md.
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""PCB trace sizing from current (legacy IPC-2221 and qualified IPC-2152 data).

The forward question -- "how much current does this track carry?" -- is
answered by ``pcb_calc_track_current_capacity``. The design-time question is
the INVERSE: "I need to carry 3 A; how WIDE must the track be?" Forcing the
planner to binary-search the forward tool is error prone, so this module
solves the closed form directly.

IPC-2221 (the curve-fit that IPC-2152 refines) gives the temperature rise of
a track carrying a current ``I``::

    I = k * dT^0.44 * (h * w)^0.725

with ``h`` (copper thickness) and ``w`` (width) in mils, ``dT`` in degC, and
``k = 0.048`` external (top/bottom) or ``0.024`` internal (buried, where heat
escapes more slowly so a track must be ~2.6x wider for the same rise). Solving
for width::

    w = (I / (k * dT^0.44))^(1/0.725) / h

Copper weight converts as ``h = copper_oz * 1.378 mils`` (1 oz/ft^2). The same
constants as the forward tool, so the two round-trip exactly. Pure math, no
Altium.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

_K_EXTERNAL = 0.048
_K_INTERNAL = 0.024
_DT_EXP = 0.44            # temperature-rise exponent
_AREA_EXP = 0.725        # cross-section exponent
from ..units import OZ_TO_MILS as _OZ_TO_MILS  # 1 oz/ft^2 in mils
_RHO_OHM_MIL = 6.7e-7    # annealed copper resistivity, ohm-mil, 25 degC


def copper_thickness_mils(copper_oz: float) -> float:
    """Copper foil thickness in mils for a weight in oz/ft^2."""
    if copper_oz <= 0:
        raise ValueError("copper_oz must be positive")
    return copper_oz * _OZ_TO_MILS


def _k_for_layer(layer: str) -> float:
    norm = layer.strip().lower()
    if norm == "external":
        return _K_EXTERNAL
    if norm == "internal":
        return _K_INTERNAL
    raise ValueError("layer must be 'external' or 'internal'")


def current_capacity_amps(
    width_mils: float,
    *,
    copper_oz: float = 1.0,
    delta_t_c: float = 10.0,
    layer: str = "external",
) -> float:
    """Forward IPC-2221: current a track sustains at ``delta_t_c`` rise. Kept
    here so tests can round-trip against :func:`trace_width_for_current`."""
    if width_mils <= 0:
        raise ValueError("width_mils must be positive")
    if delta_t_c <= 0:
        raise ValueError("delta_t_c must be positive")
    h = copper_thickness_mils(copper_oz)
    return _k_for_layer(layer) * (delta_t_c ** _DT_EXP) * \
        ((h * width_mils) ** _AREA_EXP)


def trace_resistance_mohm(
    width_mils: float, copper_oz: float, length_mils: float
) -> float:
    """DC resistance of a track in milliohms, ``R = rho * L / (h * w)``."""
    if width_mils <= 0 or length_mils < 0:
        raise ValueError("width_mils > 0 and length_mils >= 0 required")
    h = copper_thickness_mils(copper_oz)
    return _RHO_OHM_MIL * length_mils / (h * width_mils) * 1000.0


@dataclass(frozen=True)
class TraceWidthResult:
    """Minimum and recommended track width for a target current."""

    current_a: float
    min_width_mils: float          # exact IPC-2221 minimum
    recommended_width_mils: float  # min * (1 + margin), rounded up to 0.1 mil
    copper_oz: float
    delta_t_c: float
    layer: str
    resistance_mohm: Optional[float] = None   # at recommended width, if length
    voltage_drop_mv: Optional[float] = None


def trace_width_for_current(
    current_a: float,
    *,
    copper_oz: float = 1.0,
    delta_t_c: float = 10.0,
    layer: str = "external",
    margin: float = 0.2,
    length_mils: float = 0.0,
) -> TraceWidthResult:
    """Minimum track width (mils) to carry ``current_a`` at ``delta_t_c`` rise.

    ``margin`` (default 0.2 = 20 %) widens the recommendation above the bare
    IPC minimum, rounded up to a 0.1 mil grid; manufacturers and real-world
    derating make a margin prudent. With ``length_mils`` set, the resistance
    and voltage drop at the RECOMMENDED width are also returned.
    """
    if current_a <= 0:
        raise ValueError("current_a must be positive")
    if delta_t_c <= 0:
        raise ValueError("delta_t_c must be positive")
    if margin < 0:
        raise ValueError("margin must be >= 0")
    h = copper_thickness_mils(copper_oz)
    k = _k_for_layer(layer)
    # h*w = (I / (k * dT^b))^(1/c)
    area = (current_a / (k * (delta_t_c ** _DT_EXP))) ** (1.0 / _AREA_EXP)
    min_w = area / h
    rec_w = math.ceil(min_w * (1.0 + margin) * 10.0) / 10.0
    res = None
    vdrop = None
    if length_mils > 0:
        res = trace_resistance_mohm(rec_w, copper_oz, length_mils)
        vdrop = current_a * res        # mV (res is mOhm, current in A)
    return TraceWidthResult(
        current_a=current_a, min_width_mils=min_w,
        recommended_width_mils=rec_w, copper_oz=copper_oz,
        delta_t_c=delta_t_c, layer=layer.strip().lower(),
        resistance_mohm=res, voltage_drop_mv=vdrop)


__all__ = [
    "TraceWidthResult",
    "copper_thickness_mils",
    "current_capacity_amps",
    "trace_resistance_mohm",
    "trace_width_for_current",
    "trace_width_for_current_qualified",
]


def trace_width_for_current_qualified(
    current_a: float, *, dataset: dict | None = None, copper_oz: float,
    delta_t_c: float, layer: str, margin: float = 0.2,
) -> dict:
    """Conservative, bounded interpolation of explicitly qualified IPC-2152 data.

    No IPC-2152 charts are bundled or inferred from IPC-2221. ``dataset`` must
    identify its source, qualification, conditions, and points. Each point is
    {current_a,width_mils}; conditions must exactly match copper_oz, delta_t_c,
    and layer. The integrator is responsible for qualifying the table against
    the actual board construction/environment; this is not IPC certification.
    Linear width interpolation is combined with the upper bracketing width
    (conservative step envelope), never extrapolation. See IPC's primary paper:
    https://www.ipc.org/system/files/technical_resource/E7%26S22_03.pdf
    """
    def positive(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0
    if not all(positive(v) for v in (current_a, copper_oz, delta_t_c)) or not math.isfinite(margin) or margin < 0:
        raise ValueError("finite positive current/copper/delta_t and nonnegative margin required")
    _k_for_layer(layer)
    unsupported = {"supported": False, "model": "IPC-2152-qualified-table",
                   "certified": False}
    if not dataset:
        return {**unsupported, "reason": "qualified IPC-2152 calibration dataset required"}
    if (dataset.get("standard") != "IPC-2152" or dataset.get("qualified") is not True
            or not dataset.get("source") or not dataset.get("qualification")
            or not dataset.get("board_conditions")):
        return {**unsupported, "reason": "dataset requires IPC-2152 source, qualification and board_conditions"}
    conditions = dataset.get("conditions", {})
    requested = {"copper_oz": copper_oz, "delta_t_c": delta_t_c, "layer": layer.strip().lower()}
    if conditions != requested:
        return {**unsupported, "reason": "dataset conditions do not match requested construction/temperature/layer"}
    points = dataset.get("points", [])
    if len(points) < 2 or any(not isinstance(p, dict) or not positive(p.get("current_a")) or not positive(p.get("width_mils")) for p in points):
        raise ValueError("dataset needs at least two finite positive current/width points")
    points = sorted(points, key=lambda p: p["current_a"])
    if any(b["current_a"] <= a["current_a"] or b["width_mils"] < a["width_mils"] for a, b in zip(points, points[1:])):
        raise ValueError("dataset current must be unique and width monotonic")
    if not points[0]["current_a"] <= current_a <= points[-1]["current_a"]:
        return {**unsupported, "reason": "current outside qualified data range; extrapolation prohibited"}
    for a, b in zip(points, points[1:]):
        if a["current_a"] <= current_a <= b["current_a"]:
            fraction = (current_a-a["current_a"])/(b["current_a"]-a["current_a"])
            interpolated = a["width_mils"] + fraction * (b["width_mils"]-a["width_mils"])
            width = a["width_mils"] if fraction == 0 else b["width_mils"]
            return {"supported": True, "model": "IPC-2152-qualified-table", "certified": False,
                    "min_width_mils": width, "interpolated_width_mils": interpolated,
                    "recommended_width_mils": math.ceil(width*(1+margin)*10)/10,
                    "method": "upper-bracket conservative envelope; no extrapolation",
                    "provenance": {key: dataset[key] for key in ("source", "qualification", "board_conditions", "conditions")}}
