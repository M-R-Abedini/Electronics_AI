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
from eda_agent.placer.place import escape, problem
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


def test_decap_affinity_uses_nearest_allowed_pin_without_counting_crossings():
    board = _board()
    chip, cap = board.fp("U1"), board.fp("R1")
    chip.pads = [Pad("1", "A", (-0.6, 0), (0.3, 0.3)),
                 Pad("2", "A", (0.6, 0), (0.3, 0.3)),
                 Pad("3", "A", (0, 0), (0.3, 0.3))]
    role = {"value": "decap", "confidence": 0.9}
    target = {"value": {"ref": "U1", "net": "A", "pins": ["2", "3"]}, "confidence": 0.9}
    intent = {"parts": {cap.ref: {"role": role, "target": target}}}

    with_intent = problem.build(board, intent=intent)
    without = problem.build(board)
    assert len(with_intent.affinities) == 1
    actual = problem.rust_problem(with_intent).cost()
    plain = problem.rust_problem(without).cost()
    expected = min(math.dist(cap.pad_world(cap.pads[0]), chip.pad_world(pin)) for pin in chip.pads[1:])
    assert math.isclose(actual["hpwl"] - plain["hpwl"], expected * problem.DECAP_AFFINITY_WEIGHT)
    assert actual["crossings"] == plain["crossings"]
    assert math.isclose(actual["total"], problem.reference_cost(with_intent, with_intent.poses())["total"])

    target["confidence"] = 0.1
    assert not problem.build(board, intent=intent).affinities


def test_s1_fixes_locked_parts_only_here():
    assert problem.build(_board()).fixed == {"U1": "locked in the file"}


def test_floorplan_reserves_and_audits_rule_sized_pad_exits():
    board = _board()
    chip = board.fp("U1")
    chip.pads = [Pad(str(i + 1), "A", (0.8, (i - 3.5) * 0.12), (0.35, 0.10)) for i in range(8)]
    blocker = board.fp("R1")
    blocker.x, blocker.y = 12.1, 10.0
    intent = {"parts": {"U1": {"role": {"value": "ic", "confidence": 0.95}}}}
    pb = problem.build(board, intent=intent, floorplan=True)
    assert pb.parts[0][2] > problem.build(board, intent=intent).parts[0][2]
    assert len(pb.exits["U1"]) == 8
    assert math.isclose(pb.exits["U1"][0].width, 0.6)
    result = escape.audit(board, pb.exits)
    assert result["clear"] == 0 and all(row["blocked_by"] == ["R1"] for row in result["blocked"])
    blocker.side = "B"
    assert escape.audit(board, pb.exits)["clear"] == 8


def test_floorplan_adds_verified_block_pin_links_only():
    board = _board()
    part = board.fp("R1")
    intent = {"parts": {part.ref: {"role": {"value": "crystal", "confidence": 0.9},
                                  "target": {"value": {"ref": "U1", "pins": ["1"]}, "confidence": 0.8}}}}
    assert problem.build(board, intent=intent, floorplan=True).local_block_links == 1
    assert not problem.build(board, intent=intent).affinities
    intent["parts"][part.ref]["target"]["value"]["pins"] = ["2"]
    assert not problem.build(board, intent=intent, floorplan=True).affinities


def test_placement_readback_uses_the_mirrored_footprint_frame():
    board = _board()
    part = board.fp("R1")
    part.side = "B"
    part.pads[0].local = (0.2, 0.4)
    part.pads[0].rotation = 30.0
    part.pads[0].layers = ("B.Cu", "B.Mask")
    pb = problem.build(board, side_policy="top_only")
    result = problem.apply(pb, pb.poses())
    flipped = result.fp("R1")
    assert (flipped.side, flipped.pads[0].local, flipped.pads[0].rotation) == ("F", (0.2, -0.4), -30.0)
    assert flipped.pads[0].layers[0] == "F.Cu"
    assert (part.side, part.pads[0].local) == ("B", (0.2, 0.4))


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
