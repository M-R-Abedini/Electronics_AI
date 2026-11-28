# SPDX-License-Identifier: MIT
"""Unit tests for Pillar A-C regularity, Pro-Score selection, thermal EP vias, and lint physics."""

from __future__ import annotations

from eda_agent.placer.eval import lint, metrics
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
from eda_agent.placer.place import decap, pipeline, problem
from eda_agent.placer.route.fanout import plan_qfn_thermal_vias


def _rect(x0: float, y0: float, x1: float, y1: float) -> list[tuple[float, float]]:
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _smd_passive(ref: str, x: float, y: float, rot: float, net1: str, net2: str, lib: str = "R:0603") -> Footprint:
    return Footprint(
        ref=ref,
        value="10k",
        lib_id=lib,
        x=x,
        y=y,
        rotation=rot,
        side="F",
        pads=[
            Pad("1", net1, (-0.75, 0.0), (0.8, 0.9), layers=("F.Cu", "F.Mask", "F.Paste")),
            Pad("2", net2, (0.75, 0.0), (0.8, 0.9), layers=("F.Cu", "F.Mask", "F.Paste")),
        ],
        courtyard=Courtyard("file", [_rect(-1.4, -0.7, 1.4, 0.7)]),
    )


def test_rust_sa_regularity_improves_alignment_and_axis() -> None:
    fps = [
        Footprint(
            ref="U1",
            value="IC",
            lib_id="IC:QFN",
            x=25.0,
            y=25.0,
            rotation=0.0,
            side="F",
            locked=True,
            pads=[
                Pad(str(k + 1), f"S{k}", (-4.0 + k * 1.2, 3.0), (0.5, 0.9), layers=("F.Cu",))
                for k in range(8)
            ],
            courtyard=Courtyard("file", [_rect(-5.0, -4.0, 5.0, 4.0)]),
        )
    ]
    # 8 scattered 0603 passives at irregular coordinates and mixed orientations
    for k in range(8):
        fps.append(_smd_passive(f"R{k + 1}", 12.0 + k * 3.37, 12.0 + (k % 3) * 2.13, float((k % 2) * 90), f"S{k}", "GND"))
    board = Board(
        name="reg_test",
        footprints=fps,
        outline=[Outline(_rect(0.0, 0.0, 50.0, 50.0))],
        board_area_mm2=2500.0,
        copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)],
        netclasses=[NetClass("Default", 0.15, 0.20, 0.50, 0.25)],
        nets=[Net("GND", "Default", 8)] + [Net(f"S{k}", "Default", 2) for k in range(8)],
        rules={"min_clearance": 0.15},
    )
    board.reindex()
    pb = problem.build(board, side_policy="top_only", floorplan=True)
    assert pb.w_reg > 0.0
    assert sum(1 for g in pb.reg_groups if g > 0) == 8

    core = problem.rust_problem(pb)
    core.anneal(40_000, 0, 8.0, 0.05)
    placed = problem.apply(pb, core.positions())
    reg = metrics.regularity(placed)
    assert reg["aligned_fraction"] >= 0.50
    assert reg["dominant_axis_fraction"] >= 0.75


def test_decap_bounded_slack_allows_alignment_without_leaving_good_zone() -> None:
    u1 = Footprint(
        ref="U1", value="IC", lib_id="IC:SOIC", x=20.0, y=20.0, rotation=0.0, side="F", locked=True,
        pads=[
            Pad("1", "VCC", (-2.0, -2.0), (0.6, 1.0), layers=("F.Cu",)),
            Pad("2", "VCC", (2.0, -2.0), (0.6, 1.0), layers=("F.Cu",)),
            Pad("3", "GND", (0.0, 2.0), (0.6, 1.0), layers=("F.Cu",)),
        ],
        courtyard=Courtyard("file", [_rect(-3.0, -3.0, 3.0, 3.0)]),
    )
    # Two 0603 decaps cleanly outside U1 courtyard (y <= 16.3) and slightly offset in Y (15.2 vs 15.8)
    c1 = _smd_passive("C1", 18.0, 15.2, 0.0, "VCC", "GND", lib="C:0603")
    c2 = _smd_passive("C2", 22.0, 15.8, 0.0, "VCC", "GND", lib="C:0603")
    board = Board(
        name="decap_align_test",
        footprints=[u1, c1, c2],
        outline=[Outline(_rect(0.0, 0.0, 40.0, 40.0))],
        board_area_mm2=1600.0,
        copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)],
        netclasses=[NetClass("Default", 0.15, 0.20, 0.50, 0.25)],
        nets=[Net("VCC", "Default", 4), Net("GND", "Default", 3)],
        rules={"min_clearance": 0.15},
    )
    board.reindex()
    intent = {
        "parts": {
            "C1": {
                "role": {"value": "decap", "confidence": 1.0, "status": "settled"},
                "target": {"value": {"ref": "U1", "net": "VCC", "pins": ["1"]}, "confidence": 1.0, "status": "settled"},
            },
            "C2": {
                "role": {"value": "decap", "confidence": 1.0, "status": "settled"},
                "target": {"value": {"ref": "U1", "net": "VCC", "pins": ["2"]}, "confidence": 1.0, "status": "settled"},
            },
        }
    }
    aligned, _rep = decap.align_passives(board, intent, {"U1"}, extend=True, max_trials=120)
    reg = metrics.regularity(aligned)
    assert reg["aligned_fraction"] == 1.0
    assert reg["dominant_axis_fraction"] == 1.0


