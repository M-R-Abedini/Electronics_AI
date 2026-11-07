# SPDX-License-Identifier: MIT
"""Edits to the Specctra DSN that KiCad exports (architecture 7.3).

KiCad's exporter writes coordinates in micrometres with the Y axis negated
(``(unit um)``; checked against the board outline and component places).  We
only ever *add* keepouts; nothing KiCad wrote is changed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_UNIT = re.compile(r"^\s*\(unit (\w+)\)", re.MULTILINE)
_SCALE = {"um": 1000.0, "mil": 1000.0 / 25.4, "mm": 1.0}


@dataclass(frozen=True)
class Keepout:
    """A wire + via keepout on one copper layer (board millimetres, KiCad Y down)."""

    name: str
    layer: str
    outline: tuple[tuple[float, float], ...]


def _num(v: float) -> str:
    text = f"{v:.1f}"
    return text.removesuffix(".0")


def _quote(name: str) -> str:
    return '"' + name.replace('"', "'") + '"'


def _token(name: str) -> str:
    return name if re.fullmatch(r"[\w.+\-]+", name) else _quote(name)


def keepout_text(keepout: Keepout, scale: float) -> str:
    coords = " ".join(f"{_num(x * scale)} {_num(-y * scale)}" for x, y in keepout.outline)
    return f"    (keepout {_quote(keepout.name)} (polygon {_token(keepout.layer)} 0 {coords}))\n"


def add_keepouts(dsn: str, keepouts: list[Keepout]) -> str:
    """Insert keepouts into the ``(structure ...)`` section, before its via list."""
    if not keepouts:
        return dsn
    unit = _UNIT.search(dsn)
    if unit is None or unit.group(1) not in _SCALE:
        raise ValueError("DSN has no supported (unit ...) declaration")
    scale = _SCALE[unit.group(1)]
    start = dsn.find("(structure")
    via = re.compile(r"^\s*\(via ", re.MULTILINE).search(dsn, start)
    if start < 0 or via is None:
        raise ValueError("DSN structure section not recognised")
    block = "".join(keepout_text(k, scale) for k in keepouts)
    return dsn[:via.start()] + block + dsn[via.start():]


def layers(dsn: str) -> list[str]:
    start = dsn.find("(structure")
    end = dsn.find("(boundary", start)
    return re.findall(r"\(layer ([^\s()]+)", dsn[start:end])
