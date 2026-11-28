# SPDX-License-Identifier: MIT
"""Unit tests for universal PCB EMI/EMC, HV safety & thermal DFM lint rules (Rules 10-14)."""

from __future__ import annotations

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
)


def _rect_poly(x0: float, y0: float, x1: float, y1: float) -> list[tuple[float, float]]:
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _make_board(
    *,
    footprints: list[Footprint],
    nets: list[Net] | None = None,
    netclasses: list[NetClass] | None = None,
    zones: list[dict] | None = None,
    tracks: list[dict] | None = None,
    vias: list[dict] | None = None,
) -> Board:
    return Board(
        name="synthetic_emc",
        footprints=footprints,
        outline=[Outline(outer=_rect_poly(0.0, 0.0, 100.0, 80.0))],
        board_area_mm2=8000.0,
        copper_layers=[
            CopperLayer("F.Cu", "F.Cu", 0, "signal"),
            CopperLayer("In1.Cu", "In1.Cu", 1, "power"),
            CopperLayer("In2.Cu", "In2.Cu", 2, "signal"),
            CopperLayer("B.Cu", "B.Cu", 3, "signal"),
        ],
        rules={"min_clearance": 0.15},
        netclasses=netclasses or [NetClass("Default", 0.15, 0.2, 0.5, 0.25)],
        nets=nets or [],
        zones=zones or [],
        routing={"tracks": tracks or [], "vias": vias or []},
        stackup=Stackup(source="file", board_thickness_mm=1.6),
    )


