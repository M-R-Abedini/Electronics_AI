# SPDX-License-Identifier: MIT
"""Phase 3 unit tests: Hartley routing, SI/PI, VIPPO/DRU hardening, surgical S6a, and opt-in S6b."""

from __future__ import annotations

import math
from pathlib import Path

from eda_agent.placer import impedance, silkscreen
from eda_agent.placer.eval import lint
from eda_agent.placer.ir import (
    Board,
    CopperLayer,
    Courtyard,
    Footprint,
    Net,
    NetClass,
    Outline,
    Pad,
    Stackup,
    StackupLayer,
    derive,
)
from eda_agent.placer.route import dru, dsn, fanout, pairs, s6a, s6b


def _make_4l_board(*, with_planes: bool = True, stackup_source: str = "file") -> Board:
    layers = [
        CopperLayer("F.Cu", "F.Cu", 0, "signal"),
        CopperLayer("In1.Cu", "In1.Cu", 1, "power"),
        CopperLayer("In2.Cu", "In2.Cu", 2, "power"),
        CopperLayer("B.Cu", "B.Cu", 3, "signal"),
    ]
    u1 = Footprint(
        "U1", "MCU", "QFN-16_EP", 20.0, 15.0, 0.0, "F", locked=True,
        pads=[
            Pad("1", "USB_DP", (-3.0, -0.5), (0.4, 0.4), layers=("F.Cu",)),
            Pad("2", "USB_DN", (-3.0, 0.5), (0.4, 0.4), layers=("F.Cu",)),
            Pad("3", "VCC_3V3", (3.0, -1.0), (0.4, 0.4), layers=("F.Cu",)),
            Pad("4", "SIG1", (3.0, 0.0), (0.4, 0.4), layers=("F.Cu",)),
            Pad("5", "SIG2", (3.0, 1.0), (0.4, 0.4), layers=("F.Cu",)),
            Pad("6", "GND", (-1.0, 3.0), (0.4, 0.4), layers=("F.Cu",)),
            Pad("7", "GND", (0.0, 3.0), (0.4, 0.4), layers=("F.Cu",)),
            Pad("8", "GND", (1.0, 3.0), (0.4, 0.4), layers=("F.Cu",)),
            Pad("9", "GND", (0.0, 0.0), (3.2, 3.2), layers=("F.Cu",)),  # QFN EP thermal pad
        ],
        courtyard=Courtyard("file", [[(-3.6, -3.6), (3.6, -3.6), (3.6, 3.6), (-3.6, 3.6)]]),
        uuid="u-U1",
    )
    c_far = Footprint(
        "C1", "100nF", "C_0402", 34.0, 15.0, 0.0, "F", locked=False,
        pads=[
            Pad("1", "VCC_3V3", (-0.5, 0.0), (0.5, 0.5), layers=("F.Cu",)),
            Pad("2", "GND", (0.5, 0.0), (0.5, 0.5), layers=("F.Cu",)),
        ],
        courtyard=Courtyard("file", [[(-1.0, -0.5), (1.0, -0.5), (1.0, 0.5), (-1.0, 0.5)]]),
        uuid="u-C1",
    )
    zones = []
    fill_map = {}
    if with_planes:
        zones = [
            {
                "uuid": "z-gnd",
                "net": "GND",
                "layers": ["In1.Cu"],
                "filled": True,
                "filled_area_mm2": {"In1.Cu": 580.0},
                "outline": [[(0.5, 0.5), (20.0, 0.5), (20.0, 29.5), (0.5, 29.5)]],
            },
            {
                "uuid": "z-3v3-split",
                "net": "VCC_3V3",
                "layers": ["In1.Cu"],
                "filled": True,
                "filled_area_mm2": {"In1.Cu": 560.0},
                "outline": [[(20.5, 0.5), (39.5, 0.5), (39.5, 29.5), (20.5, 29.5)]],
            },
            {
                "uuid": "z-pwr",
                "net": "VCC_3V3",
                "layers": ["In2.Cu"],
                "filled": True,
                "filled_area_mm2": {"In2.Cu": 1100.0},
                "outline": [[(0.5, 0.5), (39.5, 0.5), (39.5, 29.5), (0.5, 29.5)]],
            },
        ]
        fill_map = {"In1.Cu": {"GND": 580.0, "VCC_3V3": 560.0}, "In2.Cu": {"VCC_3V3": 1100.0}}

    stack_layers = [
        StackupLayer("F.Cu", "copper", thickness_mm=0.035),
        StackupLayer("dielectric 1", "prepreg", thickness_mm=0.11, epsilon_r=4.2),
        StackupLayer("In1.Cu", "copper", thickness_mm=0.0175),
        StackupLayer("dielectric 2", "core", thickness_mm=1.20, epsilon_r=4.5),
        StackupLayer("In2.Cu", "copper", thickness_mm=0.0175),
        StackupLayer("dielectric 3", "prepreg", thickness_mm=0.11, epsilon_r=4.2),
        StackupLayer("B.Cu", "copper", thickness_mm=0.035),
    ] if stackup_source == "file" else []

    b = Board(
        name="p3_board",
        footprints=[u1, c_far],
        outline=[Outline([(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)])],
        board_area_mm2=1200.0,
        copper_layers=layers,
        netclasses=[NetClass("Default", 0.15, 0.15, 0.50, 0.25)],
        nets=[
            Net("GND", "Default", 5),
            Net("VCC_3V3", "Default", 2),
            Net("USB_DP", "Default", 2),
            Net("USB_DN", "Default", 2),
            Net("SIG1", "Default", 1),
            Net("SIG2", "Default", 1),
        ],
        zones=zones,
        zone_fill_by_layer_mm2=fill_map,
        stackup=Stackup(stackup_source, stack_layers, []),
        rules={
            "min_track_width": 0.10,
            "min_clearance": 0.10,
            "min_via_diameter": 0.45,
            "min_through_drill": 0.20,
            "min_via_annular_width": 0.10,
            "hole_clearance": 0.20,
            "hole_to_hole": 0.20,
            "copper_edge_clearance": 0.30,
        },
    )
    derive(b)
    return b


