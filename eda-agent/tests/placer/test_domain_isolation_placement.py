# SPDX-License-Identifier: MIT
"""Unit tests for galvanic isolation domain decomposition and DRU clearance placement guards."""

from __future__ import annotations

from pathlib import Path

from eda_agent.placer.ir import (
    Board,
    CopperLayer,
    Courtyard,
    Footprint,
    Net,
    NetClass,
    Outline,
    Pad,
)
from eda_agent.placer.place import decap, domains, problem
from eda_agent.placer.route import dru


def _rect_poly(x0: float, y0: float, x1: float, y1: float) -> list[tuple[float, float]]:
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _make_fp(
    ref: str,
    x: float,
    y: float,
    pads: list[tuple[str, str, float, float]],
    *,
    side: str = "F",
    locked: bool = False,
    half: float = 1.0,
) -> Footprint:
    return Footprint(
        ref=ref,
        value=ref,
        lib_id="Test:Part",
        x=x,
        y=y,
        rotation=0.0,
        side=side,
        locked=locked,
        pads=[
            Pad(number=num, net=net, local=(lx, ly), size=(0.8, 0.8), layers=("F.Cu",))
            for num, net, lx, ly in pads
        ],
        courtyard=Courtyard(source="file", polygons=[_rect_poly(-half, -half, half, half)]),
    )


def _isolated_board(dru_path: Path | None = None) -> Board:
    """Two-domain board (0..100 x 0..50) split by a 6mm full-layer moat at x=47..53."""
    board = Board(
        name="iso_test",
        copper_layers=[
            CopperLayer("F.Cu", "F.Cu", 0, "signal"),
            CopperLayer("In1.Cu", "In1.Cu", 1, "mixed"),
            CopperLayer("In2.Cu", "In2.Cu", 2, "mixed"),
            CopperLayer("B.Cu", "B.Cu", 3, "signal"),
        ],
        outline=[Outline(outer=_rect_poly(0.0, 0.0, 100.0, 50.0))],
        board_area_mm2=5000.0,
        rules={"min_clearance": 0.15, "copper_edge_clearance": 0.3},
        netclasses=[
            NetClass("Default", 0.15, 0.25, 0.6, 0.3),
            NetClass("HV_PRI_DC", 1.6, 0.6, 0.8, 0.4),
            NetClass("SEC_OUT", 0.25, 0.3, 0.6, 0.3),
        ],
        nets=[
            Net("PRI_BUS", "HV_PRI_DC", 2),
            Net("PRI_RET", "HV_PRI_DC", 2),
            Net("SEC_V", "SEC_OUT", 2),
            Net("SEC_G", "SEC_OUT", 2),
        ],
        footprints=[
            # Fixed primary connector on left
            _make_fp("J1", 8.0, 25.0, [("1", "PRI_BUS", 0.0, -1.0), ("2", "PRI_RET", 0.0, 1.0)], locked=True),
            # Fixed secondary connector on right
            _make_fp("J2", 92.0, 25.0, [("1", "SEC_V", 0.0, -1.0), ("2", "SEC_G", 0.0, 1.0)], locked=True),
            # Fixed isolation bridge straddling x=47..53
            _make_fp(
                "T1",
                50.0,
                25.0,
                [
                    ("1", "PRI_BUS", -6.0, -1.0),
                    ("2", "PRI_RET", -6.0, 1.0),
                    ("3", "SEC_V", 6.0, -1.0),
                    ("4", "SEC_G", 6.0, 1.0),
                ],
                locked=True,
                half=6.5,
            ),
            # Movable primary decap
            _make_fp("C1", 30.0, 25.0, [("1", "PRI_BUS", -0.5, 0.0), ("2", "PRI_RET", 0.5, 0.0)]),
            # Movable secondary decap
            _make_fp("C2", 70.0, 25.0, [("1", "SEC_V", -0.5, 0.0), ("2", "SEC_G", 0.5, 0.0)]),
        ],
        rule_areas=[
            {
                "uuid": "moat-pri-sec",
                "name": "MOAT_PRI_SEC",
                "layers": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
                "keepout": {"tracks": True, "vias": True, "pads": True, "copperpour": True, "footprints": True},
                "polygons": [_rect_poly(47.0, 0.0, 53.0, 50.0)],
            }
        ],
        source={"path": str(dru_path.with_suffix(".kicad_pcb"))} if dru_path else {},
    )
    board.reindex()
    return board


