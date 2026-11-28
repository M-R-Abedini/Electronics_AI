# SPDX-License-Identifier: MIT
"""Regression tests for assembly-layer legend default, non-blocking silkscreen text,
ESD proximity/orientation refinement, and S6a decap/tap reach upgrades."""

from __future__ import annotations

import inspect

from eda_agent.placer import mcp, silkscreen
from eda_agent.placer.ir import (
    Board,
    Capabilities,
    CopperLayer,
    Courtyard,
    Footprint,
    Net,
    NetClass,
    Outline,
    Pad,
    Stackup,
    derive,
)
from eda_agent.placer.place import decap, pipeline, review
from eda_agent.placer.route import s6a


def test_assembly_only_is_default_silkscreen_policy_everywhere() -> None:
    assert inspect.signature(silkscreen.build_config).parameters["policy"].default == "assembly_only"
    assert inspect.signature(pipeline.place_board).parameters["silkscreen_policy"].default == "assembly_only"
    assert inspect.signature(mcp.PlacerService.place_board).parameters["silkscreen_policy"].default == "assembly_only"
    assert inspect.signature(mcp.PlacerService.place_cluster).parameters["silkscreen_policy"].default == "assembly_only"
    tools = {t["name"]: t["inputSchema"]["properties"] for t in mcp.PlacerService.list_tools()}
    assert tools["place_board"]["silkscreen_policy"]["default"] == "assembly_only"
    assert tools["place_cluster"]["silkscreen_policy"]["default"] == "assembly_only"


def test_review_ignores_silkscreen_text_drc_during_placement() -> None:
    text_drc = {
        "type": "silk_over_copper",
        "severity": "error",
        "description": "Silkscreen clipped by solder mask",
        "items": [
            {"description": "Reference field R12 on F.Silkscreen"},
            {"description": "Pad 1 [SIG] of R12 on F.Cu"},
        ],
    }
    text_overlap = {
        "type": "silk_overlap",
        "severity": "error",
        "description": "Silkscreen overlap",
        "items": [
            {"description": "PCB_TEXT 'U1' on F.Silkscreen"},
            {"description": "Segment of U1 on F.Silkscreen"},
        ],
    }
    graphic_drc = {
        "type": "silk_over_copper",
        "severity": "error",
        "description": "Silkscreen clipped by solder mask",
        "items": [
            {"description": "Segment of P4 on F.Silkscreen"},
            {"description": "PTH pad 1 [SIG] of R1"},
        ],
    }
    assert review._is_silkscreen_text_drc(text_drc) is True
    assert review._is_silkscreen_text_drc(text_overlap) is True
    assert review._is_silkscreen_text_drc(graphic_drc) is False


def test_esd_refine_rotates_and_tucks_esd_toward_connector() -> None:
    conn = Footprint(
        ref="J1", value="Conn_01x02", lib_id="Connector:Terminal_2P",
        x=40.0, y=20.0, rotation=0.0, side="F", locked=True,
        pads=[
            Pad("1", "IO_SIG", (0.0, -2.5), (1.8, 1.8), kind="thru_hole", layers=("F.Cu", "B.Cu")),
            Pad("2", "GND", (0.0, 2.5), (1.8, 1.8), kind="thru_hole", layers=("F.Cu", "B.Cu")),
        ],
        courtyard=Courtyard("file", [[(-3.0, -5.0), (3.0, -5.0), (3.0, 5.0), (-3.0, 5.0)]]),
        uuid="fp-J1",
    )
    # Place D1 rotated 0 deg (signal pad 1 at -2.2, facing AWAY from J1) and shifted in Y.
    esd = Footprint(
        ref="D1", value="SMBJ24CA", lib_id="Diode_SMD:D_SMB",
        x=32.5, y=12.0, rotation=0.0, side="F",
        pads=[
            Pad("1", "IO_SIG", (-2.2, 0.0), (1.5, 2.0), layers=("F.Cu",)),
            Pad("2", "GND", (2.2, 0.0), (1.5, 2.0), layers=("F.Cu",)),
        ],
        courtyard=Courtyard("file", [[(-3.5, -2.0), (3.5, -2.0), (3.5, 2.0), (-3.5, 2.0)]]),
        uuid="fp-D1",
    )
    board = derive(Board(
        name="esd_unit",
        footprints=[conn, esd],
        outline=[Outline([(0.0, 0.0), (50.0, 0.0), (50.0, 40.0), (0.0, 40.0)])],
        outline_valid=True,
        board_area_mm2=2000.0,
        copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)],
        netclasses=[NetClass("Default", 0.2, 0.2, 0.6, 0.3)],
        nets=[Net("IO_SIG", "Default", 2), Net("GND", "Default", 2)],
        stackup=Stackup(source="missing"),
        capabilities=Capabilities(can_flip=True, can_rotate_any_angle=True, can_write_copper=True, can_live_apply=False),
    ))
    intent = {
        "parts": {
            "J1": {"role": {"value": "connector", "confidence": 1.0}},
            "D1": {
                "role": {"value": "esd", "confidence": 1.0},
                "target": {"value": {"ref": "J1"}, "confidence": 1.0},
            },
        }
    }
    before_d, _ = decap._esd_signal_dist(board.fp("D1"), board.fp("J1"))
    assert before_d is not None and before_d > 10.0

    refined, rep = decap.refine_esd(board, intent, fixed={"J1"})
    after_d, _ = decap._esd_signal_dist(refined.fp("D1"), refined.fp("J1"))
    assert rep["moved"] == ["D1"]
    assert after_d is not None and after_d < before_d - 3.0