def test_dru_extracts_annular_silk_and_netclass_pairs(tmp_path: Path):
    dru_file = tmp_path / "fab.kicad_dru"
    dru_file.write_text(
        '(version 1)\n'
        '(rule "Via annular"\n'
        '  (condition "A.Type == \'via\'")\n'
        '  (constraint annular_width (min 0.13mm)))\n'
        '(rule "Silk height"\n'
        '  (layer "?.Silkscreen")\n'
        '  (constraint text_height (min 1.05mm))\n'
        '  (constraint text_thickness (min 0.17mm)))\n'
        '(rule "Silk clearance"\n'
        '  (constraint silk_clearance (min 0.25mm)))\n'
        '(rule "HV pair"\n'
        '  (condition "A.NetClass == \'HV\' && B.NetClass == \'Default\'")\n'
        '  (constraint clearance (min 0.4mm)))\n',
        encoding="utf-8",
    )
    eff = dru.extract_effective_rules(dru_file)
    assert eff["min_via_annular_width_mm"] == 0.13
    assert eff["min_silk_text_height_mm"] == 1.05
    assert eff["min_silk_text_thickness_mm"] == 0.17
    assert eff["silk_clearance_mm"] == 0.25

    b = _make_4l_board()
    cfg = silkscreen.build_config(b, dru_rules=eff)
    assert cfg["min_height_mm"] >= 1.05
    assert cfg["min_stroke_mm"] >= 0.17
    assert cfg["min_pad_clearance_mm"] >= 0.25

    sample_dsn = (
        "(pcb board\n  (resolution um 10)\n  (unit um)\n"
        "  (structure (rule (width 150) (clearance 150)))\n"
        "  (network\n"
        "    (class kicad_default \"\" SIG1 (rule (width 150) (clearance 150)))\n"
        "    (class HV \"\" SIG2 (rule (width 150) (clearance 150)))\n"
        "  )\n  (wiring)\n)\n"
    )
    out_dsn, meta = dru.compile_dru_to_dsn(sample_dsn, dru_file, b)
    assert "Via annular" in meta["translated"]
    assert "HV pair" in meta["translated"]
    assert "(clearance 400)" in out_dsn
    # Silkscreen rules stay in untranslated for DSN P5 honesty while flowing into effective_rules.
    assert "Silk height" in meta["untranslated"]
    assert meta["effective_rules"]["min_silk_text_height_mm"] == 1.05


