# SPDX-License-Identifier: MIT
"""Unit and integration tests for BGA escape (S6a), Safe ECO cluster routing preservation,
and post-route starved_thermal healing.
"""

from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import pytest
from eda_agent.placer import corpus
from eda_agent.placer.io.kicad import read_board, run_helper
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
from eda_agent.placer.mcp import PlacerService
from eda_agent.placer.place.pipeline import place_board
from eda_agent.placer.route import drc
from eda_agent.placer.route.bga_escape import detect_bga, plan_bga_escapes
from eda_agent.placer.route.check import _starved_thermal_pad_uuids
from eda_agent.placer.route.copper import pad_polygon

# The two integration tests below read the real StickHub corpus board,
# which lives in the KiCad 10 demo tree on developer machines and does
# not exist on CI runners. Follow the gating precedent of
# test_route_check_stickhub.py: skip when the board file is absent
# instead of failing with FileNotFoundError. When the board is present
# but the KiCad toolchain is not, the conftest hook already converts
# ToolchainError into a skip.
_STICKHUB_PATH = corpus.get("stickhub").path
_requires_stickhub = pytest.mark.skipif(
    not _STICKHUB_PATH.is_file(),
    reason="StickHub corpus board unavailable (KiCad 10 demos not installed; "
    "set ELECTRONICS_AI_KICAD to the KiCad install root)",
)


def _synthetic_bga_board(*, layers: int = 4, pitch: float = 0.8, grid_n: int = 6) -> Board:
    pads: list[Pad] = []
    nets_map: dict[str, int] = {}
    half = 0.5 * (grid_n - 1) * pitch
    idx = 0
    for r in range(grid_n):
        for c in range(grid_n):
            idx += 1
            lx = round(c * pitch - half, 4)
            ly = round(r * pitch - half, 4)
            if r == 0 and c == 0:
                net = "GND"
            else:
                net = f"SIG_{r}_{c}"
            nets_map[net] = nets_map.get(net, 0) + 1
            pads.append(
                Pad(
                    number=f"{chr(65 + r)}{c + 1}",
                    net=net,
                    local=(lx, ly),
                    size=(0.40, 0.40),
                    rotation=0.0,
                    shape="Circle",
                    kind="smd",
                    layers=("F.Cu",),
                    drill=None,
                    function="",
                    pin_type="",
                    uuid=f"pad-{idx}",
                )
            )
    bga_fp = Footprint(
        ref="U1",
        value="TEST_BGA",
        lib_id="Package_BGA:BGA-36",
        x=20.0,
        y=20.0,
        rotation=0.0,
        side="F",
        locked=False,
        pads=pads,
        courtyard=Courtyard(source="courtyard", polygons=[[(-4, -4), (4, -4), (4, 4), (-4, 4)]]),
        uuid="fp-u1",
    )
    copper_layers = [
        CopperLayer(name="F.Cu", canonical="F.Cu", index=0, declared_type="signal"),
    ]
    for i in range(1, layers - 1):
        copper_layers.append(
            CopperLayer(name=f"In{i}.Cu", canonical=f"In{i}.Cu", index=i, declared_type="power" if i == 1 else "signal")
        )
    copper_layers.append(
        CopperLayer(name="B.Cu", canonical="B.Cu", index=layers - 1, declared_type="signal")
    )
    zones = []
    if layers >= 4:
        zones.append({
            "uuid": "zone-gnd",
            "name": "GND_PLANE",
            "net": "GND",
            "layers": ["In1.Cu"],
            "outline": [[(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0)]],
            "filled": True,
            "filled_area_mm2": {"In1.Cu": 1500.0},
            "is_rule_area": False,
        })
    board = Board(
        name="synthetic_bga",
        footprints=[bga_fp],
        outline=[Outline(outer=[(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0)], holes=[])],
        outline_valid=True,
        board_area_mm2=1600.0,
        copper_layers=copper_layers,
        rules={
            "min_track_width": 0.10,
            "min_clearance": 0.10,
            "min_via_diameter": 0.40,
            "min_through_drill": 0.15,
            "hole_to_hole": 0.25,
            "copper_edge_clearance": 0.30,
        },
        netclasses=[
            NetClass(
                name="Default",
                clearance=0.12,
                track_width=0.12,
                via_diameter=0.45,
                via_drill=0.20,
                diff_pair_width=0.12,
                diff_pair_gap=0.12,
            )
        ],
        nets=[Net(name=n, netclass="Default", pads=cnt) for n, cnt in nets_map.items()],
        zones=zones,
        rule_areas=[],
        obstacles=[],
        zone_fill_by_layer_mm2={"In1.Cu": {"GND": 1500.0}} if layers >= 4 else {},
        stackup=Stackup(source="missing"),
        capabilities=Capabilities(can_flip=True, can_rotate_any_angle=True, can_write_copper=True, can_live_apply=False),
        source={},
    )
    return derive(board)