def test_plan_qfn_thermal_vias_ignores_paste_only_stencil_pads() -> None:
    # HTSSOP-24-1EP style IC with 6 segmented F.Paste stencil pads sitting directly inside pad 25 EP
    pads = [
        Pad(str(k + 1), f"N{k}", (-3.5 + (k % 12) * 0.65, -2.9 if k < 12 else 2.9), (0.4, 1.5), layers=("F.Cu", "F.Mask", "F.Paste"))
        for k in range(24)
    ]
    pads.append(Pad("25", "GND", (0.0, 0.0), (5.0, 3.2), layers=("F.Cu", "F.Mask")))
    for sx in (-1.6, 0.0, 1.6):
        for sy in (-0.8, 0.8):
            pads.append(Pad("", None, (sx, sy), (1.3, 1.3), layers=("F.Paste",)))
    u8 = Footprint(
        ref="U8", value="DAC8775", lib_id="Package_SO:HTSSOP-24-1EP",
        x=30.0, y=30.0, rotation=0.0, side="F", pads=pads,
        courtyard=Courtyard("file", [_rect(-4.5, -4.0, 4.5, 4.0)]),
    )
    board = Board(
        name="ep_paste_test",
        footprints=[u8],
        outline=[Outline(_rect(0.0, 0.0, 60.0, 60.0))],
        board_area_mm2=3600.0,
        copper_layers=[
            CopperLayer("F.Cu", "F.Cu", 0),
            CopperLayer("In1.Cu", "In1.Cu", 1),
            CopperLayer("In2.Cu", "In2.Cu", 2),
            CopperLayer("B.Cu", "B.Cu", 3),
        ],
        netclasses=[NetClass("Default", 0.17, 0.20, 0.60, 0.30)],
        nets=[Net("GND", "Default", 1)] + [Net(f"N{k}", "Default", 1) for k in range(24)],
        zones=[{"uuid": "z1", "net": "GND", "layers": ["In1.Cu"], "outline": [_rect(1.0, 1.0, 59.0, 59.0)], "filled": True}],
        rules={"min_clearance": 0.17, "min_via_diameter": 0.50, "min_through_drill": 0.25, "hole_to_hole": 0.25},
    )
    board.reindex()
    vias = plan_qfn_thermal_vias(board, {"GND": {"In1.Cu"}}, fixed=[])
    assert len(vias) >= 3
    assert all(v["net"] == "GND" and v["thermal_ep"] == "U8" for v in vias)