def test_vippo_hole_clearance_protects_through_vias_without_via_smd():
    sample_dsn = (
        "(pcb board\n  (resolution um 10)\n  (unit um)\n"
        "  (structure\n    (rule (width 100) (clearance 100))\n  )\n"
        "  (network)\n  (wiring)\n)\n"
    )
    out_dsn, applied = dsn.enforce_via_hole_clearances(
        sample_dsn,
        hole_clearance_mm=0.25,
        hole_to_hole_mm=0.25,
        via_diameter_mm=0.30,
        via_drill_mm=0.15,
        copper_clearance_mm=0.10,
        skip_via_smd=True,
    )
    assert "(type via_wire)" in out_dsn
    assert "(type via_pin)" in out_dsn
    assert "(type via_via)" in out_dsn
    assert "(type via_smd)" not in out_dsn
    assert "via_wire_mm" in applied and "via_smd_mm" not in applied


def test_impedance_inverse_solver_and_stackup_fallback():
    sol = impedance.solve_width_for_target(
        "surface_microstrip", 50.0, H1=0.11, T1=0.035, Er1=4.2, w_min=0.08, w_max=0.60,
    )
    assert sol is not None
    assert abs(sol["achieved_ohms"] - 50.0) <= 0.6
    assert 0.14 <= sol["width_mm"] <= 0.25

    b_file = _make_4l_board(stackup_source="file")
    pair_dims = impedance.pair_rule_from_stackup(b_file, "F.Cu", target_zdiff=90.0)
    assert pair_dims is not None
    assert 0.08 <= pair_dims[0] <= 0.50

    b_missing = _make_4l_board(stackup_source="missing")
    assert impedance.pair_rule_from_stackup(b_missing, "F.Cu", target_zdiff=90.0) is None


def test_qfn_thermal_vias_and_ipc2152_width():
    w_5a = fanout.ipc2152_track_width(5.0, copper_oz=1.0, delta_t_c=20.0, internal=False)
    w_10a = fanout.ipc2152_track_width(10.0, copper_oz=1.0, delta_t_c=20.0, internal=False)
    assert w_5a > 1.5 and w_10a > w_5a

    b = _make_4l_board(with_planes=True)
    # Give U1 EP pad (at 20,15) full GND plane coverage on In1.Cu
    b.zones[0]["outline"] = [[(0.5, 0.5), (39.5, 0.5), (39.5, 29.5), (0.5, 29.5)]]
    vias = fanout.plan_qfn_thermal_vias(b, {"GND": {"In1.Cu"}}, fixed=[])
    assert len(vias) >= 4
    assert all(v["net"] == "GND" and v.get("thermal_ep") == "U1" for v in vias)


