# SPDX-License-Identifier: MIT
"""Unit tests of the Board IR derivations and the evaluation, on synthetic boards (no KiCad)."""

from __future__ import annotations

from eda_agent.placer.eval import legality, lint, metrics
from eda_agent.placer.eval.score import evaluate
from eda_agent.placer.intent import model
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


def test_evaluate_reports_calibrated_pro_score_without_lint():
    e = evaluate(_board([_fp("R1", 5, 5), _fp("R2", 12, 12)]))
    ps = e["pro_score"]
    assert ps["version"] == 1 and ps["calibrated"] is True and "lint" not in ps["weights_used"]
    assert 0 <= ps["score"] <= 100


def test_inner_plane_carrying_other_nets_is_mixed_and_not_a_power_layer():
    from eda_agent.placer.ir import power_layers

    b = _board([_fp("R1", 5, 5)], layers=4, fills={"In1.Cu": {"GND": 300.0}, "In2.Cu": {"VDD": 300.0}})
    b.routing = {"tracks": [{"layer": "In2.Cu", "net": "SIG", "points": [[0, 0], [10, 0]], "width": 0.2}]}
    derive(b)
    assert {r.layer: r.role for r in b.stackup.roles}["In2.Cu"] == "mixed"
    assert power_layers(b) == ["In1.Cu"]


def test_lint_reference_split_and_pro_score_activation():
    """Fast net crossing a split in the adjacent reference plane triggers a critical lint error."""
    b = _board([_fp("U1", 3, 10, nets=("USB_DP", "GND")), _fp("J1", 17, 10, nets=("USB_DP", "GND"))], layers=4,
               fills={"In1.Cu": {"GND": 320.0}})
    # Split GND zone on In1.Cu with a 2 mm vertical gap from x=9 to x=11
    b.zones = [
        {"uuid": "z1", "net": "GND", "layers": ["In1.Cu"], "filled": True,
         "filled_area_mm2": {"In1.Cu": 160.0}, "outline": [[(0.5, 0.5), (9.0, 0.5), (9.0, 19.5), (0.5, 19.5)]]},
        {"uuid": "z2", "net": "GND", "layers": ["In1.Cu"], "filled": True,
         "filled_area_mm2": {"In1.Cu": 160.0}, "outline": [[(11.0, 0.5), (19.5, 0.5), (19.5, 19.5), (11.0, 19.5)]]},
    ]
    b.routing = {
        "tracks": [{"layer": "F.Cu", "net": "USB_DP", "width": 0.2, "points": [[2.5, 10.0], [16.5, 10.0]]}],
        "vias": [],
    }
    intent = {"parts": {}, "nets": {"USB_DP": {"class": model.claim("diff_pair", "user", 1.0)}}}
    rep = lint.audit(b, intent)
    assert rep["has_copper"] is True
    assert rep["critical_errors"] == 1
    assert rep["findings"][0]["rule"] == "reference_split_crossing"
    assert rep["score"] < 100.0

    # With a continuous plane, the split crossing disappears and Pro Score includes lint weight 10
    b.zones = [
        {"uuid": "z_full", "net": "GND", "layers": ["In1.Cu"], "filled": True,
         "filled_area_mm2": {"In1.Cu": 360.0}, "outline": [[(0.5, 0.5), (19.5, 0.5), (19.5, 19.5), (0.5, 19.5)]]},
    ]
    ev = evaluate(b, intent=intent)
    assert ev["metrics"]["lint"]["critical_errors"] == 0
    assert ev["metrics"]["lint"]["score"] == 100.0
    assert ev["pro_score"]["weights_used"]["lint"] == 10


