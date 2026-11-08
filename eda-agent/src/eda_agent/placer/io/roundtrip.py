# SPDX-License-Identifier: MIT
"""Semantic round-trip of the KiCad adapter (plan F0b, task 2).

identity   read -> write the same placement -> read: the IR must not change.
perturbed  move every footprint, rotate it 90 degrees and flip it to the other
           side, write, then restore the original poses and write again: the
           IR must come back unchanged.  This exercises flip before F0c/F1
           depend on it.

Only ``source`` (path, hash, file format) and ``name`` (file stem) may differ.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from ..ir import Board
from .kicad import read_board, write_board

TOL_MM = 2e-6  # pcbnew stores nanometres; two conversions may round twice
TOL_DEG = 1e-4


def _canon(board: Board) -> dict:
    data = board.to_dict()
    data.pop("source", None)
    data.pop("name", None)  # taken from the file name
    return data


def diff(a: object, b: object, path: str = "", out: list[str] | None = None, limit: int = 30) -> list[str]:
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f"{path}.{k}: missing on {'left' if k not in a else 'right'}")
            else:
                diff(a[k], b[k], f"{path}.{k}", out, limit)
    elif isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            out.append(f"{path}: length {len(a)} != {len(b)}")
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                diff(x, y, f"{path}[{i}]", out, limit)
    elif isinstance(a, float) or isinstance(b, float):
        if not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
            out.append(f"{path}: {a!r} != {b!r}")
        else:
            tol = TOL_DEG if path.endswith("rotation") else TOL_MM
            d = abs(a - b)
            if path.endswith("rotation"):
                d = min(d, abs(360.0 - d))
            if d > max(tol, abs(a) * 1e-9) and not math.isclose(a, b, rel_tol=1e-9, abs_tol=tol):
                out.append(f"{path}: {a!r} != {b!r}")
    elif a != b:
        out.append(f"{path}: {a!r} != {b!r}")
    return out


def roundtrip(board_path: str | Path, work: str | Path) -> dict:
    src, work = Path(board_path).resolve(), Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    original = read_board(src)
    base = _canon(original)

    same = work / f"{src.stem}.identity.kicad_pcb"
    write_board(src, original, same)
    identity = diff(base, _canon(read_board(same)))

    moved = original.copy()
    for fp in moved.footprints:
        fp.x, fp.y = fp.x + 1.0, fp.y - 0.5
        fp.rotation = (fp.rotation + 90.0) % 360.0
        fp.side = "B" if fp.side == "F" else "F"
    step = work / f"{src.stem}.perturbed.kicad_pcb"
    write_board(src, moved, step)
    back = work / f"{src.stem}.restored.kicad_pcb"
    write_board(step, original, back)
    restored = diff(base, _canon(read_board(back)))
    return {"board": str(src), "footprints": len(original.footprints), "identity_diffs": identity,
            "restored_diffs": restored, "passed": not identity and not restored}


if __name__ == "__main__":
    out_dir = Path(sys.argv[1])
    results = [roundtrip(p, out_dir / Path(p).stem) for p in sys.argv[2:]]
    print(json.dumps(results, indent=1))
    sys.exit(0 if all(r["passed"] for r in results) else 1)
