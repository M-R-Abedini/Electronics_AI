"""Shared helpers for the circuit-design skill.

Pure Python 3 standard library only.  No numpy/scipy is required, which keeps
the skill usable on Termux, minimal containers and CI images.

The value parser follows SPICE suffix rules (``m`` is milli, ``meg`` is mega)
because every value eventually reaches a SPICE netlist.  Guessing that ``1M``
means one mega-ohm is a classic and expensive silent error, so the parser
returns a warning instead of guessing.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable

# --------------------------------------------------------------------------
# Net names
# --------------------------------------------------------------------------

def spice_net(net: str, ground: str) -> str:
    """Map a spec net to a legal, unambiguous SPICE node name.

    Ground is always node ``0``.  Other names are sanitised to SPICE's
    printable subset.  ``spice_node_collisions()`` checks that the mapping is
    one-to-one before a netlist is written.
    """
    if net == ground:
        return "0"
    name = re.sub(r"[^A-Za-z0-9_+\-.]", "_", str(net).strip())
    if not name:
        name = "N"
    if name[0].isdigit():
        name = "N" + name
    return name


def spice_ref(ref: str, ctype: str) -> str:
    """Return the SPICE element name for a component.

    SPICE identifies subcircuit instances by an ``X`` prefix.  An op-amp
    drawn as ``U1`` therefore becomes ``XU1`` in the netlist, matching what
    LTspice generates for the ``opamp2`` symbol.  The schematic and BOM keep
    the user's ``U1`` reference.
    """
    if ctype == "OPAMP" and not str(ref).upper().startswith("X"):
        return "X" + str(ref)
    return str(ref)


def spice_node_collisions(nets: Iterable[str], ground: str) -> dict[str, list[str]]:
    """Return ``{spice_node_lower: [spec_net, ...]}`` for colliding nets.

    SPICE node names are case-insensitive, so the comparison is lowercased.
    """
    groups: dict[str, list[str]] = {}
    for net in nets:
        node = spice_net(str(net), ground)
        groups.setdefault(node.lower(), []).append(str(net))
    return {node: names for node, names in groups.items() if len(set(names)) > 1}


# --------------------------------------------------------------------------
# SPICE / SI value parsing
# --------------------------------------------------------------------------

# Ordered longest-first so MEG wins over M and MIL wins over M.
_SUFFIXES: list[tuple[str, float]] = [
    ("meg", 1e6),
    ("mil", 25.4e-6),
    ("t", 1e12),
    ("g", 1e9),
    ("k", 1e3),
    ("m", 1e-3),
    ("u", 1e-6),
    ("n", 1e-9),
    ("p", 1e-12),
    ("f", 1e-15),
]

_VALUE_RE = re.compile(
    r"""^\s*
    (?P<num>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)
    \s*(?P<suffix>[a-zA-Zµμ]*)
    \s*$""",
    re.VERBOSE,
)

_MICRO = {"µ", "μ"}


class ValueParseError(ValueError):
    """Raised when a component value cannot be parsed unambiguously."""


def parse_value(text: Any, *, name: str = "value") -> tuple[float, list[str]]:
    """Parse a SPICE-style value such as ``4k7``, ``100n``, ``2.2u`` or ``1meg``.

    Returns ``(value, warnings)``.  Raises :class:`ValueParseError` for text
    that is not a number plus a known suffix.

    Supported forms:
      * plain numbers, including exponent notation (``1e-6``)
      * SPICE suffixes ``t g meg k m u n p f`` (case-insensitive)
      * ``mil`` (thousandths of an inch)
      * ``4k7`` / ``2u2`` / ``1r5`` shorthand
      * a trailing unit after the suffix (``10kohm``, ``100nF``, ``5V``)
    """
    if isinstance(text, bool):
        raise ValueParseError(f"{name}: boolean is not a value")
    if isinstance(text, (int, float)):
        if not math.isfinite(float(text)):
            raise ValueParseError(f"{name}: value must be finite, got {text!r}")
        return float(text), []

    if not isinstance(text, str):
        raise ValueParseError(f"{name}: expected a number or string, got {type(text).__name__}")

    raw = text.strip()
    if not raw:
        raise ValueParseError(f"{name}: empty value")

    # Normalise micro sign to ASCII before suffix matching.
    for ch in _MICRO:
        raw = raw.replace(ch, "u")

    # Shorthand: 4k7 == 4.7k, 2u2 == 2.2u, 1r5 == 1.5 (r is a decimal point).
    m = re.fullmatch(r"([+-]?)(\d+)([rRkKmMuUnNpP])(\d+)", raw)
    if m:
        sign, whole, marker, frac = m.groups()
        if marker in "rR":
            raw = f"{sign}{whole}.{frac}"
        else:
            raw = f"{sign}{whole}.{frac}{marker}"

    m = _VALUE_RE.match(raw)
    if not m:
        raise ValueParseError(f"{name}: cannot parse {text!r}")

    number = float(m.group("num"))
    suffix = m.group("suffix").lower()
    warnings: list[str] = []

    if not suffix:
        return number, warnings

    # Split the known suffix from a trailing unit name (ohm, F, V, H, A, s...).
    units = {
        "", "ohm", "ohms", "f", "farad", "farads", "v", "volt", "volts",
        "a", "amp", "amps", "ampere", "amperes", "h", "henry", "henries",
        "s", "sec", "second", "seconds", "hz", "w", "watt", "watts",
    }
    multiplier = None
    for token, factor in _SUFFIXES:
        if suffix.startswith(token):
            remainder = suffix[len(token):]
            # Reject an unknown trailing unit (e.g. "1kx").
            if remainder not in units:
                continue
            multiplier = factor
            if token == "m" and text.strip().endswith("M"):
                warnings.append(
                    f"{name}: SPICE treats 'M' as milli (1e-3); use 'Meg' for 1e6 "
                    f"if you meant mega"
                )
            break

    if multiplier is None:
        # A bare unit such as "5V" or "10ohm" is still useful.
        unit_only = {
            "ohm": 1.0, "ohms": 1.0, "f": 1.0, "v": 1.0, "a": 1.0,
            "h": 1.0, "s": 1.0, "hz": 1.0, "w": 1.0,
        }
        if suffix in unit_only:
            return number, warnings
        raise ValueParseError(f"{name}: unknown suffix in {text!r}")

    return number * multiplier, warnings


def parse_float(text: Any, *, name: str = "value") -> float:
    return parse_value(text, name=name)[0]


def spice_number(value: float, *, sig: int = 6) -> str:
    """Format a number using SPICE engineering suffixes.

    ``1000 -> '1k'``, ``1e-6 -> '1u'``, ``1e6 -> '1Meg'``.
    Values outside the suffix range fall back to exponent notation.
    """
    if not math.isfinite(value):
        raise ValueError("cannot format a non-finite value")
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    a = abs(value)
    for token, factor in (
        ("T", 1e12), ("G", 1e9), ("Meg", 1e6), ("k", 1e3),
        ("", 1.0), ("m", 1e-3), ("u", 1e-6), ("n", 1e-9),
        ("p", 1e-12), ("f", 1e-15),
    ):
        if a >= factor * 0.999999:
            scaled = a / factor
            text = _trim(scaled, sig)
            return f"{sign}{text}{token}"
    return f"{sign}{_trim(a, sig)}"


def _trim(value: float, sig: int) -> str:
    if value == 0:
        return "0"
    digits = max(0, sig - 1 - int(math.floor(math.log10(abs(value)))))
    digits = min(digits, 15)
    text = f"{value:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def si_number(value: float, unit: str = "", *, sig: int = 6) -> str:
    """Human-readable engineering notation, e.g. ``1.59 kHz``."""
    if not math.isfinite(value):
        return f"{value} {unit}".strip()
    if value == 0:
        return f"0 {unit}".strip()
    sign = "-" if value < 0 else ""
    a = abs(value)
    for prefix, factor in (
        ("T", 1e12), ("G", 1e9), ("M", 1e6), ("k", 1e3),
        ("", 1.0), ("m", 1e-3), ("µ", 1e-6), ("n", 1e-9),
        ("p", 1e-12), ("f", 1e-15),
    ):
        if a >= factor * 0.999999:
            return f"{sign}{_trim(a / factor, sig)} {prefix}{unit}".strip()
    return f"{sign}{_trim(a, sig)} {unit}".strip()


# --------------------------------------------------------------------------
# E-series
# --------------------------------------------------------------------------

E_SERIES: dict[str, list[int]] = {
    "E3": [10, 22, 47],
    "E6": [10, 15, 22, 33, 47, 68],
    "E12": [10, 12, 15, 18, 22, 27, 33, 39, 47, 56, 68, 82],
    "E24": [10, 11, 12, 13, 15, 16, 18, 20, 22, 24, 27, 30, 33, 36, 39,
            43, 47, 51, 56, 62, 68, 75, 82, 91],
    "E48": [100, 105, 110, 115, 121, 127, 133, 140, 147, 154, 162, 169,
            178, 187, 196, 205, 215, 226, 237, 249, 261, 274, 287, 301,
            316, 332, 348, 365, 383, 402, 422, 442, 464, 487, 511, 536,
            562, 590, 619, 649, 681, 715, 750, 787, 825, 866, 909, 953],
    "E96": [100, 102, 105, 107, 110, 113, 115, 118, 121, 124, 127, 130,
            133, 137, 140, 143, 147, 150, 154, 158, 162, 165, 169, 174,
            178, 182, 187, 191, 196, 200, 205, 210, 215, 221, 226, 232,
            237, 243, 249, 255, 261, 267, 274, 280, 287, 294, 301, 309,
            316, 324, 332, 340, 348, 357, 365, 374, 383, 392, 402, 412,
            422, 432, 442, 453, 464, 475, 487, 499, 511, 523, 536, 549,
            562, 576, 590, 604, 619, 634, 649, 665, 681, 698, 715, 732,
            750, 768, 787, 806, 825, 845, 866, 887, 909, 931, 953, 976],
}


def eseries_values(series: str = "E24") -> list[float]:
    key = series.upper()
    if key not in E_SERIES:
        raise ValueError(f"unknown E-series {series!r}; choose {', '.join(E_SERIES)}")
    return [v / 10.0 for v in E_SERIES[key]]


def nearest_eseries(
    value: float,
    series: str = "E24",
    direction: str = "nearest",
) -> dict[str, float]:
    """Return the nearest E-series value at or around ``value``.

    ``direction`` is ``nearest``, ``up`` (>= value) or ``down`` (<= value).
    The result is scaled by a power of ten so the full range is covered.
    """
    if value <= 0:
        raise ValueError("E-series lookup requires a positive value")
    base = eseries_values(series)
    decade = 10 ** math.floor(math.log10(value))
    candidates: list[float] = []
    for mult in (decade / 10, decade, decade * 10):
        candidates.extend(v * mult for v in base)
    candidates = sorted(set(candidates))
    if direction == "up":
        usable = [c for c in candidates if c >= value * (1 - 1e-12)]
        chosen = usable[0] if usable else candidates[-1]
    elif direction == "down":
        usable = [c for c in candidates if c <= value * (1 + 1e-12)]
        chosen = usable[-1] if usable else candidates[0]
    elif direction == "nearest":
        chosen = min(candidates, key=lambda c: abs(math.log(c / value)))
    else:
        raise ValueError("direction must be nearest, up or down")
    return {
        "value": chosen,
        "error_percent": (chosen - value) / value * 100.0,
        "series": series.upper(),
    }


# --------------------------------------------------------------------------
# Result / reporting helpers
# --------------------------------------------------------------------------

def result(
    *,
    inputs: dict[str, Any] | None = None,
    results: dict[str, Any] | None = None,
    formulas: Iterable[str] = (),
    assumptions: Iterable[str] = (),
    warnings: Iterable[str] = (),
    notes: Iterable[str] = (),
) -> dict[str, Any]:
    """Build the uniform JSON envelope every calculator returns."""
    return {
        "ok": True,
        "inputs": inputs or {},
        "results": results or {},
        "formulas": list(formulas),
        "assumptions": list(assumptions),
        "warnings": list(warnings),
        "notes": list(notes),
    }


def fail(message: str, *, warnings: Iterable[str] = ()) -> dict[str, Any]:
    return {"ok": False, "error": message, "warnings": list(warnings)}


def _json_default(obj: Any) -> Any:
    if isinstance(obj, float):
        if math.isnan(obj):
            return "NaN"
        if math.isinf(obj):
            return "Infinity" if obj > 0 else "-Infinity"
    if isinstance(obj, set):
        return sorted(obj)
    raise TypeError(f"not JSON serialisable: {type(obj).__name__}")


def _json_safe(value: Any) -> Any:
    """Replace non-finite floats so the output is strict JSON.

    ``json.dumps(..., allow_nan=False)`` raises on ``inf``/``nan`` *before*
    the ``default`` hook is consulted, so sanitise the tree explicitly.
    """
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def dumps(data: Any, *, pretty: bool = True) -> str:
    return json.dumps(
        _json_safe(data),
        indent=2 if pretty else None,
        sort_keys=False,
        allow_nan=False,
        default=_json_default,
    )


def emit(data: dict[str, Any], *, as_json: bool = False, pretty: bool = True) -> None:
    """Print a result envelope as JSON or as a compact human summary."""
    if as_json:
        print(dumps(data, pretty=pretty))
        return
    if not data.get("ok", False):
        print(f"ERROR: {data.get('error', 'unknown error')}")
        for w in data.get("warnings", []):
            print(f"WARNING: {w}")
        return
    for key, value in data.get("results", {}).items():
        if isinstance(value, float):
            print(f"{key}: {si_number(value)}")
        elif isinstance(value, dict):
            print(f"{key}:")
            for k, v in value.items():
                print(f"  {k}: {v}")
        else:
            print(f"{key}: {value}")
    for f in data.get("formulas", []):
        print(f"formula: {f}")
    for a in data.get("assumptions", []):
        print(f"assumption: {a}")
    for w in data.get("warnings", []):
        print(f"WARNING: {w}")
    for n in data.get("notes", []):
        print(f"note: {n}")


def parallel(*resistances: float) -> float:
    """Parallel combination of positive resistances (0 is ignored)."""
    vals = [r for r in resistances if r is not None and r > 0]
    if not vals:
        return math.inf
    return 1.0 / sum(1.0 / r for r in vals)


def series(*resistances: float) -> float:
    return sum(r for r in resistances if r is not None)


def divider_ratio(r_top: float, r_bottom: float) -> float:
    if r_top < 0 or r_bottom < 0 or (r_top + r_bottom) <= 0:
        raise ValueError("divider resistors must be non-negative and not both zero")
    return r_bottom / (r_top + r_bottom)