def test_lint_return_via_and_stitching_cap():
    """Fast layer transition checks nearby GND return via and GND<->PWR stitching capacitor."""
    b = _board([_fp("U1", 3, 10, nets=("CLK", "GND")), _fp("U2", 17, 10, nets=("CLK", "GND"))], layers=4,
               fills={"In1.Cu": {"GND": 360.0}, "In2.Cu": {"+3V3": 360.0}})
    b.zones = [
        {"uuid": "z1", "net": "GND", "layers": ["In1.Cu"], "filled": True,
         "filled_area_mm2": {"In1.Cu": 360.0}, "outline": [[(0.5, 0.5), (19.5, 0.5), (19.5, 19.5), (0.5, 19.5)]]},
        {"uuid": "z2", "net": "+3V3", "layers": ["In2.Cu"], "filled": True,
         "filled_area_mm2": {"In2.Cu": 360.0}, "outline": [[(0.5, 0.5), (19.5, 0.5), (19.5, 19.5), (0.5, 19.5)]]},
    ]
    b.routing = {
        "tracks": [
            {"layer": "F.Cu", "net": "CLK", "width": 0.2, "points": [[2.5, 10.0], [10.0, 10.0]]},
            {"layer": "B.Cu", "net": "CLK", "width": 0.2, "points": [[10.0, 10.0], [16.5, 10.0]]},
        ],
        "vias": [{"x": 10.0, "y": 10.0, "diameter": 0.6, "drill": 0.3, "net": "CLK", "layers": ["F.Cu", "B.Cu"]}],
    }
    intent = {"parts": {}, "nets": {"CLK": {"class": model.claim("clock", "user", 1.0)}}}
    rep = lint.audit(b, intent)
    assert any(f["rule"] == "reference_change_stitch_cap" for f in rep["findings"])

    # Add a stitching capacitor connecting GND and +3V3 within 2 mm of the transition via -> warning clears
    b.footprints.append(_fp("C1", 11.0, 10.5, nets=("+3V3", "GND")))
    intent["parts"]["C1"] = {"role": model.claim("decap", "user", 1.0)}
    rep_ok = lint.audit(b, intent)
    assert not any(f["rule"] == "reference_change_stitch_cap" for f in rep_ok["findings"])


def test_lint_crystal_keepout_and_fb_vs_sw():
    """Unrelated track under crystal and FB track near SW / under inductor trigger critical lint findings."""
    b = _board([
        _fp("Y1", 10, 5, nets=("XTAL1", "XTAL2")),
        _fp("L1", 10, 14, nets=("SW", "VOUT")),
    ], layers=2)
    b.routing = {
        "tracks": [
            {"layer": "F.Cu", "net": "NOISY", "width": 0.2, "points": [[8.0, 5.0], [12.0, 5.0]]},
            {"layer": "F.Cu", "net": "SW", "width": 0.4, "points": [[6.0, 12.0], [10.0, 12.0]]},
            {"layer": "F.Cu", "net": "FB", "width": 0.2, "points": [[6.0, 12.4], [12.0, 14.0]]},
        ],
        "vias": [],
    }
    intent = {
        "parts": {
            "Y1": {"role": model.claim("crystal", "user", 1.0)},
            "L1": {"role": model.claim("converter_inductor", "user", 1.0)},
        },
        "nets": {
            "SW": {"class": model.claim("sw_node", "user", 1.0)},
            "FB": {"class": model.claim("feedback", "user", 1.0)},
        },
    }
    rep = lint.audit(b, intent)
    rules = {f["rule"] for f in rep["findings"] if f["severity"] == "critical"}
    assert "signal_under_crystal" in rules
    assert "fb_near_sw" in rules
    assert "fb_under_inductor" in rules


