"""Parse ngspice ASCII rawfiles into plots, variables and typed values.

The ASCII rawfile format is a sequence of plots.  Each plot has a header
(``Plotname``, ``Flags``, ``No. Variables``, ``No. Points``, ``Variables``)
followed by a ``Values`` table.  Real values are floats; complex values are
``real,imag`` pairs.  The first variable is the scale (frequency or time).

This module is deliberately dependency-free so it runs wherever the skill runs.
It does not interpret the circuit; it only turns the rawfile into a structure
that the metric layer can query.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Plot:
    """One analysis plot from a rawfile."""

    name: str
    flags: set[str] = field(default_factory=set)
    variables: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    points: list[list[float | complex]] = field(default_factory=list)

    @property
    def is_complex(self) -> bool:
        return "complex" in self.flags

    def scale(self) -> list[float]:
        """Return the first variable (frequency/time) as real floats.

        ngspice writes AC and noise frequency columns as complex pairs with a
        zero imaginary part.  A non-zero imaginary part is a malformed scale,
        not something to discard silently.
        """
        values: list[float] = []
        for row in self.points:
            value = row[0]
            if isinstance(value, complex):
                if abs(value.imag) > 1e-9 * max(1.0, abs(value.real)):
                    raise ValueError(
                        f"{self.name!r} scale has a non-zero imaginary part: "
                        f"{value!r}")
                value = value.real
            values.append(float(value))
        return values

    def column(self, name: str) -> list[float | complex]:
        """Return one variable's values, matching names case-insensitively."""
        lookup = {var.lower(): index for index, var in enumerate(self.variables)}
        try:
            index = lookup[name.lower()]
        except KeyError as exc:
            raise KeyError(
                f"{self.name!r} has no variable {name!r}; "
                f"available: {', '.join(self.variables)}"
            ) from exc
        return [row[index] for row in self.points]


def _value(token: str) -> float | complex:
    """Parse one rawfile value.  A comma means a complex pair."""
    token = token.strip().replace(" ", "")
    if "," in token:
        real, imag = token.split(",", 1)
        return complex(float(real), float(imag))
    return float(token)


def _header_int(lines: list[str], index: int, key: str) -> int | None:
    prefix = key + ":"
    for line in lines[index:]:
        stripped = line.strip()
        if stripped.startswith(prefix):
            try:
                return int(stripped.split(":", 1)[1].strip())
            except ValueError:
                return None
        if stripped.startswith("Variables:") or stripped.startswith("Values:"):
            break
    return None


def parse_raw(text: str) -> dict[str, Plot]:
    """Parse an ngspice ASCII rawfile into ``{plotname: Plot}``.

    Malformed or truncated input is ignored rather than guessed.  A completed
    plot is returned only when its variable table and values agree.
    """
    lines = text.splitlines()
    plots: dict[str, Plot] = {}
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index].strip()
        if not line.startswith("Plotname:"):
            index += 1
            continue
        name = line.split(":", 1)[1].strip()
        plot = Plot(name=name)
        index += 1
        expected_points: int | None = None
        while index < total and not lines[index].strip().startswith("Variables:"):
            header = lines[index].strip()
            if header.startswith("Flags:"):
                plot.flags = set(header.split(":", 1)[1].split())
            elif header.startswith("No. Points:"):
                try:
                    expected_points = int(header.split(":", 1)[1].strip())
                except ValueError:
                    expected_points = None
            index += 1
        if index >= total:
            break
        # Variable table.
        index += 1
        while index < total:
            entry = lines[index].strip()
            if not entry or entry.startswith("Values:"):
                break
            parts = entry.split()
            if (len(parts) >= 2
                    and parts[0].lstrip("+-").isdigit()):
                plot.variables.append(parts[1])
                plot.types.append(parts[2] if len(parts) > 2 else "")
            index += 1
        while index < total and not lines[index].strip().startswith("Values:"):
            index += 1
        if index >= total:
            break
        index += 1
        if not plot.variables:
            plots[name] = plot
            continue
        variable_count = len(plot.variables)
        while index < total:
            raw = lines[index].strip()
            if not raw:
                index += 1
                continue
            if raw.startswith("Plotname:"):
                break
            parts = raw.split()
            if not parts or not parts[0].lstrip("+-").isdigit():
                index += 1
                continue
            tokens: list[str] = parts[1:]
            index += 1
            # Subsequent variables are normally one per line.  Some writers
            # put several values on the index line; both are handled.
            while len(tokens) < variable_count and index < total:
                nxt = lines[index].strip()
                if not nxt or nxt.startswith("Plotname:"):
                    break
                # A line that starts with an integer and has no comma is the
                # next point's index when the current point is incomplete.
                if (len(tokens) < variable_count
                        and re.fullmatch(r"[+-]?\d+", nxt.split()[0])
                        and "," not in nxt):
                    break
                tokens.extend(nxt.split())
                index += 1
            if len(tokens) < variable_count:
                # Truncated point: stop this plot without inventing data.
                break
            row: list[float | complex] = []
            try:
                row = [_value(token) for token in tokens[:variable_count]]
            except ValueError:
                break
            plot.points.append(row)
            if expected_points is not None and len(plot.points) >= expected_points:
                break
        plots[name] = plot
    return plots


def find_plot(plots: dict[str, Plot], kind: str) -> Plot | None:
    """Find a plot by analysis kind: ``ac``, ``tran``, ``noise`` or ``op``."""
    kind = kind.lower()
    for name, plot in plots.items():
        low = name.lower()
        if kind == "ac" and ("ac analysis" in low or (
                plot.is_complex and plot.variables
                and plot.variables[0].lower() == "frequency")):
            return plot
        if kind == "tran" and re.search(r"\btran(?:sient)?\b", low):
            return plot
        if kind == "noise" and "noise" in low:
            return plot
        if kind == "op" and ("operating point" in low or low == "op"):
            return plot
    return None
