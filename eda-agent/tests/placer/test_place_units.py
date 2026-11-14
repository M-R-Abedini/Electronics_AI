# SPDX-License-Identifier: MIT
"""Placer S1-S5 on a synthetic board (Rust core + numpy; no KiCad)."""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from xml.etree import ElementTree

import pytest
from eda_agent.placer.eval.score import evaluate
from eda_agent.placer.intent import infer, model, validate
from eda_agent.placer.io import schematic_input
from eda_agent.placer.io.kicad import read_board
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
from eda_agent.placer.mcp import PlacerService
from eda_agent.placer.place import (
    blocks,
    decap,
    escape,
    mechanical,
    pipeline,
    problem,
    rigid,
)
from eda_agent.placer.place.pipeline import _compare_floorplans, place_board, solve
from eda_agent.placer.toolchain import ToolchainError, kicad

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


def _kicad_cluster_fixture(board_path: Path) -> Path:
    """Build a filled KiCad board with a non-GND +3V3 pour, footprint keepout, track, and crossed signals."""
    try:
        kc = kicad()
    except ToolchainError as exc:
        pytest.skip(str(exc))
    script = """import sys, pcbnew
out = sys.argv[1]
b = pcbnew.NewBoard(out)
b.GetDesignSettings().SetCopperLayerCount(2)
rect = pcbnew.PCB_SHAPE(b, pcbnew.SHAPE_T_RECT)
rect.SetLayer(pcbnew.Edge_Cuts)
rect.SetStart(pcbnew.VECTOR2I(0, 0))
rect.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(30), pcbnew.FromMM(20)))
b.Add(rect)
nets = {}
for name in ('+3V3', 'SIG_A', 'SIG_B'):
    net = pcbnew.NETINFO_ITEM(b, name)
    b.Add(net)
    nets[name] = net
lset = pcbnew.LSET()
lset.AddLayer(pcbnew.F_Cu)
parts = (
    ('U1', 5.0, 5.0, ('+3V3', 'SIG_A'), True),
    ('R1', 5.0, 15.0, ('+3V3', 'SIG_B'), False),
    ('R2', 25.0, 5.0, ('+3V3', 'SIG_B'), False),
    ('R3', 25.0, 15.0, ('+3V3', 'SIG_A'), False),
)
for ref, x, y, pnets, locked in parts:
    fp = pcbnew.FOOTPRINT(b)
    fp.SetReference(ref)
    for i, netname in enumerate(pnets):
        pad = pcbnew.PAD(fp)
        pad.SetNumber(str(i + 1))
        pad.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(-0.6 + 1.2 * i), 0))
        pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(0.5), pcbnew.FromMM(0.6)))
        pad.SetLayerSet(lset)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetNet(nets[netname])
        fp.Add(pad)
    cy = pcbnew.PCB_SHAPE(fp, pcbnew.SHAPE_T_RECT)
    cy.SetLayer(pcbnew.F_CrtYd)
    cy.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(-1.0), pcbnew.FromMM(-0.6)))
    cy.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(1.0), pcbnew.FromMM(0.6)))
    fp.Add(cy)
    fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
    fp.SetLocked(locked)
    b.Add(fp)
zone = pcbnew.ZONE(b)
zone.SetLayer(pcbnew.B_Cu)
zone.SetNet(nets['+3V3'])
zone.SetAssignedPriority(3)
zo = zone.Outline()
zo.NewOutline()
for px, py in ((1, 1), (29, 1), (29, 19), (1, 19)):
    zo.Append(pcbnew.FromMM(px), pcbnew.FromMM(py))
b.Add(zone)
keepout = pcbnew.ZONE(b)
keepout.SetIsRuleArea(True)
keepout.SetLayer(pcbnew.F_Cu)
keepout.SetDoNotAllowFootprints(True)
ko = keepout.Outline()
ko.NewOutline()
for px, py in ((15, 14), (19, 14), (19, 19), (15, 19)):
    ko.Append(pcbnew.FromMM(px), pcbnew.FromMM(py))
b.Add(keepout)
region = pcbnew.ZONE(b)
region.SetIsRuleArea(True)
region.SetLayer(pcbnew.F_Cu)
region.SetDoNotAllowFootprints(False)
region.SetDoNotAllowTracks(False)
region.SetDoNotAllowVias(False)
region.SetDoNotAllowPads(False)
region.SetDoNotAllowZoneFills(False)
ro = region.Outline()
ro.NewOutline()
for px, py in ((10, 14), (14, 14), (14, 19), (10, 19)):
    ro.Append(pcbnew.FromMM(px), pcbnew.FromMM(py))
b.Add(region)
track = pcbnew.PCB_TRACK(b)
track.SetLayer(pcbnew.F_Cu)
track.SetNet(nets['SIG_A'])
track.SetWidth(pcbnew.FromMM(0.25))
track.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(5), pcbnew.FromMM(5)))
track.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(25), pcbnew.FromMM(15)))
b.Add(track)
pcbnew.ZONE_FILLER(b).Fill(b.Zones())
pcbnew.SaveBoard(out, b)
"""
    subprocess.run([str(kc.python), "-I", "-c", script, str(board_path)], check=True)
    return board_path


