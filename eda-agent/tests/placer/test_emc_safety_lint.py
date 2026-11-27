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
