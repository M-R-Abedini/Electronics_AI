# SPDX-License-Identifier: MIT
"""Placer S1-S5 on a synthetic board (Rust core + numpy; no KiCad)."""

from __future__ import annotations

import math
from xml.etree import ElementTree

import pytest
from eda_agent.placer.eval.score import evaluate
from eda_agent.placer.intent import infer, model, validate
from eda_agent.placer.io import schematic_input
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
from eda_agent.placer.place import (
    blocks,
    decap,
    escape,
    mechanical,
    pipeline,
    problem,
    rigid,
)
from eda_agent.placer.place.pipeline import _compare_floorplans, solve

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


def test_decap_refinement_moves_only_resolved_unlocked_capacitor():
    board = _board()
    board.footprints = [board.fp("U1"), board.fp("R1")]
    board.reindex()
    intent = {"parts": {"R1": {"role": model.claim("decap", "board", 1),
                               "target": model.claim({"ref": "U1", "net": "A", "pins": ["1"]}, "board", 1)}}}
    before = board.placement()
    result, report = decap.refine(board, intent, {"U1"})
    assert report["moved"] == ["R1"]
    assert report["after"]["p90_mm"] < report["before"]["p90_mm"]
    assert board.placement() == before
    assert result.fp("U1").position == board.fp("U1").position
    assert not evaluate(result, intent=intent)["legality"]["strict_violations"]
    locked, report = decap.refine(board, intent, {"U1", "R1"})
    assert locked is board and not report["trials"]
    rejected, report = decap.refine(board, intent, {"U1"}, preserve_escapes=lambda _: False)
    assert rejected is board and not report["moved"] and report["trials"] > 0


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


def test_floorplan_guard_protects_resolved_decap_tail():
    base = _board()
    candidate = base.copy()
    candidate.fp("R1").x = -50
    intent = {"parts": {"R1": {"role": model.claim("decap", "board", 1),
                               "target": model.claim({"ref": "U1", "net": "A"}, "board", 1)}}}
    legal = {"strict_violations": 0, "illegal_after_s5": [], "s4_failed": []}
    result = _compare_floorplans(candidate, legal, base, legal, intent)
    assert result["use_baseline"] and "decap_p90_mm" in result["regressions"]
    assert not _compare_floorplans(base, legal, base, legal, intent)["use_baseline"]


def test_floorplan_can_select_reservations_without_blocks(monkeypatch):
    board = _board()
    trial = escape.Escape("1", "A", (1, 0), (2, 0), 0.4, 0.6)
    monkeypatch.setattr(escape, "envelopes", lambda *_: {"U1": [trial]})
    monkeypatch.setattr(escape, "select_reservations", lambda exits: exits)
    calls = []

    def solve_variant(board, *, floorplan, reserve=False, **kwargs):
        assert kwargs["budget_divisor"] == 4
        calls.append((floorplan, reserve))
        placed = board.copy()
        placed.name = {(False, False): "baseline", (True, False): "blocks",
                       (False, True): "reserve", (True, True): "combined"}[floorplan, reserve]
        return placed, {"strict_violations": 0, "illegal_after_s5": [], "s4_failed": [], "seconds_s0_s5": 0, "fixed": {}}

    monkeypatch.setattr(pipeline, "_solve", solve_variant)
    monkeypatch.setattr(pipeline, "_floorplan_metrics", lambda b, _: {
        "crossings": 4 if b.name == "combined" else 3, "decap_p90_mm": 1.0, "resolved_decaps": 1})
    monkeypatch.setattr(escape, "audit", lambda b, *_, **kwargs: {
        "clear": int(b.name in ("reserve", "combined")), "total": 1})
    result, info = solve(board, intent={"parts": {}}, floorplan=True)
    assert calls == [(False, False), (True, False), (False, True), (True, True)]
    assert result.name == info["floorplan"]["selected_variant"] == "reserve"
    assert info["floorplan"]["comparisons"]["combined"]["regressions"] == ["crossings"]


def test_block_seed_respects_fixed_anchor_and_prioritizes_its_linked_part():
    board = _board()
    intent = {"parts": {"R1": {"role": model.claim("decap", "board", 1),
                               "target": model.claim({"ref": "U1", "net": "A"}, "board", 1)}},
              "blocks": [model.claim({"anchor": "U1", "members": ["U1", "R1"]}, "board", 1)]}
    pb = problem.build(board, intent=intent, floorplan=True)
    start = pb.poses()
    poses, priority, count = blocks.seed(pb, intent, start)
    assert count == 1 and priority == [0, 1]
    assert poses[0] == start[0] and poses[2:] == start[2:]
    assert math.dist(poses[1][:2], poses[0][:2]) < math.dist(start[1][:2], start[0][:2])


def test_rigid_collapse_preserves_member_geometry_and_pins_on_rotation():
    pb = problem.build(_board())
    start = pb.poses()
    start[1] = (5.0, 5.0, 1)
    start[2] = (8.0, 5.0, 0)
    macros = rigid.compile_macros(pb, start, {1: [2], 0: [3]})
    assert list(macros) == [1]  # The locked footprint stays a separate solver part.
    compact, retained = rigid.collapse(pb, macros)
    poses = [start[i] for i in retained]
    assert len(compact.parts) == len(pb.parts) - 1
    core = problem.rust_problem(compact)
    poses[retained.index(1)] = (10.0, 13.0, 2)
    core.set_positions(poses)
    full = rigid.expand(pb, core.positions(), retained, macros)
    assert full[1] == (10.0, 13.0, 2)
    assert full[2] == (10.0, 10.0, 1)
    compact_cost = problem.reference_cost(compact, poses)
    full_cost = problem.reference_cost(pb, full)
    assert math.isclose(compact_cost["total"], full_cost["total"])


