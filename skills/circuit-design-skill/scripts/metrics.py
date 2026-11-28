"""Evaluate generic electrical metrics from ngspice results.

The audio skill supplies targets and conditions; this module performs the
generic extraction that both audio and non-audio designs can use.  It does not
decide whether a circuit sounds good, and it does not replace a measurement.

Supported measures:

``gain_db``
    AC gain at one frequency, ``20 log10(|Vout| / |Vin|)``.
``bandwidth_hz`` / ``bandwidth_low_hz`` / ``bandwidth_width_hz``
    -3 dB corners around the dominant passband peak, and their difference.
    For multi-band responses, the dominant (highest-gain) passband is selected.
``thd_percent``
    Total harmonic distortion reported by ngspice's ``.four`` analysis.
``ein_uv`` / ``noise_uv``
    Input- or output-referred RMS noise integrated over the noise-analysis
    sweep, in microvolts.
``output_vrms``
    RMS value of a transient output over a stated or default window.

All values are returned with their conditions in the result record.  A metric
without a target, limit or ``exploratory`` flag is a validation error, not a
silent pass.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from ngspice_raw import Plot, find_plot

SUPPORTED = {
    "gain_db",
    "bandwidth_hz",
    "bandwidth_low_hz",
    "bandwidth_width_hz",
    "thd_percent",
    "ein_uv",
    "noise_uv",
    "output_vrms",
}

ANALYSIS_FOR = {
    "gain_db": "ac",
    "bandwidth_hz": "ac",
    "bandwidth_low_hz": "ac",
    "bandwidth_width_hz": "ac",
    "thd_percent": "fourier",
    "ein_uv": "noise",
    "noise_uv": "noise",
    "output_vrms": "tran",
}


@dataclass
class MetricResult:
    metric: dict[str, Any]
    value: float
    unit: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.metric.get("id"),
            "measure": self.metric.get("measure"),
            "value": self.value,
            "unit": self.unit,
            "detail": self.detail,
            "requirement": self.metric.get("requirement"),
        }


def _name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("a variable name is required")
    text = value.strip()
    if "(" not in text:
        return f"v({text})"
    return text


def _magnitude(value: float | complex) -> float:
    return abs(complex(value))


def _gain_curve(plot: Plot, metric: dict[str, Any]) -> tuple[list[float], list[float]]:
    xs = plot.scale()
    output = _name(metric.get("output"))
    source = _name(metric.get("input"))
    vout = plot.column(output)
    vin = plot.column(source)
    if not (len(xs) == len(vout) == len(vin)):
        raise ValueError("AC plot has inconsistent column lengths")
    gains: list[float] = []
    for vo, vi in zip(vout, vin):
        denominator = _magnitude(vi)
        if denominator == 0:
            gains.append(float("-inf"))
        else:
            gains.append(20.0 * math.log10(_magnitude(vo) / denominator))
    return xs, gains


def _nearest(xs: list[float], target: float) -> int:
    if not xs:
        raise ValueError("plot has no points")
    return min(range(len(xs)), key=lambda index: abs(xs[index] - target))


def _interpolate_corner(x0: float, g0: float, x1: float, g1: float,
                        threshold: float) -> float:
    if x0 <= 0 or x1 <= 0:
        if g1 == g0:
            return x0
        t = (threshold - g0) / (g1 - g0)
        return x0 + max(0.0, min(1.0, t)) * (x1 - x0)
    if g1 == g0:
        return x0
    t = (threshold - g0) / (g1 - g0)
    t = max(0.0, min(1.0, t))
    return 10.0 ** (math.log10(x0) + t * (math.log10(x1) - math.log10(x0)))


def _gain_db(metric: dict[str, Any], plots: dict[str, Plot]) -> tuple[float, str, str]:
    plot = find_plot(plots, "ac")
    if plot is None:
        raise ValueError("no AC analysis plot; add an 'ac' analysis")
    xs, gains = _gain_curve(plot, metric)
    frequency = float(metric.get("frequency", 1000.0))
    if frequency <= 0:
        raise ValueError("gain frequency must be positive")
    index = _nearest(xs, frequency)
    if xs[index] <= 0 or abs(math.log10(xs[index] / frequency)) > 0.02:
        raise ValueError(
            f"nearest AC point is {xs[index]:g} Hz, not within 5% of "
            f"{frequency:g} Hz; add a sweep point or increase points")
    return gains[index], "dB", f"at {xs[index]:.6g} Hz"


def _bandwidth(metric: dict[str, Any], plots: dict[str, Plot],
               mode: str) -> tuple[float, str, str]:
    plot = find_plot(plots, "ac")
    if plot is None:
        raise ValueError("no AC analysis plot; add an 'ac' analysis")
    xs, gains = _gain_curve(plot, metric)
    if len(xs) != len(gains) or len(xs) < 2:
        raise ValueError("AC sweep has too few points")
    if any(not math.isfinite(x) or x <= 0 for x in xs):
        raise ValueError("AC sweep frequencies must be finite and positive")
    if any(xs[i] >= xs[i + 1] for i in range(len(xs) - 1)):
        raise ValueError("AC sweep frequency axis must be strictly ascending")
    finite_indices = [i for i, gain in enumerate(gains) if math.isfinite(gain)]
    if not finite_indices:
        raise ValueError("AC gain is not finite at any point")

    peak_index = max(finite_indices, key=lambda i: gains[i])
    peak_gain = gains[peak_index]
    threshold = peak_gain - 3.0

    # Find the nearest -3 dB crossing on each side of the dominant peak.
    # Taking the first/last crossing across the entire sweep can incorrectly
    # combine separate passbands into one enormous bandwidth.
    lower: float | None = None
    for index in range(peak_index - 1, -1, -1):
        if gains[index] < threshold <= gains[index + 1]:
            lower = _interpolate_corner(
                xs[index], gains[index], xs[index + 1], gains[index + 1],
                threshold)
            break

    upper: float | None = None
    for index in range(peak_index, len(xs) - 1):
        if gains[index] >= threshold > gains[index + 1]:
            upper = _interpolate_corner(
                xs[index], gains[index], xs[index + 1], gains[index + 1],
                threshold)
            break

    peak_detail = f"dominant peak {xs[peak_index]:.6g} Hz at {peak_gain:.3f} dB"
    if mode == "upper":
        if upper is None:
            raise ValueError(
                "dominant passband has no -3 dB upper corner; extend stop")
        return upper, "Hz", f"-3 dB; {peak_detail}"
    if mode == "lower":
        if lower is None:
            raise ValueError(
                "dominant passband has no -3 dB lower corner; extend start")
        return lower, "Hz", f"-3 dB; {peak_detail}"
    if lower is None or upper is None:
        raise ValueError(
            "dominant passband does not contain both -3 dB corners")
    return upper - lower, "Hz", (
        f"dominant passband {lower:.6g}..{upper:.6g} Hz; {peak_detail}")


def _thd(metric: dict[str, Any], log_text: str) -> tuple[float, str, str]:
    output = _name(metric.get("output")).lower()
    # ngspice can contain several .four blocks.  Match the one for this output
    # instead of taking the first THD line in the log.
    text = log_text
    for block in re.split(r"(?=Fourier analysis for\s+)", log_text,
                          flags=re.IGNORECASE):
        first = block.strip().splitlines()[0].strip() if block.strip() else ""
        if first.lower().startswith("fourier analysis for"):
            target = first.split("for", 1)[1].strip().rstrip(":").lower()
            if target == output:
                text = block
                break
    patterns = (
        r"Total harmonic distortion\s*[:=]\s*([0-9.eE+-]+)\s*(?:%|percent)",
        r"\bTHD\s*[:=]\s*([0-9.eE+-]+)\s*(?:%|percent)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return float(match.group(1)), "%", "reported by ngspice .four"
    raise ValueError(
        "no THD line in the ngspice log; add a 'fourier' analysis and a "
        "transient sine source")


def _noise_value(metric: dict[str, Any], plots: dict[str, Plot],
                 variable: str) -> float:
    plot = find_plot(plots, "noise")
    if plot is None:
        raise ValueError("no noise analysis plot; add a 'noise' analysis")
    names = [name.lower() for name in plot.variables]
    # Some ngspice versions expose a scalar total alongside the spectrum.
    candidates = (
        variable.replace("_spectrum", "_total"),
        f"{variable}_total",
        variable,
    )
    for candidate in candidates:
        if candidate in names:
            values = plot.column(candidate)
            if len(values) == 1:
                return _magnitude(values[0])
            xs = plot.scale()
            ys = [_magnitude(value) for value in values]
            if len(xs) < 2:
                raise ValueError(f"{candidate} has too few points to integrate")
            total = 0.0
            for index in range(len(xs) - 1):
                delta = xs[index + 1] - xs[index]
                if delta < 0:
                    raise ValueError(f"{candidate} frequency axis is not ascending")
                total += 0.5 * (ys[index] ** 2 + ys[index + 1] ** 2) * delta
            return math.sqrt(total)
    raise ValueError(
        f"noise plot has no {variable!r} variable; available: "
        + ", ".join(plot.variables))


def _ein(metric: dict[str, Any], plots: dict[str, Plot]) -> tuple[float, str, str]:
    value = _noise_value(metric, plots, "inoise_spectrum")
    return value * 1e6, "uV", "input-referred RMS over the noise sweep"


def _noise(metric: dict[str, Any], plots: dict[str, Plot]) -> tuple[float, str, str]:
    value = _noise_value(metric, plots, "onoise_spectrum")
    return value * 1e6, "uV", "output-referred RMS over the noise sweep"


def _output_vrms(metric: dict[str, Any],
                 plots: dict[str, Plot]) -> tuple[float, str, str]:
    plot = find_plot(plots, "tran")
    if plot is None:
        raise ValueError("no transient analysis plot; add a 'tran' analysis")
    xs = plot.scale()
    ys = [_magnitude(value) for value in plot.column(_name(metric.get("output")))]
    if len(xs) < 2:
        raise ValueError("transient plot has too few points")
    start = metric.get("start")
    stop = metric.get("stop")
    if start is None:
        start = xs[0] + 0.75 * (xs[-1] - xs[0])
    if stop is None:
        stop = xs[-1]
    start, stop = float(start), float(stop)
    if not (xs[0] <= start < stop <= xs[-1]):
        raise ValueError(
            f"window {start:g}..{stop:g} s is outside the transient sweep "
            f"{xs[0]:g}..{xs[-1]:g} s")
    points = [(x, y) for x, y in zip(xs, ys) if start <= x <= stop]
    if len(points) < 2:
        raise ValueError("window contains too few transient points")
    area = 0.0
    for index in range(len(points) - 1):
        dt = points[index + 1][0] - points[index][0]
        area += 0.5 * (points[index][1] ** 2 + points[index + 1][1] ** 2) * dt
    duration = points[-1][0] - points[0][0]
    if duration <= 0:
        raise ValueError("transient window has zero duration")
    return math.sqrt(area / duration), "V", (
        f"RMS over {start:g}..{stop:g} s")


def evaluate(block: dict[str, Any], plots: dict[str, Plot],
             log_text: str) -> tuple[list[MetricResult], list[str]]:
    """Evaluate every metric in a ``metrics`` block.

    Returns the successful results and a list of per-metric error strings.  A
    missing plot, missing variable or unparseable log line is an error for that
    metric, never a silent zero.
    """
    results: list[MetricResult] = []
    errors: list[str] = []
    items = block.get("metrics") if isinstance(block, dict) else None
    if not isinstance(items, list) or not items:
        return results, ["metrics.metrics is missing or empty"]
    for index, metric in enumerate(items):
        if not isinstance(metric, dict):
            errors.append(f"metrics[{index}] is not an object")
            continue
        measure = metric.get("measure")
        label = metric.get("id") or f"metrics[{index}]"
        try:
            if measure == "gain_db":
                value, unit, detail = _gain_db(metric, plots)
            elif measure == "bandwidth_hz":
                value, unit, detail = _bandwidth(metric, plots, "upper")
            elif measure == "bandwidth_low_hz":
                value, unit, detail = _bandwidth(metric, plots, "lower")
            elif measure == "bandwidth_width_hz":
                value, unit, detail = _bandwidth(metric, plots, "width")
            elif measure == "thd_percent":
                value, unit, detail = _thd(metric, log_text)
            elif measure == "ein_uv":
                value, unit, detail = _ein(metric, plots)
            elif measure == "noise_uv":
                value, unit, detail = _noise(metric, plots)
            elif measure == "output_vrms":
                value, unit, detail = _output_vrms(metric, plots)
            else:
                raise ValueError(f"unsupported measure {measure!r}")
        except (KeyError, ValueError, ZeroDivisionError) as exc:
            errors.append(f"{label}: {exc}")
            continue
        results.append(MetricResult(metric, value, unit, detail))
    return results, errors


def compare(metric: dict[str, Any], value: float) -> tuple[bool | None, str]:
    """Apply a metric's target or limit.  ``None`` means informational only."""
    if metric.get("exploratory"):
        return None, "exploratory"
    if "target" in metric:
        target = float(metric["target"])
        tolerance = float(metric.get("tolerance", 0.0))
        return (abs(value - target) <= tolerance,
                f"target {target:g} +/- {tolerance:g}")
    if "max" in metric:
        limit = float(metric["max"])
        return value <= limit, f"max {limit:g}"
    if "min" in metric:
        limit = float(metric["min"])
        return value >= limit, f"min {limit:g}"
    return None, "no target or limit"
