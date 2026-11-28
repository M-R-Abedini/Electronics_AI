#!/usr/bin/env python3
"""Discrete E-series optimizer for a first-order RC low-pass design.

This is an analytical pre-sizing tool, not a replacement for loaded-circuit
analysis, SPICE corner simulation, or datasheet operating-limit checks.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from circuit_common import ValueParseError, eseries_values, parse_value


def _value(raw: str | float, name: str) -> float:
    value, _warnings = parse_value(raw, name=name)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive value")
    return value


def _series_candidates(series: str, lower: float, upper: float) -> list[float]:
    if lower <= 0 or upper < lower:
        raise ValueError("candidate range must be positive and min <= max")
    base = eseries_values(series)
    lo = math.floor(math.log10(lower)) - 1
    hi = math.ceil(math.log10(upper)) + 1
    values = sorted({
        value * (10 ** decade)
        for decade in range(lo, hi + 1)
        for value in base
        if lower <= value * (10 ** decade) <= upper
    })
    if not values:
        raise ValueError(f"no {series} values in range [{lower:g}, {upper:g}]")
    return values


def optimize_rc_lowpass(
    spec: dict[str, Any],
    *,
    target_fc_hz: float,
    r_min: float = 1e3,
    r_max: float = 1e6,
    c_min: float = 1e-9,
    c_max: float = 1e-6,
    r_series: str = "E24",
    c_series: str = "E12",
    r_tolerance_percent: float = 1.0,
    c_tolerance_percent: float = 10.0,
    max_error_percent: float = 5.0,
    top_n: int = 20,
) -> dict[str, Any]:
    """Rank E-series R/C pairs by their exact independent tolerance-corner error."""
    if spec.get("topology") != "rc_lowpass":
        raise ValueError("optimizer currently supports topology 'rc_lowpass' only")
    if target_fc_hz <= 0 or not math.isfinite(target_fc_hz):
        raise ValueError("target_fc_hz must be finite and positive")
    if not (0 <= r_tolerance_percent < 100 and 0 <= c_tolerance_percent < 100):
        raise ValueError("component tolerances must be >= 0 and < 100 percent")
    if max_error_percent < 0 or top_n < 1:
        raise ValueError("max_error_percent must be >= 0 and top_n must be >= 1")

    resistors = [part for part in spec.get("components", []) if part.get("type") == "R"]
    capacitors = [part for part in spec.get("components", []) if part.get("type") == "C"]
    if not resistors or not capacitors:
        raise ValueError("spec must contain at least one resistor and one capacitor")
    r_values = _series_candidates(r_series, r_min, r_max)
    c_values = _series_candidates(c_series, c_min, c_max)

    candidates: list[dict[str, Any]] = []
    for resistance in r_values:
        for capacitance in c_values:
            nominal = 1.0 / (2.0 * math.pi * resistance * capacitance)
            low = 1.0 / (
                2.0 * math.pi * resistance * (1 + r_tolerance_percent / 100)
                * capacitance * (1 + c_tolerance_percent / 100)
            )
            high = 1.0 / (
                2.0 * math.pi * resistance * (1 - r_tolerance_percent / 100)
                * capacitance * (1 - c_tolerance_percent / 100)
            )
            worst_error = max(abs(low - target_fc_hz), abs(high - target_fc_hz)) / target_fc_hz * 100
            candidates.append({
                "r_ohm": resistance,
                "c_f": capacitance,
                "fc_nominal_hz": nominal,
                "fc_min_hz": low,
                "fc_max_hz": high,
                "worst_case_error_percent": worst_error,
                "nominal_error_percent": (nominal - target_fc_hz) / target_fc_hz * 100,
                "meets_error_limit": worst_error <= max_error_percent,
            })

    candidates.sort(key=lambda item: (
        item["worst_case_error_percent"],
        abs(item["nominal_error_percent"]),
        item["r_ohm"],
    ))
    best = candidates[0]
    optimized = copy.deepcopy(spec)
    optimized.setdefault("parameters", {})["r"] = best["r_ohm"]
    optimized["parameters"]["c"] = best["c_f"]
    optimized_resistors = [part for part in optimized.get("components", []) if part.get("type") == "R"]
    optimized_capacitors = [part for part in optimized.get("components", []) if part.get("type") == "C"]
    optimized_resistors[0]["value"] = best["r_ohm"]
    optimized_resistors[0]["tolerance_percent"] = r_tolerance_percent
    optimized_capacitors[0]["value"] = best["c_f"]
    optimized_capacitors[0]["tolerance_percent"] = c_tolerance_percent
    assumptions = optimized.setdefault("assumptions", [])
    assumption = (
        "RC optimization assumes an ideal zero-ohm source and an unloaded output; "
        "include actual source/load impedance before accepting the design."
    )
    if assumption not in assumptions:
        assumptions.append(assumption)
    optimized.setdefault("design", {}).setdefault("optimization", {}).update({
        "method": "exhaustive E-series search with exact independent R/C tolerance corners",
        "target_fc_hz": target_fc_hz,
        "r_series": r_series.upper(),
        "c_series": c_series.upper(),
        "max_error_percent": max_error_percent,
        "best_candidate": best,
        "candidates_evaluated": len(candidates),
    })
    return {
        "ok": bool(best["meets_error_limit"]),
        "target_fc_hz": target_fc_hz,
        "candidates_evaluated": len(candidates),
        "best": best,
        "top_candidates": candidates[:top_n],
        "optimized_spec": optimized,
        "assumptions": [assumption],
        "limitations": [
            "Ideal source and unloaded output assumed; source/load impedance changes the transfer function.",
            "Temperature drift, aging, voltage coefficient, parasitics and correlated tolerances are not modeled.",
            "The result is an analytical candidate; run circuit simulation and datasheet limit checks before release.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path, help="existing rc_lowpass design spec")
    parser.add_argument("--target-fc", required=True, help="target cutoff frequency, e.g. 1.59k")
    parser.add_argument("--out-spec", required=True, type=Path, help="path for optimized design spec")
    parser.add_argument("--r-min", default="1k")
    parser.add_argument("--r-max", default="1meg")
    parser.add_argument("--c-min", default="1n")
    parser.add_argument("--c-max", default="1u")
    parser.add_argument("--r-series", default="E24")
    parser.add_argument("--c-series", default="E12")
    parser.add_argument("--r-tol", type=float, default=1.0)
    parser.add_argument("--c-tol", type=float, default=10.0)
    parser.add_argument("--max-error-percent", type=float, default=5.0)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--require-feasible", action="store_true",
                        help="do not write a spec unless the best candidate meets the error limit")
    args = parser.parse_args(argv)
    try:
        spec = json.loads(args.spec.read_text(encoding="utf-8"))
        result = optimize_rc_lowpass(
            spec,
            target_fc_hz=_value(args.target_fc, "target-fc"),
            r_min=_value(args.r_min, "r-min"),
            r_max=_value(args.r_max, "r-max"),
            c_min=_value(args.c_min, "c-min"),
            c_max=_value(args.c_max, "c-max"),
            r_series=args.r_series,
            c_series=args.c_series,
            r_tolerance_percent=args.r_tol,
            c_tolerance_percent=args.c_tol,
            max_error_percent=args.max_error_percent,
            top_n=args.top,
        )
        if args.require_feasible and not result["ok"]:
            print(json.dumps({k: v for k, v in result.items() if k != "optimized_spec"}, indent=2))
            print("No candidate meets --max-error-percent; no spec was written.", file=sys.stderr)
            return 2
        args.out_spec.parent.mkdir(parents=True, exist_ok=True)
        args.out_spec.write_text(json.dumps(result["optimized_spec"], indent=2) + "\n", encoding="utf-8")
        print(json.dumps({k: v for k, v in result.items() if k != "optimized_spec"}, indent=2))
        print(f"Optimized spec written to {args.out_spec}")
        return 0 if result["ok"] else 1
    except (OSError, json.JSONDecodeError, ValueError, ValueParseError) as exc:
        print(f"optimization failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