def test_lint_exempts_pth_endpoint_antipad_and_intentional_moat() -> None:
    # 4-layer board with solid In1.Cu GND except an intentional moat keepout under U1 (16-pin bridge)
    # and a through-hole terminal J2 (2.6 mm pad -> 1.5 mm antipad) on fast net RTD_P.
    j2 = Footprint(
        ref="J2", value="Conn", lib_id="TerminalBlock:4Pin", x=50.0, y=25.0, rotation=0.0, side="F", locked=True,
        pads=[Pad("1", "RTD_P", (0.0, 0.0), (2.6, 2.6), kind="thru_hole", drill=(1.3, 1.3), layers=("*.Cu", "*.Mask"))],
        courtyard=Courtyard("file", [_rect(-2.5, -2.5, 2.5, 2.5)]),
    )
    u1 = Footprint(
        ref="U1", value="ISO", lib_id="SOIC:16", x=25.0, y=25.0, rotation=0.0, side="F", locked=True,
        pads=[Pad(str(k + 1), f"P{k}", (-4.0 if k < 8 else 4.0, -3.5 + (k % 8)), (0.6, 1.2), layers=("F.Cu",)) for k in range(16)],
        courtyard=Courtyard("file", [_rect(-5.5, -5.0, 5.5, 5.0)]),
    )
    board = Board(
        name="lint_phys_test",
        footprints=[j2, u1],
        outline=[Outline(_rect(0.0, 0.0, 60.0, 50.0))],
        board_area_mm2=3000.0,
        copper_layers=[
            CopperLayer("F.Cu", "F.Cu", 0),
            CopperLayer("In1.Cu", "In1.Cu", 1),
            CopperLayer("In2.Cu", "In2.Cu", 2),
            CopperLayer("B.Cu", "B.Cu", 3),
        ],
        netclasses=[NetClass("Default", 0.20, 0.25, 0.60, 0.30)],
        nets=[Net("GND", "Default", 0), Net("RTD_P", "Default", 1)],
        zones=[{
            "uuid": "z_gnd", "net": "GND", "layers": ["In1.Cu"],
            "outline": [_rect(1.0, 1.0, 59.0, 49.0)], "filled": True,
            "filled_area_mm2": {"In1.Cu": 2200.0},
        }],
        rule_areas=[{
            "uuid": "moat1", "name": "MOAT_ISO",
            "layers": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
            "keepout": {"tracks": True, "vias": True, "pads": True, "zones": True, "footprints": True},
            "polygons": [_rect(21.5, 1.0, 28.5, 49.0)],
        }],
        routing={
            "tracks": [
                {"net": "RTD_P", "layer": "F.Cu", "width": 0.25, "points": [(35.0, 25.0), (50.0, 25.0)]},
            ],
            "vias": [],
        },
        rules={"min_clearance": 0.20},
    )
    board.reindex()
    derive(board)
    intent = {
        "parts": {"U1": {"role": {"value": "ic", "confidence": 1.0, "status": "settled"}}},
        "nets": {"RTD_P": {"class": {"value": "analog_sensitive", "confidence": 1.0, "status": "settled"}}},
    }
    res = lint.audit(board, intent=intent)
    rules_hit = {f["rule"] for f in res["findings"]}
    assert "reference_split_crossing" not in rules_hit
    assert "plane_perforation" not in rules_hit


def test_pro_score_selector_prefers_higher_pro_score_candidate() -> None:
    info_ok = {"strict_violations": 0, "illegal_after_s5": [], "s4_failed": []}
    m_base = {
        "crossings": 75, "decap_p90_mm": 2.1, "all_decap_p90_mm": 2.1,
        "decap_median_mm": 1.8, "resolved_decaps": 18, "regularity_score": 55.0,
        "placement_pro_score": 76.0, "esd_max_mm": 7.0,
    }
    m_better = {
        "crossings": 62, "decap_p90_mm": 2.8, "all_decap_p90_mm": 2.8,
        "decap_median_mm": 1.78, "resolved_decaps": 18, "regularity_score": 82.0,
        "placement_pro_score": 84.5, "esd_max_mm": 6.5,
    }
    assert pipeline._floorplan_rank(info_ok, m_better, {"clear": 55}) < pipeline._floorplan_rank(
        info_ok, m_base, {"clear": 29}
    )


def test_silk_graphic_overhang_protection_and_overlap_guard() -> None:
    """Footprints whose F.SilkS graphics overhang F.CrtYd are protected in SA and decap alignment."""
    d4 = Footprint(
        ref="D4", value="BAT54", lib_id="Diode_SMD:D_SOD-882", x=10.0, y=10.0, rotation=0.0, side="F",
        pads=[Pad("1", "N1", (-0.4, 0.0), (0.4, 0.5), layers=("F.Cu",)),
              Pad("2", "GND", (0.4, 0.0), (0.4, 0.5), layers=("F.Cu",))],
        courtyard=Courtyard("file", [_rect(-0.85, -0.65, 0.85, 0.65)]),
        silk_local_mm=(-0.88, -0.68, 0.88, 0.68),
    )
    d6 = Footprint(
        ref="D6", value="BAT54", lib_id="Diode_SMD:D_SOD-882", x=11.72, y=10.0, rotation=0.0, side="F",
        pads=[Pad("1", "N2", (-0.4, 0.0), (0.4, 0.5), layers=("F.Cu",)),
              Pad("2", "GND", (0.4, 0.0), (0.4, 0.5), layers=("F.Cu",))],
        courtyard=Courtyard("file", [_rect(-0.85, -0.65, 0.85, 0.65)]),
        silk_local_mm=(-0.88, -0.68, 0.88, 0.68),
    )
    board = Board(
        name="silk_overhang_test",
        footprints=[d4, d6],
        outline=[Outline(_rect(0.0, 0.0, 40.0, 40.0))],
        board_area_mm2=1600.0,
        copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)],
        netclasses=[NetClass("Default", 0.15, 0.2, 0.5, 0.25)],
        nets=[Net("GND", "Default", 2), Net("N1", "Default", 1), Net("N2", "Default", 1)],
        rules={"silk_clearance": 0.10},
    )
    board.reindex()
    derive(board)
    # At x=10.0 and x=11.72, courtyards have 0.02 mm gap (no courtyard overlap),
    # yet their silkscreen graphics overlap by 0.04 mm -> must be caught!
    assert decap.silk_graphic_overlap(board, d4) is True
    pb = problem.build(board, side_policy="top_only", floorplan=True)
    # Solver box must expand beyond raw courtyard (-0.85..0.85) to cover silk + half silk_clearance.
    assert pb.parts[0][0] <= -0.95
    assert pb.parts[0][2] >= 0.95
    # Moving d6 to x=12.0 leaves 0.24 mm silk-to-silk clearance -> clean!
    d6.x = 12.0
    assert decap.silk_graphic_overlap(board, d4) is False