def test_place_cluster_preserves_power_zone_and_keepout_and_clears_old_fill(tmp_path):
    board_path = _kicad_cluster_fixture(tmp_path / "cluster_power_zone.kicad_pcb")
    reference = read_board(board_path)
    assert reference.zones[0]["net"] == "+3V3" and reference.zones[0]["filled"] is True
    assert reference.routing_items == 1 and len(reference.rule_areas) == 2

    res = PlacerService(tmp_path / "runs").place_cluster(str(board_path), ["R1", "R2"], seed=0)
    rep = json.loads(Path(res["run_report"]).read_text(encoding="utf-8"))
    assert res["passed"] is True
    assert res["zones_require_refill"] is True
    assert rep["stages"]["place"]["zones_require_refill"] is True
    cluster_info = rep["stages"]["place"]["cluster"]
    assert cluster_info["zones_preserved"] == 1
    assert cluster_info["input_zone_fill_by_layer_mm2"]["B.Cu"]["+3V3"] > 0.0

    reloaded = read_board(res["placed_board"])
    assert len(reloaded.zones) == len(reference.zones) == 1
    z_out, z_ref = reloaded.zones[0], reference.zones[0]
    assert (z_out["uuid"], z_out["net"], z_out["layers"], z_out["priority"], z_out["outline"]) == (
        z_ref["uuid"], z_ref["net"], z_ref["layers"], z_ref["priority"], z_ref["outline"])
    assert z_out["filled"] is False
    assert all(area == 0.0 for area in z_out["filled_area_mm2"].values())
    assert all(area == 0.0 for layers in reloaded.zone_fill_by_layer_mm2.values() for area in layers.values())
    assert reloaded.rule_areas == reference.rule_areas
    assert reloaded.routing_items == 0
    for ref in ("U1", "R3"):
        out_fp, ref_fp = reloaded.fp(ref), reference.fp(ref)
        assert (out_fp.position, out_fp.rotation, out_fp.side, out_fp.locked) == (
            ref_fp.position, ref_fp.rotation, ref_fp.side, ref_fp.locked)


def test_place_cluster_identity_pose_matches_reference_hpwl_and_crossings_with_power_plane(tmp_path, monkeypatch):
    board_path = _kicad_cluster_fixture(tmp_path / "cluster_identity.kicad_pcb")

    def noop_solve(board, *, side_policy="expert", mode="best", seed=0, intent=None, floorplan=False):
        fixed = problem.fixed_parts(board)
        return board.copy(), {
            "mode": mode, "side_policy": side_policy, "seed": seed, "movable": 2, "fixed": fixed,
            "s4_failed": [], "illegal_after_s5": [], "strict_violations": 0, "seconds_s0_s5": 0.01,
            "mechanical_anchors": [],
            "floorplan": {"requested": floorplan, "selected_variant": "baseline",
                          "escape": escape.audit(board, escape.envelopes(board, intent or {},
                                                                         {fp.ref: problem.local_box(fp)
                                                                          for fp in board.footprints}))},
        }

    monkeypatch.setattr(pipeline, "solve", noop_solve)
    rep = place_board(board_path, out_dir=tmp_path / "out", route=False, floorplan=True, cluster_refs=["R1", "R2"])
    assert rep["metrics"]["hpwl"] == rep["reference_evaluation"]["metrics"]["hpwl"]
    assert rep["metrics"]["crossings"] == rep["reference_evaluation"]["metrics"]["crossings"]
    assert rep["metrics"]["crossings"]["count"] == 1
    assert rep["metrics"]["hpwl"]["signal_mm"] < rep["metrics"]["hpwl"]["all_mm"]


def test_place_cluster_validation_and_mechanical_constraints(tmp_path):
    board_path = _kicad_cluster_fixture(tmp_path / "cluster_mech.kicad_pcb")
    reference = read_board(board_path)

    with pytest.raises(ValueError, match="route=False"):
        place_board(board_path, out_dir=tmp_path / "r1", route=True, cluster_refs=["R1"])
    with pytest.raises(ValueError, match="existing footprint"):
        place_board(board_path, out_dir=tmp_path / "r2", route=False, cluster_refs=["NO_SUCH_REF"])
    with pytest.raises(ValueError, match="no movable footprint"):
        place_board(board_path, out_dir=tmp_path / "r3", route=False, cluster_refs=["U1"])

    intent_preserve = infer.draft(reference)
    intent_preserve["mechanical"] = {"R1": model.claim({"mode": "preserve"}, "board", 1)}
    p_preserve = tmp_path / "preserve.json"
    model.dump(intent_preserve, p_preserve)
    with pytest.raises(ValueError, match="no movable footprint"):
        place_board(board_path, out_dir=tmp_path / "r5", route=False, intent_path=p_preserve, cluster_refs=["R1"])

    region_id = next(area["uuid"] for area in reference.rule_areas if not area["keepout"]["footprints"])

    intent_outside_region = infer.draft(reference)
    intent_outside_region["mechanical"] = {
        "R3": model.claim({"mode": "region", "region": region_id, "angles_deg": [0]}, "board", 1),
    }
    p_outside = tmp_path / "outside_region.json"
    model.dump(intent_outside_region, p_outside)
    with pytest.raises(ValueError, match="relocate an outside"):
        place_board(board_path, out_dir=tmp_path / "r4", route=False, intent_path=p_outside, cluster_refs=["R1"])

    intent_reloc = infer.draft(reference)
    intent_reloc["mechanical"] = {
        "R1": model.claim({"mode": "region", "region": region_id, "angles_deg": [0]}, "board", 1),
    }
    p_reloc = tmp_path / "reloc.json"
    model.dump(intent_reloc, p_reloc)
    rep_reloc = place_board(board_path, out_dir=tmp_path / "r6", route=False, floorplan=True,
                            intent_path=p_reloc, cluster_refs=["R1", "R2"])
    reloc_out = read_board(rep_reloc["artifacts"]["placed_board"])
    assert 10.0 <= reloc_out.fp("R1").x <= 14.0 and 14.0 <= reloc_out.fp("R1").y <= 19.0
    assert reloc_out.fp("R1").locked == reference.fp("R1").locked
    assert reloc_out.fp("R3").position == reference.fp("R3").position
