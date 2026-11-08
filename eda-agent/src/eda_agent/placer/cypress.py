# SPDX-License-Identifier: MIT
"""Cypress (ISPD 2025, NVlabs/Cypress, Apache-2.0) as an external baseline (plan F1, task 5).

Cypress needs a CUDA GPU, so it is never part of the product.  The exchange
skips KiCad on the GPU side: Cypress's own ``pcb-util`` reads KiCad through
``kiutils``, which predates the KiCad 10 file format, and its Bookshelf writer
anonymises reference designators by default.  Instead:

export   ``pixi run cypress export``: every corpus board becomes a JSON file in
         the dictionary form of Cypress's own ``pcb.PCB`` (``PCB.from_dict``),
         components = world courtyard polygons on the expert's side (P2), pins
         relative to the courtyard centre, all nets, plus the S1 fixed parts.
         Everything goes into one ``cypress_in.zip``.
GPU      ``scripts/cypress/cypress_colab.ipynb`` builds Cypress against a
         pinned PyTorch 1.13.1 / CUDA 11.7 stack, refuses to run without CUDA,
         places every board and returns ``cypress_out.zip``.
import   ``pixi run cypress import cypress_out.zip``: the placed dictionaries
         become poses on the original board (rotation recovered from the pins,
         translation from the fixed parts), ready for the C1 route check.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import zipfile
from pathlib import Path

from . import corpus
from .eval.legality import courtyard_polygon
from .geometry import transform
from .io.kicad import read_board, write_board
from .ir import Board
from .place.problem import fixed_parts, local_box
from .toolchain import RUNTIME_DIR, sha256_file

ALIGN_TOL_MM = 0.05


def _pin_names(fp) -> list[str]:
    seen: dict[str, int] = {}
    names = []
    for pad in fp.pads:
        k = seen.get(pad.number, 0)
        seen[pad.number] = k + 1
        names.append(pad.number if k == 0 else f"{pad.number}#{k}")
    return names


def to_cypress(board: Board, fixed: set[str]) -> dict:
    """The board in the dictionary form of Cypress's ``pcb.PCB`` (millimetres, KiCad axes)."""
    comps, nets = {}, {}
    xs, ys = [], []
    for fp in board.footprints:
        shape = courtyard_polygon(fp)
        ring = [list(map(float, p)) for p in shape.exterior.coords[:-1]] if shape is not None else []
        if not ring:
            x0, y0, x1, y1 = local_box(fp)
            ring = [list(transform(c, (fp.x, fp.y), fp.rotation)) for c in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        cx = (min(p[0] for p in ring) + max(p[0] for p in ring)) / 2
        cy = (min(p[1] for p in ring) + max(p[1] for p in ring)) / 2
        pins = {}
        for name, pad in zip(_pin_names(fp), fp.pads, strict=True):
            if not pad.net:
                continue
            ax, ay = fp.pad_world(pad)
            pins[name] = {"pin_name": name, "anon_name": name, "component": fp.ref, "net": pad.net,
                          "rltv_x": ax - cx, "rltv_y": ay - cy, "abs_x": ax, "abs_y": ay}
            nets.setdefault(pad.net, {"net_name": pad.net, "anon_name": pad.net, "pins": []})["pins"].append(
                [fp.ref, name])
        comps[fp.ref] = {"name": fp.ref, "anon_name": fp.ref, "shape": ring, "ctr_x": cx, "ctr_y": cy,
                         "layer": "TOP" if fp.side == "F" else "BOTTOM", "page": 0, "height": 0.0, "pins": pins}
        xs += [p[0] for p in ring]
        ys += [p[1] for p in ring]
    for o in board.outline:
        xs += [p[0] for p in o.outer]
        ys += [p[1] for p in o.outer]
    return {"name": board.name, "components": comps, "nets": nets, "layers": ["TOP", "BOTTOM"],
            "x_range": [min(xs), max(xs)], "y_range": [min(ys), max(ys)], "fixed": sorted(fixed)}


def export(names: list[str], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"format": "Cypress pcb.PCB.from_dict, mm, KiCad axes (+Y down); 'fixed' lists S1 parts",
                "boards": []}
    files = []
    for name in names:
        entry = corpus.get(name)
        board = read_board(entry.path)
        fixed = set(fixed_parts(board))
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(to_cypress(board, fixed)), encoding="utf-8")
        files.append(path)
        manifest["boards"].append({"name": name, "file": path.name, "source_sha256": sha256_file(entry.path),
                                   "components": len(board.footprints), "fixed": len(fixed)})
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    archive = out_dir / "cypress_in.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in [*files, out_dir / "manifest.json"]:
            zf.write(path, path.name)
    return archive