def test_feranec_connector_entry_signal_flow_and_utilization() -> None:
    """Connector -> Series R -> Clamp Diode generates entry-corridor affinities and utilization metrics."""
    j2 = Footprint(
        ref="J2", value="Conn", lib_id="Conn:1x02", x=55.0, y=25.0, rotation=0.0, side="F", locked=True,
        pads=[Pad("1", "IN_RAW", (0.0, -2.5), (1.8, 1.8), kind="thru_hole", layers=("F.Cu", "B.Cu")),
              Pad("2", "GND", (0.0, 2.5), (1.8, 1.8), kind="thru_hole", layers=("F.Cu", "B.Cu"))],
        courtyard=Courtyard("file", [_rect(-2.5, -5.0, 2.5, 5.0)]),
    )
    r8 = Footprint(
        ref="R8", value="1k", lib_id="R:0603", x=30.0, y=25.0, rotation=0.0, side="F",
        pads=[Pad("1", "IN_RAW", (-0.8, 0.0), (0.8, 0.9), layers=("F.Cu",)),
              Pad("2", "IN_FILT", (0.8, 0.0), (0.8, 0.9), layers=("F.Cu",))],
        courtyard=Courtyard("file", [_rect(-1.5, -0.8, 1.5, 0.8)]),
    )
    d8 = Footprint(
        ref="D8", value="BAT54", lib_id="D:SOD-882", x=20.0, y=25.0, rotation=0.0, side="F",
        pads=[Pad("1", "IN_FILT", (-0.4, 0.0), (0.4, 0.5), layers=("F.Cu",)),
              Pad("2", "GND", (0.4, 0.0), (0.4, 0.5), layers=("F.Cu",))],
        courtyard=Courtyard("file", [_rect(-0.85, -0.65, 0.85, 0.65)]),
    )
    u4 = Footprint(
        ref="U4", value="ADC", lib_id="TSSOP:16", x=15.0, y=25.0, rotation=0.0, side="F",
        pads=[Pad("1", "IN_FILT", (2.0, 0.0), (0.6, 1.2), layers=("F.Cu",)),
              Pad("2", "GND", (-2.0, 0.0), (0.6, 1.2), layers=("F.Cu",))],
        courtyard=Courtyard("file", [_rect(-3.0, -4.0, 3.0, 4.0)]),
    )
    board = Board(
        name="feranec_flow_test",
        footprints=[j2, r8, d8, u4],
        outline=[Outline(_rect(0.0, 0.0, 60.0, 50.0))],
        board_area_mm2=3000.0,
        copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)],
        netclasses=[NetClass("Default", 0.15, 0.2, 0.5, 0.25)],
        nets=[Net("GND", "Default", 3), Net("IN_RAW", "Default", 2), Net("IN_FILT", "Default", 3)],
        rules={"silk_clearance": 0.10},
    )
    board.reindex()
    derive(board)
    pb = problem.build(board, side_policy="top_only", floorplan=True)
    # Expect affinities pulling R8 toward J2 and D8 toward R8 + J2.
    assert len(pb.affinities) >= 3
    util = metrics.board_utilization(board)
    assert util["available"] is True
    assert util["can_compact"] is True
    assert util["max_side_utilization"] < 0.10