def test_pad_polygon_handles_kicad10_capitalized_circle() -> None:
    board = _synthetic_bga_board()
    fp = board.fp("U1")
    pad = fp.pads[0]
    poly = pad_polygon(fp, pad, 0.0)
    # True circle area for d=0.40 mm is pi * 0.2^2 = 0.12566 mm^2 (square would be 0.16 mm^2).
    assert abs(poly.area - math.pi * 0.2 * 0.2) < 0.005


def test_detect_bga_and_plan_escapes_four_layer() -> None:
    board = _synthetic_bga_board(layers=4, pitch=0.8, grid_n=6)
    bga = detect_bga(board.fp("U1"))
    assert bga is not None
    assert bga["ncols"] == 6 and bga["nrows"] == 6
    assert abs(bga["pitch_x"] - 0.8) < 1e-4

    # 2-layer board must skip BGA escape cleanly.
    board_2l = _synthetic_bga_board(layers=2, pitch=0.8, grid_n=6)
    res_2l = plan_bga_escapes(board_2l, {}, fixed=[])
    assert res_2l["copper"] == []

    # 4-layer 6x6 BGA:
    # - plane-only mode (default in s6a.plan for Gate C3 safety): escapes only plane balls (A1=GND).
    res_plane = plan_bga_escapes(board, {}, fixed=[], include_signals=False)
    assert len(res_plane["packages"]) == 1
    assert res_plane["packages"][0]["planned"] == 1
    assert res_plane["packages"][0]["routed"] == 1
    assert len(res_plane["copper"]) == 2

    # - full signal+plane mode: 16 interior balls + 1 outer plane ball (A1=GND) = 17 planned.
    res = plan_bga_escapes(board, {}, fixed=[], include_signals=True)
    assert len(res["packages"]) == 1
    pkg = res["packages"][0]
    assert pkg["ref"] == "U1"
    assert pkg["planned"] == 17
    assert pkg["routed"] == 17
    assert len(res["copper"]) == 34  # 17 tracks + 17 vias
    # Channel-preserving via check: at 0.80 mm pitch, 0.45 mm netclass via leaves 0.35 mm < 0.36 mm
    # (0.12 + 2*0.12), so the engine must select the legal 0.40 mm board-minimum via instead.
    via_diams = {c["diameter"] for c in res["copper"] if "points" not in c}
    assert via_diams == {0.40}


def test_detect_bga_rejects_perimeter_qfn() -> None:
    # 24-pin QFN: only perimeter pads + 1 large thermal pad -> must NOT be classified as BGA.
    pads: list[Pad] = []
    for i in range(6):
        pos = (i - 2.5) * 0.5
        for side_idx, (lx, ly) in enumerate([(pos, -2.0), (pos, 2.0), (-2.0, pos), (2.0, pos)]):
            pads.append(
                Pad(
                    number=str(i * 4 + side_idx + 1),
                    net=f"N{i}_{side_idx}",
                    local=(lx, ly),
                    size=(0.25, 0.5),
                    rotation=0.0,
                    shape="Rect",
                    kind="smd",
                    layers=("F.Cu",),
                    drill=None,
                    function="",
                    pin_type="",
                    uuid=f"qfn-{i}-{side_idx}",
                )
            )
    pads.append(
        Pad(
            number="25",
            net="GND",
            local=(0.0, 0.0),
            size=(2.5, 2.5),
            rotation=0.0,
            shape="Rect",
            kind="smd",
            layers=("F.Cu",),
            drill=None,
            function="",
            pin_type="",
            uuid="qfn-ep",
        )
    )
    qfn = Footprint(
        ref="U2",
        value="QFN24",
        lib_id="Package_DFN_QFN:QFN-24",
        x=10.0,
        y=10.0,
        rotation=0.0,
        side="F",
        locked=False,
        pads=pads,
        courtyard=Courtyard(source="courtyard", polygons=[[(-3, -3), (3, -3), (3, 3), (-3, 3)]]),
        uuid="fp-u2",
    )
    assert detect_bga(qfn) is None