def test_rigid_corridor_detects_obstructed_same_side_ports():
    board = _board()
    pb = problem.build(board)
    poses = pb.poses()
    poses[1:5] = [(4.0, 10.0, 0), (4.0, 12.0, 0), (16.0, 10.0, 0), (16.0, 12.0, 0)]
    macros = rigid.compile_macros(pb, poses, {1: [2], 3: [4]})
    assert len(macros) == 2
    from eda_agent.placer.macro import Port

    macros[1].ports = [Port("A", "pad", "F.Cu", 0, 0, 0, 0.5)]
    macros[3].ports = [Port("A", "pad", "F.Cu", 0, 0, 180, 0.5)]
    placed = problem.apply(pb, poses)
    result = rigid.corridors(placed, macros)
    assert result["total"] == 1 and result["clear"] == 0
    assert "U1" in result["rows"][0]["blocked_by"]
    # With an independent obstacle and no signal paths, a legal local move clears the strip.
    placed.footprints = [fp for fp in placed.footprints if fp.ref in {"U1", "R1", "R2", "R3", "R4"}]
    for fp in placed.footprints:
        fp.pads.clear()
    placed.reindex()
    improved, report = rigid.clear_blockers(placed, macros, set(), {}, rigid.corridors(placed, macros), max_trials=8)
    assert report["clear"] == 1 and report["moved"] == ["U1"]
    assert placed.fp("U1").position == (10.0, 10.0)
    assert improved.fp("R1").position == placed.fp("R1").position
    _, locked_report = rigid.clear_blockers(placed, macros, {"U1"}, {}, rigid.corridors(placed, macros), max_trials=8)
    assert locked_report["clear"] == 0 and locked_report["trials"] == 0

    # Clearing one obstacle should make progress even while another blocks the same strip.
    second = _fp("R9", 13.0, 10.0, [])
    placed.footprints.append(second)
    placed.reindex()
    _, partial = rigid.clear_blockers(placed, macros, {"R9"}, {}, rigid.corridors(placed, macros), max_trials=8)
    assert partial["clear"] == 0 and partial["moved"] == ["U1"]
    assert partial["before_blockers"] == 2 and partial["after_blockers"] == 1


def test_escape_reservation_selects_only_the_crowded_bank():
    def left(pin, y):
        return escape.Escape(pin, "A", (0, y), (-1, y), 0.3, 0.6)

    def right(pin, y):
        return escape.Escape(pin, "B", (2, y), (3, y), 0.3, 0.6)

    tight = [left("1", 0), left("2", 0.3)]
    assert escape.select_reservations({"U1": tight + [right("3", 0), right("4", 1)]}) == {"U1": tight}


def test_cold_placement_rejects_wiring_mismatch_and_does_not_infer_anchors_from_seed(tmp_path, monkeypatch):
    board = _board()
    board.outline = [Outline([(0, 0), (20, 0), (20, 20), (15, 20), (15, 5), (5, 5), (5, 20), (0, 20)])]
    sch = tmp_path / "test.kicad_sch"
    sch.write_text("(kicad_sch)", encoding="utf-8")
    root = ElementTree.Element("export")
    nets = ElementTree.SubElement(root, "nets")
    for name, pads in board.net_pads().items():
        net = ElementTree.SubElement(nets, "net", name=name)
        for fp, pad in pads:
            ElementTree.SubElement(net, "node", ref=fp.ref, pin=pad.number, pinfunction="PORT")
    monkeypatch.setattr(schematic_input, "read_netlist", lambda _: root)
    cold, report = schematic_input.cold_input(board, sch)
    assert problem.fixed_parts(cold) == problem.fixed_parts(board)
    assert cold.fp("R1").position != board.fp("R1").position
    assert cold.fp("R1").pads[0].function == "PORT"
    assert report["reset_poses"] > 0
    nets[0][0].set("pin", "wrong")
    with pytest.raises(ValueError, match="does not match PCB endpoints"):
        schematic_input.cold_input(board, sch)


def test_mechanical_constraints_preserve_pose_and_find_a_region_anchor():
    board = _board()
    board.fp("R1").rotation = 17
    board.rule_areas = [{"uuid": "slot", "layers": ["F.Cu"], "keepout": {},
                         "polygons": [[(1, 14), (6, 14), (6, 19), (1, 19)]]}]
    intent = infer.draft(board)
    intent["mechanical"] = {
        "R1": model.claim({"mode": "preserve"}, "board", 1),
        "R2": model.claim({"mode": "region", "region": "slot", "angles_deg": [90]}, "board", 1),
    }
    assert validate.validate(intent, board)["valid"]
    anchored, rows = mechanical.anchor(board, intent)
    assert anchored.fp("R1").position == board.fp("R1").position
    assert anchored.fp("R1").rotation == 17
    assert rows[1]["angle_deg"] == 90
    assert all(anchored.fp(ref).locked and not board.fp(ref).locked for ref in ("R1", "R2"))
    assert anchored.fp("R2").position == (3.5, 16.5)
    board.rule_areas[0]["layers"] = ["B.Cu"]
    assert not validate.validate(intent, board)["valid"]
    board.rule_areas[0]["layers"] = ["F.Cu"]
    board.rule_areas[0]["keepout"]["footprints"] = True
    assert not validate.validate(intent, board)["valid"]
    with pytest.raises(ValueError, match="missing usable placement region"):
        mechanical.anchor(board, intent)


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