def _angle(v: tuple[float, float]) -> float:
    return math.degrees(math.atan2(v[1], v[0]))


def import_result(original: Board, before: dict, after: dict) -> tuple[Board, dict]:
    """Poses from a placed Cypress dictionary onto ``original``.

    Rotation: Cypress turns shapes and pins about the centre, so the turn is the
    median angle change of the pin vectors (KiCad convention: R(d) maps an
    angle a to a - d).  Translation: median shift of the fixed parts.
    """
    fixed = set(before["fixed"])
    comps_b, comps_a = before["components"], after["components"]
    anchors = [(comps_b[r]["ctr_x"] - comps_a[r]["ctr_x"], comps_b[r]["ctr_y"] - comps_a[r]["ctr_y"])
               for r in fixed if r in comps_a]
    if not anchors:
        raise ValueError("no fixed part to align Cypress's output with the board")
    dx, dy = statistics.median(a[0] for a in anchors), statistics.median(a[1] for a in anchors)
    misaligned = sorted(r for r in fixed if r in comps_a and max(
        abs(comps_b[r]["ctr_x"] - comps_a[r]["ctr_x"] - dx), abs(comps_b[r]["ctr_y"] - comps_a[r]["ctr_y"] - dy))
        > ALIGN_TOL_MM)
    out = original.copy()
    odd_angles, side_changes, missing = [], [], []
    for fp in out.footprints:
        if fp.ref in fixed:
            continue
        if fp.ref not in comps_a:
            missing.append(fp.ref)
            continue
        b, a = comps_b[fp.ref], comps_a[fp.ref]
        turns = []
        for name, pin in b["pins"].items():
            pa = a["pins"].get(name)
            if pa is None or math.hypot(pin["rltv_x"], pin["rltv_y"]) < 0.05:
                continue
            d = _angle((pin["rltv_x"], pin["rltv_y"])) - _angle((pa["rltv_x"], pa["rltv_y"]))
            turns.append((d + 180.0) % 360.0 - 180.0)
        turn = statistics.median(turns) if turns else 0.0
        snapped = round(turn / 90.0) * 90.0
        if abs(turn - snapped) < 1.0:
            turn = snapped
        else:
            odd_angles.append(fp.ref)
        rotation = (fp.rotation + turn) % 360.0
        x0, y0, x1, y1 = local_box(fp)
        c_local = ((x0 + x1) / 2, (y0 + y1) / 2)
        ox, oy = transform(c_local, (0.0, 0.0), rotation)
        fp.x, fp.y, fp.rotation = round(a["ctr_x"] + dx - ox, 4), round(a["ctr_y"] + dy - oy, 4), round(rotation, 4)
        side = "F" if a["layer"] == "TOP" else "B"
        if side != fp.side:
            side_changes.append(fp.ref)
            fp.side = side
    return out, {"offset_mm": [round(dx, 4), round(dy, 4)], "misaligned_fixed": misaligned, "missing": missing,
                 "non_quarter_turns": odd_angles, "side_changes": side_changes}


def import_all(archive: Path, exported: Path, out_dir: Path) -> list[dict]:
    rows = []
    with zipfile.ZipFile(archive) as zf:
        names = sorted(n for n in zf.namelist() if n.endswith(".placed.json"))
        summary = json.loads(zf.read("summary.json")) if "summary.json" in zf.namelist() else {}
        for member in names:
            name = Path(member).name.removesuffix(".placed.json")
            entry = corpus.get(name)
            before = json.loads((exported / f"{name}.json").read_text(encoding="utf-8"))
            after = json.loads(zf.read(member))
            placed, info = import_result(read_board(entry.path), before, after)
            target = write_board(entry.path, placed, out_dir / name / "cypress.kicad_pcb", strip_routing=True)
            rows.append({"name": name, "board_file": str(target), **info})
    (out_dir / "import.json").write_text(json.dumps({"summary": summary, "boards": rows}, indent=1), encoding="utf-8")
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cypress", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export")
    ex.add_argument("--split", default="dev")
    ex.add_argument("--out", default=str(RUNTIME_DIR / "cypress" / "in"))
    im = sub.add_parser("import")
    im.add_argument("archive", help="cypress_out.zip from the notebook")
    im.add_argument("--exported", default=str(RUNTIME_DIR / "cypress" / "in"))
    im.add_argument("--out", default=str(RUNTIME_DIR / "cypress" / "imported"))
    args = ap.parse_args(argv)
    if args.cmd == "export":
        archive = export([e.name for e in corpus.entries(args.split)], Path(args.out))
        print(json.dumps({"upload_this": str(archive)}, indent=1))
    else:
        print(json.dumps(import_all(Path(args.archive), Path(args.exported), Path(args.out)), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
