# SPDX-License-Identifier: MIT
"""Unit tests of the Board IR derivations and the evaluation, on synthetic boards (no KiCad)."""

from __future__ import annotations

from eda_agent.placer.eval import legality, metrics
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
    find_diff_pairs,
    plane_zone_layers,
)

SQUARE = [(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)]


def _fp(ref, x, y, side="F", nets=("A", "B"), rot=0.0, locked=False):
    pads = [Pad(number=str(i + 1), net=n, local=(-0.5 + i, 0.0), size=(0.5, 0.5), layers=("F.Cu",))
            for i, n in enumerate(nets)]
    return Footprint(ref=ref, value="v", lib_id="lib:x", x=x, y=y, rotation=rot, side=side, locked=locked,
                     pads=pads, courtyard=Courtyard("file", [SQUARE]), uuid="u-" + ref)


def _board(fps, layers=2, fills=None):
    names = ["F.Cu"] + [f"In{i}.Cu" for i in range(1, layers - 1)] + ["B.Cu"]
    b = Board(name="t", footprints=fps, outline=[Outline([(0, 0), (20, 0), (20, 20), (0, 20)])],
              board_area_mm2=400.0,
              copper_layers=[CopperLayer(n, n, i) for i, n in enumerate(names)],
              netclasses=[NetClass("Default", 0.2, 0.2, 0.6, 0.3)],
              nets=[Net(n, "Default", 2) for n in ("A", "B", "GND")],
              zone_fill_by_layer_mm2=fills or {})
    return derive(b)


def test_layer_roles_from_fill_fraction():
    b = _board([_fp("R1", 5, 5)], layers=4,
               fills={"In1.Cu": {"GND": 300.0}, "In2.Cu": {"+5V": 150.0, "+3V3": 100.0},
                      "F.Cu": {"GND": 200.0, "SIG": 4.0}, "B.Cu": {"X": 2.0}})
    roles = {r.layer: (r.role, r.net) for r in b.stackup.roles}
    assert roles == {"F.Cu": ("mixed", "GND"), "In1.Cu": ("plane", "GND"), "In2.Cu": ("mixed", "+5V"),
                     "B.Cu": ("signal", None)}
    assert b.stackup.policy == "locked"


def test_board_without_pour_needs_a_stackup_decision():
    b = _board([_fp("R1", 5, 5)])
    assert b.stackup.policy == "needs_decision"
    assert any(u.startswith("stackup_policy") for u in b.unknowns)


def test_plane_zone_layers_keeps_only_plane_layers_of_multilayer_zones():
    b = _board([_fp("R1", 5, 5)], layers=4, fills={"In1.Cu": {"GND": 300.0}, "In2.Cu": {"VDD": 300.0}})
    b.zones = [{"uuid": "z1", "net": "GND", "layers": ["F.Cu", "In1.Cu", "B.Cu"]},
               {"uuid": "z2", "net": "SIG", "layers": ["In2.Cu"]},
               {"uuid": "z3", "net": "VDD", "layers": ["In2.Cu"]}]
    assert plane_zone_layers(b) == {"z1": ["In1.Cu"], "z3": ["In2.Cu"]}


def test_diff_pairs_by_name():
    pairs = find_diff_pairs(["/D+", "/D-", "USB_P", "USB_N", "CLK", "A+"])
    assert [(p.positive, p.negative) for p in pairs] == [("/D+", "/D-"), ("USB_P", "USB_N")]


def test_legality_finds_overlap_outline_and_honours_waiver_and_fixed():
    fps = [_fp("R1", 5, 5), _fp("R2", 5.5, 5), _fp("R3", 5.5, 5, side="B"), _fp("R4", 19.5, 10),
           _fp("H1", 10, 10, locked=True), _fp("H2", 10.5, 10, locked=True)]
    b = _board(fps)
    s = legality.summary(legality.check(b))
    assert s["by_kind"] == {"courtyard_overlap": 1, "outside_outline": 1}
    assert s["fixed_only"] == 1
    b.drc_severities = {"courtyards_overlap": "ignore"}
    s = legality.summary(legality.check(b))
    assert s["by_kind"] == {"outside_outline": 1} and s["waived"] == 1 and s["strict_violations"] == 2


def test_mst_and_crossings():
    assert len(metrics.mst_edges([(0, 0), (1, 0), (5, 0)])) == 2
    # nets A (0,0)-(10,10) and B (0,10)-(10,0) cross once; on a plane net they would be ignored
    fps = [_fp("R1", 2, 2, nets=("A", "B")), _fp("R2", 12, 12, nets=("B", "A"))]
    b = _board(fps)
    assert metrics.crossings(b)["count"] == 1


def test_evaluate_reports_uncalibrated_pro_score_without_lint():
    e = evaluate(_board([_fp("R1", 5, 5), _fp("R2", 12, 12)]))
    ps = e["pro_score"]
    assert ps["calibrated"] is False and "lint" not in ps["weights_used"]
    assert 0 <= ps["score"] <= 100