def _rules(report: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for f in report.get("findings", []):
        out.setdefault(f["rule"], []).append(f)
    return out


def test_mounting_hole_clearance_lv_and_hv():
    # H1 near HV AC_L pad at 2.0 mm clearance -> critical (<4.0 mm HV limit)
    # H2 with 8.0 mm clearance from HV -> clean!
    h1 = Footprint(
        ref="H1", value="M3", lib_id="MountingHole:MountingHole_3.2mm_M3",
        x=5.0, y=5.0, rotation=0.0,
        pads=[Pad("1", None, (0.0, 0.0), (3.2, 3.2), kind="np_thru_hole", drill=(3.2, 3.2))],
    )
    h2 = Footprint(
        ref="H2", value="M3", lib_id="MountingHole:MountingHole_3.2mm_M3",
        x=95.0, y=75.0, rotation=0.0,
        pads=[Pad("1", None, (0.0, 0.0), (3.2, 3.2), kind="np_thru_hole", drill=(3.2, 3.2))],
    )
    j1 = Footprint(
        ref="J1", value="AC_IN", lib_id="TerminalBlock:TB2",
        x=9.0, y=5.0, rotation=0.0,
        pads=[Pad("1", "AC_L", (0.0, 0.0), (2.0, 2.0), kind="thru_hole", drill=(1.0, 1.0))],
    )
    board = _make_board(
        footprints=[h1, h2, j1],
        nets=[Net("AC_L", "HV", 1)],
        netclasses=[NetClass("Default", 0.15, 0.2, 0.5, 0.25), NetClass("HV", 1.5, 0.5, 0.7, 0.3)],
        tracks=[{"net": "AC_L", "layer": "F.Cu", "width": 0.5, "points": [(9.0, 5.0), (15.0, 5.0)]}],
    )
    rep = lint.audit(board)
    hits = _rules(rep).get("mounting_hole_copper_clearance", [])
    assert len(hits) == 1
    assert hits[0]["severity"] == "critical"
    assert hits[0]["refs"] == ["H1"]


def test_copper_pour_under_unfused_mains_emi_caught_and_cleared():
    f1 = Footprint(
        ref="F1", value="Fuse", lib_id="Fuse:TR5",
        x=20.0, y=20.0, rotation=0.0,
        pads=[
            Pad("1", "AC_L", (-2.5, 0.0), (1.8, 1.8), kind="thru_hole", drill=(1.0, 1.0)),
            Pad("2", "AC_L_FUSED", (2.5, 0.0), (1.8, 1.8), kind="thru_hole", drill=(1.0, 1.0)),
        ],
        courtyard=Courtyard("file", [_rect_poly(-5.0, -5.0, 5.0, 5.0)]),
    )
    bad_zone = {
        "net": "PRI_RET",
        "layers": ["In1.Cu"],
        "filled": True,
        "outline": [_rect_poly(5.0, 5.0, 45.0, 45.0)],
        "fill_geometry": {"In1.Cu": [{"island": False, "outer": _rect_poly(5.0, 5.0, 45.0, 45.0), "holes": []}]},
    }
    clean_zone = {
        "net": "PRI_RET",
        "layers": ["In1.Cu"],
        "filled": True,
        "outline": [_rect_poly(35.0, 5.0, 45.0, 45.0)],
        "fill_geometry": {"In1.Cu": [{"island": False, "outer": _rect_poly(35.0, 5.0, 45.0, 45.0), "holes": []}]},
    }
    tracks = [{"net": "AC_L", "layer": "F.Cu", "width": 0.6, "points": [(10.0, 20.0), (17.5, 20.0)]}]
    rep_bad = lint.audit(_make_board(footprints=[f1], zones=[bad_zone], tracks=tracks))
    hits = _rules(rep_bad).get("copper_pour_under_unfused_mains", [])
    assert len(hits) == 1 and hits[0]["severity"] == "critical" and hits[0]["refs"] == ["F1"]

    rep_clean = lint.audit(_make_board(footprints=[f1], zones=[clean_zone], tracks=tracks))
    assert not _rules(rep_clean).get("copper_pour_under_unfused_mains")


def test_hv_pad_creepage_geometric_check():
    # 0603 resistor on HV_BUS: pads at +-0.8 mm, width 0.8 mm -> edge gap = 0.8 mm (<0.9 mm crit)
    r_small = Footprint(
        ref="R2", value="2M", lib_id="Resistor_SMD:R_0603",
        x=20.0, y=10.0, rotation=0.0,
        pads=[
            Pad("1", "HV_BUS", (-0.8, 0.0), (0.8, 0.9), kind="smd"),
            Pad("2", "V_MID", (0.8, 0.0), (0.8, 0.9), kind="smd"),
        ],
    )
    # THT axial resistor on HV_BUS: pads at 0 and 10.16 mm, dia 1.6 mm -> gap = 8.56 mm (clean)
    r_tht = Footprint(
        ref="R3", value="2M", lib_id="Resistor_THT:R_Axial_DIN0207_P10.16mm",
        x=40.0, y=10.0, rotation=0.0,
        pads=[
            Pad("1", "HV_BUS", (0.0, 0.0), (1.6, 1.6), kind="thru_hole", drill=(0.8, 0.8)),
            Pad("2", "V_MID", (10.16, 0.0), (1.6, 1.6), kind="thru_hole", drill=(0.8, 0.8)),
        ],
    )
    board = _make_board(footprints=[r_small, r_tht], nets=[Net("HV_BUS", "Default", 2)])
    rep = lint.audit(board)
    hits = _rules(rep).get("hv_pad_creepage_undersized", [])
    assert len(hits) == 1 and hits[0]["refs"] == ["R2"] and hits[0]["severity"] == "critical"


def test_tvs_ground_return_inductance_and_thermal_pad_via_wicking():
    j3 = Footprint(
        ref="J3", value="OUT", lib_id="TerminalBlock:TB2",
        x=90.0, y=20.0, rotation=0.0,
        pads=[Pad("1", "IOUT", (0.0, 0.0), (2.0, 2.0), kind="thru_hole", drill=(1.0, 1.0))],
    )
    d12 = Footprint(
        ref="D12", value="SMBJ18A", lib_id="Diode_SMD:D_SMB",
        x=82.0, y=20.0, rotation=0.0,
        pads=[
            Pad("1", "IOUT", (2.15, 0.0), (1.8, 2.2), kind="smd"),
            Pad("2", "GND", (-2.15, 0.0), (1.8, 2.2), kind="smd"),
        ],
    )
    u8 = Footprint(
        ref="U8", value="DAC8760", lib_id="Package_SO:HTSSOP-24-1EP",
        x=60.0, y=20.0, rotation=0.0,
        pads=[Pad("25", "GND", (0.0, 0.0), (3.2, 5.0), kind="smd")],
    )
    tracks = [{"net": "IOUT", "layer": "F.Cu", "width": 0.3, "points": [(84.15, 20.0), (90.0, 20.0)]}]

    # Single via on D12 GND + large 0.40 mm open via inside U8 EP -> both warn
    bad_vias = [
        {"net": "GND", "x": 79.0, "y": 20.0, "diameter": 0.6, "drill": 0.30, "layers": ["F.Cu", "B.Cu"]},
        {"net": "GND", "x": 60.0, "y": 20.0, "diameter": 0.7, "drill": 0.40, "layers": ["F.Cu", "B.Cu"]},
    ]
    rep_bad = lint.audit(_make_board(footprints=[j3, d12, u8], tracks=tracks, vias=bad_vias))
    r_bad = _rules(rep_bad)
    assert len(r_bad.get("tvs_ground_return_inductance", [])) == 1
    assert len(r_bad.get("thermal_pad_via_paste_wicking", [])) == 1

    # 3 parallel vias on D12 GND + 0.25 mm vias on U8 EP -> both clear!
    good_vias = [
        {"net": "GND", "x": 79.0, "y": 19.0, "diameter": 0.6, "drill": 0.30, "layers": ["F.Cu", "B.Cu"]},
        {"net": "GND", "x": 79.0, "y": 20.0, "diameter": 0.6, "drill": 0.30, "layers": ["F.Cu", "B.Cu"]},
        {"net": "GND", "x": 79.0, "y": 21.0, "diameter": 0.6, "drill": 0.30, "layers": ["F.Cu", "B.Cu"]},
        {"net": "GND", "x": 60.0, "y": 20.0, "diameter": 0.5, "drill": 0.25, "layers": ["F.Cu", "B.Cu"]},
    ]
    rep_good = lint.audit(_make_board(footprints=[j3, d12, u8], tracks=tracks, vias=good_vias))
    r_good = _rules(rep_good)
    assert not r_good.get("tvs_ground_return_inductance")
    assert not r_good.get("thermal_pad_via_paste_wicking")


def test_hv_power_inner_layer_and_prefuse_width_rules():
    j1 = Footprint(
        ref="J1", value="AC_IN", lib_id="TerminalBlock:TB2",
        x=10.0, y=40.0, rotation=0.0,
        pads=[
            Pad("1", "/AUX/AC_L_FUSED", (0.0, -2.5), (2.0, 2.0), kind="thru_hole", drill=(1.0, 1.0)),
            Pad("2", "/AUX/AC_N", (0.0, 2.5), (2.0, 2.0), kind="thru_hole", drill=(1.0, 1.0)),
            Pad("3", "/AUX/HV_BUS", (10.0, 0.0), (2.0, 2.0), kind="thru_hole", drill=(1.0, 1.0)),
        ],
    )
    nets = [
        Net("/AUX/AC_L_FUSED", "AUX_PRIMARY_HV", 1),
        Net("/AUX/AC_N", "AUX_PRIMARY_HV", 2),
        Net("/AUX/HV_BUS", "AUX_PRIMARY_HV", 3),
    ]
    ncs = [NetClass("Default", 0.15, 0.2, 0.5, 0.25), NetClass("AUX_PRIMARY_HV", 1.54, 0.5, 0.75, 0.35)]
    bad_tracks = [
        {"net": "/AUX/AC_L_FUSED", "layer": "In2.Cu", "width": 0.50, "points": [(10.0, 37.5), (40.0, 37.5)]},
        {"net": "/AUX/AC_N", "layer": "F.Cu", "width": 0.40, "points": [(10.0, 42.5), (30.0, 42.5)]},
        {"net": "/AUX/HV_BUS", "layer": "In2.Cu", "width": 0.50, "points": [(20.0, 40.0), (40.0, 40.0)]},
    ]
    rep_bad = lint.audit(_make_board(footprints=[j1], nets=nets, netclasses=ncs, tracks=bad_tracks))
    rb = _rules(rep_bad)
    inner_hits = {h["net"]: h["severity"] for h in rb.get("hv_power_on_inner_layer", [])}
    assert inner_hits["/AUX/AC_L_FUSED"] == "critical"
    assert inner_hits["/AUX/HV_BUS"] == "warning"
    width_hits = {h["net"]: h["severity"] for h in rb.get("prefuse_mains_trace_undersized", [])}
    assert width_hits["/AUX/AC_L_FUSED"] == "warning"
    assert width_hits["/AUX/AC_N"] == "critical"

    good_tracks = [
        {"net": "/AUX/AC_L_FUSED", "layer": "F.Cu", "width": 1.0, "points": [(10.0, 37.5), (40.0, 37.5)]},
        {"net": "/AUX/AC_N", "layer": "B.Cu", "width": 1.0, "points": [(10.0, 42.5), (30.0, 42.5)]},
        {"net": "/AUX/HV_BUS", "layer": "B.Cu", "width": 0.6, "points": [(20.0, 40.0), (40.0, 40.0)]},
    ]
    rep_good = lint.audit(_make_board(footprints=[j1], nets=nets, netclasses=ncs, tracks=good_tracks))
    rg = _rules(rep_good)
    assert not rg.get("hv_power_on_inner_layer")
    assert not rg.get("prefuse_mains_trace_undersized")


def test_switch_node_emi_and_vertical_prepreg_overlap_rules():
    u1 = Footprint(
        ref="U1", value="INN3162C", lib_id="Power:InSOP-24D",
        x=30.0, y=40.0, rotation=0.0,
        pads=[
            Pad("1", "/AUX/FLY_DRAIN", (0.0, 0.0), (1.0, 1.0), kind="smd", layers=["F.Cu"]),
            Pad("2", "SWDIO", (5.0, 0.0), (0.5, 0.5), kind="smd", layers=["F.Cu"]),
        ],
    )
    nets = [
        Net("/AUX/FLY_DRAIN", "AUX_PRIMARY_HV", 1),
        Net("SWDIO", "Default", 2),
        Net("V3V3_IN", "Default", 3),
        Net("/AUX/FLY_FB", "Default", 4),
    ]
    ncs = [NetClass("Default", 0.15, 0.2, 0.5, 0.25), NetClass("AUX_PRIMARY_HV", 1.54, 0.5, 0.75, 0.35)]
    # FLY_DRAIN on In2.Cu (40 mm) + vertically overlapping V3V3_IN on B.Cu across 0.21 mm prepreg
    # SWDIO must NOT be misclassified as a switching node!
    bad_tracks = [
        {"net": "/AUX/FLY_DRAIN", "layer": "In2.Cu", "width": 0.5, "points": [(10.0, 40.0), (70.0, 40.0)]},
        {"net": "V3V3_IN", "layer": "B.Cu", "width": 0.3, "points": [(20.0, 35.0), (20.0, 45.0)]},
        {"net": "/AUX/FLY_FB", "layer": "B.Cu", "width": 0.2, "points": [(35.0, 35.0), (35.0, 45.0)]},
        {"net": "SWDIO", "layer": "In2.Cu", "width": 0.2, "points": [(60.0, 10.0), (95.0, 10.0)]},
    ]
    rep_bad = lint.audit(_make_board(footprints=[u1], nets=nets, netclasses=ncs, tracks=bad_tracks))
    rb = _rules(rep_bad)
    sw_hits = rb.get("switch_node_inner_or_oversized", [])
    assert len(sw_hits) == 2  # both inner-layer and >55mm length warnings
    assert all(h["net"] == "/AUX/FLY_DRAIN" for h in sw_hits)  # SWDIO ignored!
    ov_hits = rb.get("hv_vertical_prepreg_overlap", [])
    assert any(h["severity"] == "critical" and "V3V3_IN" in h["detail"] for h in ov_hits)
    assert any(h["severity"] == "warning" and "FLY_FB" in h["detail"] for h in ov_hits)

    # Clean: FLY_DRAIN short (12 mm) on F.Cu, no vertical overlap with secondary/FB
    good_tracks = [
        {"net": "/AUX/FLY_DRAIN", "layer": "F.Cu", "width": 0.5, "points": [(30.0, 40.0), (42.0, 40.0)]},
        {"net": "V3V3_IN", "layer": "B.Cu", "width": 0.3, "points": [(75.0, 35.0), (75.0, 45.0)]},
        {"net": "/AUX/FLY_FB", "layer": "F.Cu", "width": 0.2, "points": [(60.0, 35.0), (60.0, 45.0)]},
    ]
    rep_good = lint.audit(_make_board(footprints=[u1], nets=nets, netclasses=ncs, tracks=good_tracks))
    rg = _rules(rep_good)
    assert not rg.get("switch_node_inner_or_oversized")
    assert not rg.get("hv_vertical_prepreg_overlap")


def test_dsn_restrict_power_hv_outer_layers():
    from eda_agent.placer.route import dsn

    fp = Footprint(
        ref="J1", value="AC", lib_id="TerminalBlock:TB2",
        x=10.0, y=10.0, rotation=0.0,
        pads=[
            Pad("1", "/AUX/AC_L", (0.0, 0.0), (2.0, 2.0), kind="thru_hole", drill=(1.0, 1.0)),
            Pad("2", "/AUX/HV_BUS", (5.0, 0.0), (2.0, 2.0), kind="thru_hole", drill=(1.0, 1.0)),
            Pad("3", "/AUX/FLY_DRAIN", (10.0, 0.0), (1.0, 1.0), kind="smd", layers=["F.Cu"]),
            Pad("4", "/AUX/V_SENSE_MID", (15.0, 0.0), (1.0, 1.0), kind="smd", layers=["F.Cu"]),
            Pad("5", "GND", (20.0, 0.0), (1.0, 1.0), kind="smd", layers=["F.Cu"]),
        ],
    )
    board = _make_board(
        footprints=[fp],
        nets=[
            Net("/AUX/AC_L", "AUX_PRIMARY_HV", 1),
            Net("/AUX/HV_BUS", "AUX_PRIMARY_HV", 2),
            Net("/AUX/FLY_DRAIN", "AUX_PRIMARY_HV", 3),
            Net("/AUX/V_SENSE_MID", "AUX_PRIMARY", 4),
            Net("GND", "Default", 5),
        ],
        netclasses=[NetClass("Default", 0.15, 0.2, 0.5, 0.25), NetClass("AUX_PRIMARY_HV", 1.54, 0.5, 0.75, 0.35)],
        zones=[{"uuid": "z1", "net": "GND", "layers": ["In1.Cu"], "filled": True, "fill_fraction": 0.8}],
    )
    sample_dsn = """(pcb test
  (network
    (net "/AUX/AC_L" (pins J1-1))
    (net "/AUX/HV_BUS" (pins J1-2))
    (net "/AUX/FLY_DRAIN" (pins J1-3))
    (net "/AUX/V_SENSE_MID" (pins J1-4))
    (net GND (pins J1-5))
    (class AUX_PRIMARY_HV "/AUX/AC_L" "/AUX/HV_BUS" "/AUX/FLY_DRAIN"
      (circuit (use_via "Via[0-3]_750:350_um"))
      (rule (width 500) (clearance 1540))
    )
    (class AUX_PRIMARY "/AUX/V_SENSE_MID"
      (circuit (use_via "Via[0-3]_650:300_um"))
      (rule (width 400) (clearance 270))
    )
    (class Default GND
      (rule (width 200) (clearance 150))
    )
  )
  (wiring)
)
"""
    updated, restricted = dsn.restrict_power_hv_outer_layers(sample_dsn, board)
    assert set(restricted) == {"/AUX/AC_L", "/AUX/HV_BUS", "/AUX/FLY_DRAIN"}
    # Specctra DSN / Freerouting v2.5 requires (use_layer ...) inside (class ... (circuit ...))!
    assert "(use_layer F.Cu B.Cu)" in updated
    # Low-current sense & inner-plane GND must NOT be restricted
    assert "/AUX/V_SENSE_MID" not in restricted
    assert "GND" not in restricted


def test_dsn_enforce_mains_trace_width_floor_preserves_dru_class_class():
    from eda_agent.placer.route import dsn

    fp = Footprint(
        ref="J1", value="AC", lib_id="TerminalBlock:TB2",
        x=10.0, y=10.0, rotation=0.0,
        pads=[
            Pad("1", "/AUX/AC_L_FUSED", (0.0, 0.0), (1.8, 1.8), kind="thru_hole", drill=(1.0, 1.0)),
            Pad("2", "/AUX/AC_N", (5.0, 0.0), (1.8, 1.8), kind="thru_hole", drill=(1.0, 1.0)),
            Pad("3", "/AUX/HV_BUS", (10.0, 0.0), (1.8, 1.8), kind="thru_hole", drill=(1.0, 1.0)),
        ],
    )
    board = _make_board(
        footprints=[fp],
        nets=[
            Net("/AUX/AC_L_FUSED", "AUX_PRIMARY_HV", 1),
            Net("/AUX/AC_N", "AUX_PRIMARY_HV", 2),
            Net("/AUX/HV_BUS", "AUX_PRIMARY_HV", 3),
        ],
        netclasses=[NetClass("Default", 0.15, 0.2, 0.5, 0.25), NetClass("AUX_PRIMARY_HV", 1.54, 0.5, 0.75, 0.35)],
    )
    sample_dsn = """(pcb test
  (resolution um 10)
  (unit um)
  (network
    (net "/AUX/AC_L_FUSED" (pins J1-1))
    (net "/AUX/AC_N" (pins J1-2))
    (net "/AUX/HV_BUS" (pins J1-3))
    (class AUX_PRIMARY_HV "/AUX/AC_L_FUSED" "/AUX/AC_N" "/AUX/HV_BUS"
      (circuit (use_via "Via[0-3]_750:350_um"))
      (rule (width 500) (clearance 1540))
    )
    (class_class kicad_dru_AUX_PRIMARY_HV_DOMAIN_IN AUX_PRIMARY_HV DOMAIN_IN
      (rule (clearance 8000))
    )
  )
  (wiring)
)
"""
    updated, bumped = dsn.enforce_mains_trace_width_floor(sample_dsn, board, min_width_mm=1.0)
    assert set(bumped) == {"/AUX/AC_L_FUSED", "/AUX/AC_N"}
    assert "AUX_PRIMARY_HV__mains_1000" in updated
    assert "(width 1000)" in updated
    # Must preserve 8.0 mm Pri-Sec clearance AND 1.54 mm intra-HV clearance, and NEVER self-pair at 8.0 mm!
    assert "(clearance 8000)" in updated and "AUX_PRIMARY_HV__mains_1000 DOMAIN_IN" in updated
    assert "AUX_PRIMARY_HV__mains_1000 AUX_PRIMARY_HV__mains_1000" not in updated
    assert "AUX_PRIMARY_HV AUX_PRIMARY_HV__mains_1000" in updated and "(clearance 1540)" in updated


def test_dru_compound_netname_clearance_compilation(tmp_path):
    from eda_agent.placer.route import dru

    fp = Footprint(
        ref="J1", value="AC", lib_id="TerminalBlock:TB2",
        x=10.0, y=10.0, rotation=0.0,
        pads=[
            Pad("1", "/AUX/AC_L", (0.0, 0.0), (2.0, 2.0), kind="thru_hole", drill=(1.0, 1.0)),
            Pad("2", "/AUX/AC_L_FUSED", (5.0, 0.0), (2.0, 2.0), kind="thru_hole", drill=(1.0, 1.0)),
            Pad("3", "/AUX/AC_N", (10.0, 0.0), (1.0, 0.8), kind="smd", layers=["F.Cu"]),
            Pad("4", "/AUX/HV_BUS", (15.0, 0.0), (2.0, 2.0), kind="thru_hole", drill=(1.0, 1.0)),
        ],
    )
    board = _make_board(
        footprints=[fp],
        nets=[
            Net("/AUX/AC_L", "AUX_PRIMARY_HV", 1),
            Net("/AUX/AC_L_FUSED", "AUX_PRIMARY_HV", 2),
            Net("/AUX/AC_N", "AUX_PRIMARY_HV", 3),
            Net("/AUX/HV_BUS", "AUX_PRIMARY_HV", 4),
        ],
        netclasses=[NetClass("Default", 0.15, 0.2, 0.5, 0.25), NetClass("AUX_PRIMARY_HV", 1.54, 0.5, 0.75, 0.35)],
    )
    dru_file = tmp_path / "mains.kicad_dru"
    dru_file.write_text(
        '(version 1)\n'
        '(rule "Mains_AC_L_N_2_5mm"\n'
        '  (condition "(A.NetName == \'/AUX/AC_L\' || A.NetName == \'/AUX/AC_L_FUSED\') '
        '&& (B.NetName == \'/AUX/AC_N\' || B.NetName == \'/AUX/AC_L_FUSED\')")\n'
        '  (constraint clearance (min 2.5mm))\n'
        ')\n'
        '(rule "Mains_AC_L_N_Pad_2_45mm"\n'
        '  (condition "(A.NetName == \'/AUX/AC_L\' || A.NetName == \'/AUX/AC_L_FUSED\') '
        '&& (B.NetName == \'/AUX/AC_N\' || B.NetName == \'/AUX/AC_L_FUSED\')")\n'
        '  (constraint clearance (min 2.45mm))\n'
        ')\n',
        encoding="utf-8",
    )
    sample_dsn = """(pcb test
  (resolution um 10)
  (unit um)
  (network
    (net "/AUX/AC_L" (pins J1-1))
    (net "/AUX/AC_L_FUSED" (pins J1-2))
    (net "/AUX/AC_N" (pins J1-3))
    (net "/AUX/HV_BUS" (pins J1-4))
    (class AUX_PRIMARY_HV "/AUX/AC_L" "/AUX/AC_L_FUSED" "/AUX/AC_N" "/AUX/HV_BUS"
      (circuit (use_via "Via[0-3]_750:350_um"))
      (rule (width 500) (clearance 1540))
    )
  )
  (wiring)
)
"""
    updated, meta = dru.compile_dru_to_dsn(sample_dsn, dru_file, board)
    assert "Mains_AC_L_N_2_5mm" in meta["translated"]
    assert "Mains_AC_L_N_Pad_2_45mm" in meta["translated"]
    assert not meta["untranslated"]
    # Must tighten existing intra_drunet 1540 -> 2500 in place, never emit shadowed 2450/1540 duplicates!
    assert "(clearance 2500)" in updated
    assert "(clearance 2450)" not in updated