def test_starved_thermal_uuid_extraction() -> None:
    violations = [
        {
            "type": "starved_thermal",
            "items": [
                {"description": "Zone (GND) on B.Cu", "uuid": "zone-uuid-1"},
                {"description": "SMD pad 1 of C10", "uuid": "pad-uuid-a"},
            ],
        },
        {
            "type": "starved_thermal",
            "items": [
                {"description": "Zone (GND) on F.Cu", "uuid": "zone-uuid-2"},
                {"description": "PTH pad 2 of JP1", "uuid": "pad-uuid-b"},
            ],
        },
        {
            "type": "clearance",
            "items": [
                {"description": "SMD pad 3 of U1", "uuid": "pad-uuid-ignored"},
            ],
        },
    ]
    assert _starved_thermal_pad_uuids(violations) == ["pad-uuid-a", "pad-uuid-b"]


@_requires_stickhub
def test_safe_eco_place_cluster_preserves_outside_routing(tmp_path: Path) -> None:
    src = _STICKHUB_PATH
    orig = read_board(src)
    assert orig.routing_items > 100

    # Invalid mode raises cleanly.
    with pytest.raises(ValueError, match="unsupported preserve_routing"):
        place_board(src, out_dir=tmp_path / "bad_mode", route=False, cluster_refs=["R1", "R2"], preserve_routing="bad")
    with pytest.raises(ValueError, match="only valid with cluster_refs"):
        place_board(src, out_dir=tmp_path / "no_cluster", route=False, preserve_routing="outside_cluster")

    svc = PlacerService(data_dir=tmp_path / "mcp_runs")
    res_none = svc.place_cluster(str(src), ["R1", "R2", "C1"], seed=0)
    assert read_board(res_none["placed_board"]).routing_items == 0
    assert res_none["preserve_routing"] is None

    res_eco = svc.place_cluster(str(src), ["R1", "R2", "C1"], seed=0, preserve_routing="outside_cluster")
    eco_board = read_board(res_eco["placed_board"])
    pr = res_eco["preserve_routing"]
    assert pr is not None
    assert pr["mode"] == "outside_cluster"
    assert pr["preserved_items"] > orig.routing_items * 0.8
    assert eco_board.routing_items == pr["preserved_items"]

    # Preserved outside copper must incur zero new router-caused DRC violations.
    drc_ref = drc.run(src, tmp_path / "drc_ref.json")
    drc_eco = drc.run(Path(res_eco["placed_board"]), tmp_path / "drc_eco.json")
    attr = drc.attribute(drc_eco, drc_ref)
    assert attr.counts()["router_caused"] == 0


@_requires_stickhub
def test_heal_thermals_helper_modes(tmp_path: Path) -> None:
    src = _STICKHUB_PATH
    dst = tmp_path / src.name
    shutil.copyfile(src, dst)
    board = read_board(dst)
    pad_uuid = board.footprints[0].pads[0].uuid

    for mode in ("relief", "solid"):
        spec = tmp_path / f"spec_{mode}.json"
        out = tmp_path / f"out_{mode}.json"
        spec.write_text(json.dumps({"mode": mode, "pad_uuids": [pad_uuid]}), encoding="utf-8")
        run_helper("heal_thermals", dst, spec, dst, out)
        rep = json.loads(out.read_text(encoding="utf-8"))
        assert rep["mode"] == mode
        assert rep["modified_count"] == 1
        assert rep["modified"][0]["uuid"] == pad_uuid