def test_portal_terminal_square_pads_and_surgical_s6a_prune():
    b = _make_4l_board(with_planes=True)
    u1 = b.fp("U1")
    pa, pb = u1.pads[0], u1.pads[1]
    term = pairs.Terminal({"pos": (u1, pa), "neg": (u1, pb)}, "F.Cu")
    rule = pairs.PairRule(width=0.15, gap=0.15, clearance=0.12, via_d=0.45, via_drill=0.20)
    pt = pairs.portal_terminal(b, term, rule, fixed=[])
    assert pt is not None and pt.portal is not None and len(pt.lead) == 2

    # Surgical S6a pruning: 1 bad GND tap must NOT wipe out a healthy GND tap elsewhere on the board.
    good_trk = {"net": "GND", "layer": "F.Cu", "width": 0.2, "points": [(5.0, 5.0), (5.5, 5.0)]}
    good_via = {"net": "GND", "x": 5.5, "y": 5.0, "diameter": 0.45, "drill": 0.2, "layers": ["F.Cu", "B.Cu"]}
    bad_trk = {"net": "GND", "layer": "F.Cu", "width": 0.2, "points": [(25.0, 20.0), (25.5, 20.0)]}
    bad_via = {"net": "GND", "x": 25.5, "y": 20.0, "diameter": 0.45, "drill": 0.2, "layers": ["F.Cu", "B.Cu"]}
    copper = [good_trk, good_via, bad_trk, bad_via]
    row_good = {"ref": "U1", "pad": "6", "net": "GND", "routed": True}
    row_bad = {"ref": "U1", "pad": "7", "net": "GND", "routed": True}
    live_taps = [(row_good, good_trk, good_via), (row_bad, bad_trk, bad_via)]
    touching = [{
        "type": "clearance",
        "items": [{"description": "Via [GND] on F.Cu - B.Cu", "pos": {"x": 25.5, "y": 20.0}}],
    }]
    kept_cu, kept_taps, removed = s6a._prune_drc_items_from_s6a(copper, {"taps": [row_good, row_bad]}, live_taps, touching)
    assert removed == 2
    assert len(kept_cu) == 2 and good_via in kept_cu
    assert len(kept_taps) == 1 and row_good["routed"] is True and row_bad["routed"] is False


def test_hartley_lint_catches_multi_net_split_and_far_plane_decap():
    b = _make_4l_board(with_planes=True)
    # Route fast net USB_DP on F.Cu across the In1.Cu split boundary between GND (x<=20) and VCC_3V3 (x>=20.5)
    b.routing = {
        "tracks": [
            {"net": "USB_DP", "layer": "F.Cu", "width": 0.15, "points": [(12.0, 10.0), (28.0, 10.0)]},
            {"net": "USB_DP", "layer": "B.Cu", "width": 0.15, "points": [(28.0, 10.0), (32.0, 10.0)]},
        ],
        "vias": [
            {"net": "USB_DP", "x": 28.0, "y": 10.0, "diameter": 0.45, "drill": 0.20, "layers": ["F.Cu", "B.Cu"]},
        ],
    }
    intent = {
        "parts": {
            "U1": {"role": {"value": "ic", "confidence": 0.9}},
            "C1": {
                "role": {"value": "decap", "confidence": 0.9},
                "target": {"value": {"ref": "U1", "net": "VCC_3V3", "pins": ["3"]}, "confidence": 0.9},
            },
        },
        "diff_pairs": [{"value": {"positive": "USB_DP", "negative": "USB_DN"}, "confidence": 0.95}],
        "nets": {},
    }
    res = lint.audit(b, intent=intent)
    split_findings = [f for f in res["findings"] if f["rule"] == "reference_split_crossing"]
    assert split_findings, "Expected critical reference_split_crossing across GND <-> VCC_3V3 split"
    assert split_findings[0]["severity"] == "critical"

    # C1 is ~11 mm from U1 pin 3 on plane-coupled VCC_3V3 -> must be caught by decap_loop!
    decap_findings = [f for f in res["findings"] if f["rule"] == "decap_loop"]
    assert decap_findings, "Expected decap_loop finding for far plane-coupled decap C1"
    assert math.isfinite(decap_findings[0]["value"]) and decap_findings[0]["value"] > 6.0


def test_s6b_plane_synthesis_strictly_opt_in():
    b_bare = _make_4l_board(with_planes=False)
    locked_res = s6b.propose_planes(b_bare)  # default stackup_policy="locked"
    assert locked_res["applied"] is False
    assert locked_res["reason"] == "stackup_policy_locked"
    assert locked_res["zones"] == []

    prop_res = s6b.propose_planes(b_bare, stackup_policy="propose")
    assert prop_res["applied"] is True
    assert len(prop_res["zones"]) == 2
    assert prop_res["zones"][0]["net"] == "GND"
    assert prop_res["zones"][0]["layers"] == ["In1.Cu"]
