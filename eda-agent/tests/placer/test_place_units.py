# SPDX-License-Identifier: MIT
"""Placer S1-S5 on a synthetic board (Rust core + numpy; no KiCad)."""

from __future__ import annotations

import math

from eda_agent.placer.eval.score import evaluate
from eda_agent.placer.ir import (
    Board,
    CopperLayer,
    Courtyard,
    Footprint,
    Net,
    NetClass,
    Outline,
    Pad,
    derive,
)
from eda_agent.placer.place import problem
from eda_agent.placer.place.pipeline import solve

BOX = [(-1.0, -0.6), (1.0, -0.6), (1.0, 0.6), (-1.0, 0.6)]


def _fp(ref, x, y, nets, rot=0.0, locked=False):
    pads = [Pad(str(i + 1), n, (-0.6 + 1.2 * i, 0.0), (0.5, 0.6), layers=("F.Cu",)) for i, n in enumerate(nets)]
    return Footprint(ref, "v", "lib:x", x, y, rot, "F", locked=locked, pads=pads, courtyard=Courtyard("file", [BOX]),
                     uuid="u-" + ref)


def _board():
    fps = [_fp("U1", 10.0, 10.0, ["A", "B"], locked=True)]
    nets = ["A", "B", "C", "D", "E", "F"]
    for i in range(8):  # a chain U1 - R1 - R2 ... plus crossings to remove
        fps.append(_fp(f"R{i + 1}", 3.0 + 2.0 * i, 3.0 + (i % 3) * 5.0, [nets[i % 6], nets[(i + 3) % 6]]))
    b = Board(name="t", footprints=fps, outline=[Outline([(0, 0), (20, 0), (20, 20), (0, 20)])],
              board_area_mm2=400.0, copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)],
              netclasses=[NetClass("Default", 0.2, 0.2, 0.6, 0.3)], nets=[Net(n, "Default", 3) for n in nets])
    return derive(b)


def test_rust_cost_matches_python_reference():
    pb = problem.build(_board())
    core = problem.rust_problem(pb)
    rust, ref = core.cost(), problem.reference_cost(pb, pb.poses())
    assert math.isclose(rust["hpwl"], ref["hpwl"], rel_tol=1e-12)
    assert rust["crossings"] == ref["crossings"]


def test_s1_fixes_locked_parts_only_here():
    assert problem.build(_board()).fixed == {"U1": "locked in the file"}


def test_solve_is_legal_better_and_deterministic():
    board = _board()
    start = evaluate(board)["metrics"]["hpwl"]["all_mm"]
    a, info = solve(board, mode="best", seed=3)
    b, _ = solve(board, mode="best", seed=3)
    assert [(f.x, f.y, f.rotation) for f in a.footprints] == [(f.x, f.y, f.rotation) for f in b.footprints]
    ev = evaluate(a, fixed=set(info["fixed"]))
    assert ev["legality"]["strict_violations"] == 0
    assert ev["metrics"]["hpwl"]["all_mm"] < start
    assert a.fp("U1").x == 10.0 and a.fp("U1").y == 10.0
    assert set(info["candidates"]) == {"analytical", "sa_only"}