def test_lint_two_layer_gnd_fragmentation_and_decap_via_between():
    """2-layer B.Cu GND split into two islands and via between IC & same-side decap are caught."""
    b = _board([
        _fp("U1", 5, 10, nets=("+3V3", "GND")),
        _fp("C1", 9, 10, nets=("+3V3", "GND")),
    ], layers=2, fills={"B.Cu": {"GND": 350.0}})
    b.zones = [
        {"uuid": "zg", "net": "GND", "layers": ["B.Cu"], "filled": True,
         "filled_area_mm2": {"B.Cu": 350.0}, "outline": [[(0.5, 0.5), (19.5, 0.5), (19.5, 19.5), (0.5, 19.5)]]},
    ]
    b.routing = {
        "tracks": [
            # Full-height B.Cu track splits B.Cu GND into left & right halves
            {"layer": "B.Cu", "net": "SIG_CUT", "width": 0.4, "points": [[10.0, 0.0], [10.0, 20.0]]},
            # +3V3 routes from U1 to C1 via a bottom-layer hop (via between IC and decap)
            {"layer": "F.Cu", "net": "+3V3", "width": 0.25, "points": [[4.5, 10.0], [6.0, 10.0]]},
            {"layer": "B.Cu", "net": "+3V3", "width": 0.25, "points": [[6.0, 10.0], [8.5, 10.0]]},
        ],
        "vias": [
            {"x": 6.0, "y": 10.0, "diameter": 0.6, "drill": 0.3, "net": "+3V3", "layers": ["F.Cu", "B.Cu"]},
            {"x": 8.5, "y": 10.0, "diameter": 0.6, "drill": 0.3, "net": "+3V3", "layers": ["F.Cu", "B.Cu"]},
        ],
    }
    intent = {
        "parts": {
            "C1": {
                "role": model.claim("decap", "user", 1.0),
                "target": model.claim({"ref": "U1", "net": "+3V3", "pins": ["1"]}, "user", 1.0),
            }
        }
    }
    rep = lint.audit(b, intent)
    gnd = rep["checks"]["two_layer_gnd_continuity"]
    assert gnd["applicable"] is True
    assert gnd["island_count"] >= 2
    assert gnd["largest_island_fraction"] < 0.65
    assert rep["checks"]["decap_loop"]["via_between_count"] == 1
    assert any(f["rule"] == "two_layer_gnd_fragmented" for f in rep["findings"])
    assert any(f["rule"] == "decap_loop" for f in rep["findings"])


def test_drc_is_routing_item_excludes_silkscreen_and_mask_arcs():
    from eda_agent.placer.route import drc

    assert drc.is_routing_item("Track [+3.3V] on F.Cu, length 1.2 mm") is True
    assert drc.is_routing_item("Arc [USB_DP] on B.Cu, length 0.8 mm") is True
    assert drc.is_routing_item("Via [GND] on F.Cu - B.Cu") is True
    assert drc.is_routing_item("Arc of U2 on F.Silkscreen") is False
    assert drc.is_routing_item("Arc of LOGO4 on B.Mask") is False


def test_legality_catches_movable_pad_on_freestanding_mask_aperture():
    b = _board([_fp("R1", 5.0, 5.0, side="F", nets=("A", "B"))], layers=2)
    b.obstacles = [
        {
            "uuid": "mask-logo", "kind": "mask_aperture", "source": "board",
            "layer": "F.Mask", "copper": "F.Cu", "net": None, "overlapping_pads": [],
            "outlines": [[(4.0, 4.0), (6.0, 4.0), (6.0, 6.0), (4.0, 6.0)]],
        },
        {
            "uuid": "jp-window", "kind": "mask_aperture", "source": "footprint:JP1",
            "layer": "F.Mask", "copper": "F.Cu", "net": None, "overlapping_pads": ["JP1-1", "JP1-2"],
            "outlines": [[(4.0, 4.0), (6.0, 4.0), (6.0, 6.0), (4.0, 6.0)]],
        },
    ]
    findings = legality.check(b, fixed=set())
    mask_hits = [f for f in findings if f.detail.startswith("mask_aperture:")]
    assert len(mask_hits) == 1
    assert mask_hits[0].refs == ["R1"] and mask_hits[0].detail == "mask_aperture:mask-logo"