def test_fixed_connector_silk_overhang_protected_from_neighbor_pad() -> None:
    conn = Footprint(
        ref="P4", value="Conn", lib_id="Connector:PinHeader",
        x=20.0, y=10.0, rotation=0.0, side="F", locked=True,
        pads=[Pad("1", "N1", (0.0, 0.0), (1.5, 1.5), kind="thru_hole", layers=("F.Cu", "B.Cu"))],
        courtyard=Courtyard("file", [[(-1.5, -1.5), (1.5, -1.5), (1.5, 1.5), (-1.5, 1.5)]]),
        silk_local_mm=(-1.9, -1.9, 1.9, 1.9),
        uuid="fp-P4",
    )
    res = Footprint(
        ref="R1", value="10k", lib_id="Resistor_THT:R_Axial",
        x=16.8, y=10.0, rotation=0.0, side="F",
        pads=[Pad("1", "N1", (0.8, 0.0), (1.4, 1.4), kind="thru_hole", layers=("F.Cu", "B.Cu"))],
        courtyard=Courtyard("file", [[(-1.5, -1.0), (1.5, -1.0), (1.5, 1.0), (-1.5, 1.0)]]),
        uuid="fp-R1",
    )
    board = derive(Board(
        name="silk_pad_guard",
        footprints=[conn, res],
        outline=[Outline([(0.0, 0.0), (40.0, 0.0), (40.0, 20.0), (0.0, 20.0)])],
        outline_valid=True,
        board_area_mm2=800.0,
        copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)],
        rules={"silk_clearance": 0.1},
        netclasses=[NetClass("Default", 0.2, 0.2, 0.6, 0.3)],
    ))
    assert decap.silk_graphic_overlap(board, board.fp("R1")) is True
    # Even on legacy silk_clearance == 0 boards, fixed/locked connector silk overhangs are guarded post-SA.
    board_legacy = derive(Board(
        name="silk_pad_guard_legacy",
        footprints=[conn, res],
        outline=[Outline([(0.0, 0.0), (40.0, 0.0), (40.0, 20.0), (0.0, 20.0)])],
        outline_valid=True,
        board_area_mm2=800.0,
        copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)],
        rules={"silk_clearance": 0.0},
        netclasses=[NetClass("Default", 0.2, 0.2, 0.6, 0.3)],
    ))
    assert decap.silk_graphic_overlap(board_legacy, board_legacy.fp("R1"), 0.0) is True


def test_s6a_decap_route_and_fallback_tap_budgets() -> None:
    assert s6a.LINK_ROUTE_MAX_MM > s6a.LINK_MAX_MM
    assert s6a.TAP_FALLBACK_MAX_MM > s6a.TAP_MAX_MM
    assert s6a.SHARE_FALLBACK_MAX_MM > s6a.SHARE_MAX_MM
