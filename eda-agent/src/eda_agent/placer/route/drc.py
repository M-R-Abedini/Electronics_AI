# SPDX-License-Identifier: MIT
"""KiCad DRC through ``kicad-cli`` and the router-attribution rule (architecture 8.1.3).

A violation is *router-caused* when any of its items is routing copper (track,
arc or via).  Any other violation is *pre-existing* only if the same violation
(type and item descriptions) is also reported on the untouched input board;
otherwise it still fails the gate.  The designer's DRC exclusions stored in the
project are honoured, because ``--severity-exclusions`` is not requested.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ..toolchain import RUNTIME_DIR, kicad, sha256_file

_ROUTING = re.compile(r"^(Track|Via|Arc|Blind/buried via|Micro via|Through via)\b")
_NON_COPPER_LAYER = re.compile(r"\.(Silkscreen|Mask|Fab|Courtyard|Paste|CrtYd|User)\b")
_NET = re.compile(r"\[([^\]]*)\]")
_PAD_REF = re.compile(r"\b[Pp]ad .* of (\S+)")
_GATED = ("error",)


def run(board: Path, out_json: Path, *, refill: bool = True, timeout_s: int = 600) -> dict:
    cmd = [str(kicad().cli), "pcb", "drc", "--format", "json", "--severity-error", "--severity-warning",
           "--units", "mm", "-o", str(out_json)]
    if refill:
        cmd.append("--refill-zones")
    cmd.append(str(board))
    done = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout_s,
                          check=False)
    if not out_json.is_file():
        raise RuntimeError(f"kicad-cli drc failed ({done.returncode}): {done.stderr.strip()[:400]}")
    return json.loads(out_json.read_text(encoding="utf-8"))


def run_cached(board: Path, out_json: Path) -> dict:
    """DRC of an input board, cached by the hashes of the board and its project files.

    The input never changes between baselines of one benchmark, and its DRC with
    zone refill is the slowest step of a route check (about a minute on ecc83).
    """
    board = board.resolve()
    parts = [sha256_file(board)] + [sha256_file(board.with_suffix(s)) for s in (".kicad_pro", ".kicad_dru")
                                    if board.with_suffix(s).is_file()]
    key = hashlib.sha256(("|".join(parts) + "|" + kicad().version).encode()).hexdigest()[:32]
    cache = RUNTIME_DIR / "cache" / "drc" / f"{key}.json"
    if cache.is_file():
        shutil.copyfile(cache, out_json)
        return json.loads(cache.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="eai_drc_") as tmp:
        copy = Path(tmp) / board.name  # never run kicad-cli next to the user's file
        shutil.copyfile(board, copy)
        for s in (".kicad_pro", ".kicad_dru"):
            if board.with_suffix(s).is_file():
                shutil.copyfile(board.with_suffix(s), copy.with_suffix(s))
        result = run(copy, out_json)
    cache.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(out_json, cache)
    return result


def _key(v: dict) -> tuple:
    return (v.get("type"), tuple(sorted(i.get("description", "") for i in v.get("items", []))))


def new_mask_bridges(before: dict, after: dict, moved_refs: set[str]) -> list[dict]:
    """Find new affected pads or growth on an aperture despite DRC witness churn."""
    def aperture(row):
        return tuple(sorted(item.get("uuid") or item["description"] for item in row["items"]
                            if ".Mask" in item.get("description", "")))

    def moved_pads(row):
        return tuple(sorted(item.get("uuid") or item["description"] for item in row["items"]
                            if (part := _PAD_REF.search(item.get("description", "")))
                            and part[1] in moved_refs))

    def bridges(report):
        return [row for row in report.get("violations", [])
                if row["type"] == "solder_mask_bridge" and row["severity"] == "error"]

    previous = bridges(before)
    counts = Counter(map(aperture, previous))
    known = {(aperture(row), moved_pads(row)) for row in previous if moved_pads(row)}
    added = []
    for row in bridges(after):
        artwork = aperture(row)
        identity = artwork, moved_pads(row)
        if counts[artwork] <= 0 or (identity[1] and identity not in known):
            added.append(row)
        counts[artwork] -= 1
        known.add(identity)
    return added


def is_routing_item(description: str) -> bool:
    if not _ROUTING.match(description):
        return False
    return not (description.startswith("Arc of ") or _NON_COPPER_LAYER.search(description))


@dataclass
class Attribution:
    router: list[dict] = field(default_factory=list)
    pre_existing: list[dict] = field(default_factory=list)
    new_other: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)

    def counts(self) -> dict:
        by_type: dict[str, int] = {}
        for v in self.router + self.new_other:
            by_type[v["type"]] = by_type.get(v["type"], 0) + 1
        return {"router_caused": len(self.router), "pre_existing": len(self.pre_existing),
                "new_non_routing": len(self.new_other), "warnings": len(self.warnings),
                "failing_by_type": dict(sorted(by_type.items()))}


def attribute(routed: dict, reference: dict) -> Attribution:
    """Split the routed board's violations; ``reference`` is DRC of the input board."""
    known = {_key(v) for v in reference.get("violations", [])}
    out = Attribution()
    for v in routed.get("violations", []):
        if v.get("severity") not in _GATED:
            out.warnings.append(v)
        elif any(is_routing_item(i.get("description", "")) for i in v.get("items", [])):
            out.router.append(v)
        elif _key(v) in known:
            out.pre_existing.append(v)
        else:
            out.new_other.append(v)
    return out


def item_net(description: str) -> str | None:
    m = _NET.search(description)
    return m.group(1) if m else None
