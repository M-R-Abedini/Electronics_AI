# SPDX-License-Identifier: MIT
"""Cypress (ISPD 2025, NVlabs/Cypress, Apache-2.0) as an external baseline (plan F1, task 5).

Cypress needs a GPU, so it is never part of the product.  The flow is:

export   ``pixi run cypress export``: one KiCad board per corpus entry with the
         routing removed, every S1 fixed part locked and every part on the
         expert's side (P2), plus ``manifest.json`` with hashes.
A100     ``scripts/cypress/run_cypress_a100.sh <in> <out>`` (run by the user):
         Cypress's own converters (``pcb-util``: ``KiCadConverter`` and
         ``BookshelfConverter``) turn each board into Bookshelf, ``Placer.py``
         places it, the converters write a ``.kicad_pcb`` back.
import   ``pixi run cypress import <out>``: poses are read by reference,
         aligned on the fixed parts (Cypress shifts the design to its origin),
         checked, and written onto the original board, ready for the C1 route
         check like every other baseline.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from . import corpus
from .io.kicad import read_board, write_board
from .ir import Board
from .place.problem import fixed_parts
from .toolchain import RUNTIME_DIR, sha256_file

ALIGN_TOL_MM = 0.05  # fixed parts must agree within this after alignment


def export(names: list[str], out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"format": "kicad_pcb, routing removed, S1 fixed parts locked, expert sides", "boards": []}
    for name in names:
        entry = corpus.get(name)
        board = read_board(entry.path)
        fixed = fixed_parts(board)
        for fp in board.footprints:
            fp.locked = fp.ref in fixed
        target = write_board(entry.path, board, out_dir / f"{name}.kicad_pcb", strip_routing=True)
        manifest["boards"].append({"name": name, "file": target.name, "sha256": sha256_file(target),
                                   "source_sha256": sha256_file(entry.path), "fixed": fixed,
                                   "footprints": len(board.footprints)})
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def import_result(original: Board, cypress_board: Board, fixed: set[str]) -> tuple[Board, dict]:
    """Copy Cypress's poses onto ``original``; translation recovered from the fixed parts."""
    theirs = {fp.ref: fp for fp in cypress_board.footprints}
    missing = sorted(fp.ref for fp in original.footprints if fp.ref not in theirs)
    anchors = [(fp.x - theirs[fp.ref].x, fp.y - theirs[fp.ref].y) for fp in original.footprints
               if fp.ref in fixed and fp.ref in theirs]
    if not anchors:
        raise ValueError("no fixed part to align Cypress's output with the board")
    dx = statistics.median(a[0] for a in anchors)
    dy = statistics.median(a[1] for a in anchors)
    misaligned = sorted(r for r in fixed if r in theirs and
                        max(abs(original.fp(r).x - theirs[r].x - dx), abs(original.fp(r).y - theirs[r].y - dy))
                        > ALIGN_TOL_MM)
    out = original.copy()
    side_changes = []
    for fp in out.footprints:
        if fp.ref in fixed or fp.ref not in theirs:
            continue
        t = theirs[fp.ref]
        fp.x, fp.y, fp.rotation = round(t.x + dx, 4), round(t.y + dy, 4), t.rotation
        if t.side != fp.side:
            side_changes.append(fp.ref)
            fp.side = t.side
    return out, {"offset_mm": [round(dx, 4), round(dy, 4)], "missing": missing, "misaligned_fixed": misaligned,
                 "side_changes": side_changes}


def import_all(result_dir: Path, out_dir: Path) -> list[dict]:
    rows = []
    for path in sorted(result_dir.glob("*.cypress.kicad_pcb")):
        name = path.name.removesuffix(".cypress.kicad_pcb")
        entry = corpus.get(name)
        original = read_board(entry.path)
        fixed = set(fixed_parts(original))
        placed, info = import_result(original, read_board(path), fixed)
        target = write_board(entry.path, placed, out_dir / name / "cypress.kicad_pcb", strip_routing=True)
        rows.append({"name": name, "board_file": str(target), **info})
    (out_dir / "import.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cypress", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export")
    ex.add_argument("--split", default="dev")
    ex.add_argument("--out", default=str(RUNTIME_DIR / "cypress" / "in"))
    im = sub.add_parser("import")
    im.add_argument("results")
    im.add_argument("--out", default=str(RUNTIME_DIR / "cypress" / "imported"))
    args = ap.parse_args(argv)
    if args.cmd == "export":
        m = export([e.name for e in corpus.entries(args.split)], Path(args.out))
        print(json.dumps({"out": args.out, "boards": [b["name"] for b in m["boards"]]}, indent=1))
    else:
        print(json.dumps(import_all(Path(args.results), Path(args.out)), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