def test_extract_netclass_clearances_parses_dru_and_board(tmp_path: Path) -> None:
    dru_file = tmp_path / "iso.kicad_dru"
    dru_file.write_text(
        '(version 1)\n'
        '(rule "PRI_TO_SEC"\n'
        '  (constraint clearance (min 6.0mm))\n'
        '  (condition "A.hasNetclass(\'HV_PRI_DC\') && B.hasNetclass(\'SEC_OUT\')"))\n'
        '(rule "HV_SELF"\n'
        '  (constraint clearance (min 1.8mm))\n'
        '  (condition "A.hasNetclass(\'HV_PRI_DC\') && B.hasNetclass(\'HV_PRI_DC\')"))\n',
        encoding="utf-8",
    )
    board = _isolated_board(dru_file)
    clrs = dru.extract_netclass_clearances(dru_file, board)
    assert clrs[("HV_PRI_DC", "SEC_OUT")] == 6.0
    assert clrs[("HV_PRI_DC", "HV_PRI_DC")] == 1.8


def test_decompose_isolated_domains_splits_and_guards(tmp_path: Path) -> None:
    dru_file = tmp_path / "iso.kicad_dru"
    dru_file.write_text(
        '(version 1)\n'
        '(rule "PRI_TO_SEC"\n'
        '  (constraint clearance (min 6.0mm))\n'
        '  (condition "A.hasNetclass(\'HV_PRI_DC\') && B.hasNetclass(\'SEC_OUT\')"))\n',
        encoding="utf-8",
    )
    board = _isolated_board(dru_file)
    fixed = {"J1", "J2", "T1"}
    doms = domains.decompose_isolated_domains(board, fixed)
    assert doms is not None
    assert len(doms) == 2
    assert doms[0][1] == {"C1"}
    assert doms[1][1] == {"C2"}

    regs = domains.ref_regions(board, fixed)
    assert regs is not None
    c1 = board.fp("C1")
    assert domains.footprints_in_domains([c1], regs)
    # Jumping C1 across the moat into the secondary region must be rejected
    c1.x = 60.0
    assert not domains.footprints_in_domains([c1], regs)


def test_side_obstacle_rects_includes_moat_and_dru_repulsions(tmp_path: Path) -> None:
    dru_file = tmp_path / "iso.kicad_dru"
    dru_file.write_text(
        '(version 1)\n'
        '(rule "PRI_TO_SEC"\n'
        '  (constraint clearance (min 6.0mm))\n'
        '  (condition "A.hasNetclass(\'HV_PRI_DC\') && B.hasNetclass(\'SEC_OUT\')"))\n',
        encoding="utf-8",
    )
    board = _isolated_board(dru_file)
    obs = problem.side_obstacle_rects(board, {"J1", "J2", "T1"})
    assert any(abs(x0 - 47.0) < 0.05 and abs(x1 - 53.0) < 0.05 for x0, _y0, x1, _y1, _s in obs)

    pb = problem.build(board, side_policy="top_only")
    c1_idx = pb.refs.index("C1")
    c2_idx = pb.refs.index("C2")
    assert any(
        {i, j} == {c1_idx, c2_idx} and dist >= 6.0
        for i, j, dist, _w in pb.repulsions
    )


def test_decap_refine_cannot_jump_across_isolation_moat(tmp_path: Path) -> None:
    dru_file = tmp_path / "iso.kicad_dru"
    dru_file.write_text(
        '(version 1)\n'
        '(rule "PRI_TO_SEC"\n'
        '  (constraint clearance (min 6.0mm))\n'
        '  (condition "A.hasNetclass(\'HV_PRI_DC\') && B.hasNetclass(\'SEC_OUT\')"))\n',
        encoding="utf-8",
    )
    board = _isolated_board(dru_file)
    intent = {
        "parts": {
            "C1": {
                "role": {"value": "decap", "confidence": 1.0, "status": "settled"},
                "target": {
                    "value": {"ref": "T1", "net": "PRI_BUS", "pins": ["1"]},
                    "confidence": 1.0,
                    "status": "settled",
                },
            }
        }
    }
    refined, _rep = decap.refine(board, intent, {"J1", "J2", "T1"})
    assert refined.fp("C1").x < 47.0


def test_split_ground_plane_inferred_as_power_layer() -> None:
    from eda_agent.placer.ir import derive, power_layers

    board = _isolated_board()
    board.zone_fill_by_layer_mm2 = {
        "In1.Cu": {"GND_IN": 1500.0, "GND_OUT": 1200.0, "/PSU/PRI_RET": 150.0}
    }
    derive(board)
    in1_role = next(r for r in board.stackup.roles if r.layer == "In1.Cu")
    assert in1_role.role == "plane"
    assert power_layers(board) == ["In1.Cu"]

