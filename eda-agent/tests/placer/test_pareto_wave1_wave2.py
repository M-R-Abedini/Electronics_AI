# SPDX-License-Identifier: MIT
"""Unit tests for Pareto Wave 1 & Wave 2 speed, HDI routing, placement, and lint upgrades."""

from __future__ import annotations

import itertools
import math

from eda_agent.placer import report as run_report
from eda_agent.placer.eval import congestion, legality, lint
from eda_agent.placer.intent import infer
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
    is_power_net,
)
from eda_agent.placer.refine import _baseline_cache_key
from eda_agent.placer.route import bga_escape, dsn, fanout, pairs, s6a


def _rect_outline(x0: float, y0: float, x1: float, y1: float) -> list[Outline]:
    return [Outline(outer=[(x0, y0), (x1, y0), (x1, y1), (x0, y1)], holes=[])]


def _hdi_6l_board() -> Board:
    layers = [
        CopperLayer(name="F.Cu", canonical="F.Cu", index=0),
        CopperLayer(name="In1.Cu", canonical="In1.Cu", index=1),
        CopperLayer(name="In2.Cu", canonical="In2.Cu", index=2),
        CopperLayer(name="In3.Cu", canonical="In3.Cu", index=3),
        CopperLayer(name="In4.Cu", canonical="In4.Cu", index=4),
        CopperLayer(name="B.Cu", canonical="B.Cu", index=5),
    ]
    nc = NetClass(
        name="Default",
        clearance=0.09,
        track_width=0.10,
        via_diameter=0.40,
        via_drill=0.20,
        microvia_diameter=0.20,
        microvia_drill=0.10,
    )
    rules = {
        "min_track_width": 0.09,
        "min_clearance": 0.09,
        "min_via_diameter": 0.40,
        "min_through_drill": 0.20,
        "allow_microvias": True,
        "min_microvia_diameter": 0.20,
        "min_microvia_drill": 0.10,
        "min_via_annular_width": 0.05,
        "hole_clearance": 0.15,
        "hole_to_hole": 0.15,
        "copper_edge_clearance": 0.15,
    }
    zone_in2 = {
        "uuid": "z-in2",
        "net": "GND",
        "layers": ["In2.Cu"],
        "outline": [[(0.2, 0.2), (19.8, 0.2), (19.8, 19.8), (0.2, 19.8)]],
        "filled": True,
        "filled_area_mm2": {"In2.Cu": 380.0},
    }
    zone_in4 = {
        "uuid": "z-in4",
        "net": "GND",
        "layers": ["In4.Cu"],
        "outline": [[(0.2, 0.2), (19.8, 0.2), (19.8, 19.8), (0.2, 19.8)]],
        "filled": True,
        "filled_area_mm2": {"In4.Cu": 380.0},
    }
    board = Board(
        name="test_hdi6l",
        source={"path": "test.kicad_pcb"},
        outline=_rect_outline(0.0, 0.0, 20.0, 20.0),
        board_area_mm2=400.0,
        copper_layers=layers,
        stackup=Stackup(source="test", roles=[]),
        netclasses=[nc],
        nets=[Net("GND", "Default", []), Net("SIG1", "Default", []), Net("1V8_GNSS", "Default", [])],
        footprints=[],
        rules=rules,
        zones=[zone_in2, zone_in4],
    )
    return board


def test_pad_union_cache_respects_in_place_pad_mutations():
    board = _hdi_6l_board()
    fp1 = Footprint(
        ref="R1", value="10k", lib_id="R_0402", x=5.0, y=5.0, rotation=0.0, side="F",
        courtyard=Courtyard("file", polygons=[[(-0.5, -0.3), (0.5, -0.3), (0.5, 0.3), (-0.5, 0.3)]]),
        pads=[Pad("1", "GND", (-0.4, 0.0), (0.4, 0.4), kind="smd", layers=("F.Cu",))],
    )
    fp2 = Footprint(
        ref="R2", value="10k", lib_id="R_0402", x=7.0, y=5.0, rotation=0.0, side="F",
        courtyard=Courtyard("file", polygons=[[(-0.5, -0.3), (0.5, -0.3), (0.5, 0.3), (-0.5, 0.3)]]),
        pads=[Pad("1", "SIG1", (-0.4, 0.0), (0.4, 0.4), kind="smd", layers=("F.Cu",))],
    )
    board.footprints = [fp1, fp2]
    assert legality.check(board) == []
    # Mutate pad size in place so pads now collide across the gap; cache MUST NOT return stale union.
    fp1.pads[0].size = (4.0, 0.4)
    findings = legality.check(board)
    assert any(f.kind == "courtyard_overlap" and "pad" in f.detail for f in findings)


def test_pad_exit_dogleg_and_collinear_segment_dedup():
    pts = pairs._pad_exit_dogleg((0.0, 0.0), (0.3, 0.1))
    assert len(pts) == 3
    for a, b in itertools.pairwise(pts):
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        assert dx < 1e-5 or dy < 1e-5 or abs(dx - dy) < 1e-5

    tracks = [
        {"net": "1V8_GNSS", "layer": "F.Cu", "width": 0.10, "points": [(10.0, 10.0), (10.0, 10.5)]},
        {"net": "1V8_GNSS", "layer": "F.Cu", "width": 0.10, "points": [(10.0, 10.0), (10.0, 11.2)]},
        {"net": "1V8_GNSS", "layer": "F.Cu", "width": 0.10, "points": [(10.0, 11.2), (10.0, 11.2)]},
    ]
    deduped = s6a._segments(tracks)
    assert len(deduped) == 1
    assert math.isclose(math.dist(deduped[0]["points"][0], deduped[0]["points"][1]), 1.2, abs_tol=1e-4)


def test_dsn_enable_vippo_attach_only_microvias_and_sets_via_at_smd():
    sample = """(pcb test
  (structure
    (layer F.Cu (type signal))
    (via "Via[0-5]_400:200_um")
  )
  (library
    (padstack "Via[0-5]_400:200_um"
      (shape (circle F.Cu 400))
      (attach off)
    )
    (padstack "Via[0-1]_200:100_um"
      (shape (circle F.Cu 200))
      (shape (circle In1.Cu 200))
      (attach off)
    )
    (padstack "Via[0-2]_200:100_um"
      (shape (circle F.Cu 200))
      (shape (circle In1.Cu 200))
      (shape (circle In2.Cu 200))
      (attach off)
    )
  )
  (wiring
    (via "Via[0-1]_200:100_um" 1000 -1000(net SIG1)(type fix))
    (via "Via[0-2]_200:100_um" 2000 -1000(net GND)(type fix))
    (via "Via[0-5]_400:200_um" 3000 -1000(net GND)(type fix))
  )
)"""
    out, count = dsn.enable_vippo_attach(sample, layer_count=6)
    assert count == 2
    assert "(control (via_at_smd on))" in out
    # Microvia padstacks got (attach on); through-via padstack stayed (attach off).
    ps05 = out.split('(padstack "Via[0-5]_400:200_um"')[1].split(")")[2]
    assert "attach off" in ps05
    ps01 = out.split('(padstack "Via[0-1]_200:100_um"')[1].split("(padstack")[0]
    assert "(attach on)" in ps01
    ps02 = out.split('(padstack "Via[0-2]_200:100_um"')[1].split("(wiring")[0]
    assert "(attach on)" in ps02


def test_wlcsp_detection_and_plane_aware_adj2_escape():
    board = _hdi_6l_board()
    # 3x4 0.40 mm WLCSP on B.Cu (adj1 = In4.Cu is GND plane, adj2 = In3.Cu is signal).
    pads = []
    idx = 0
    for r in range(3):
        for c in range(4):
            idx += 1
            net = "SIG1" if (r == 1 and c == 1) else "GND"
            pads.append(Pad(f"P{idx}", net, ((c - 1.5) * 0.40, (r - 1.0) * 0.40), (0.20, 0.20), kind="smd", layers=("B.Cu",)))
    u8 = Footprint(
        ref="U8", value="BMM350", lib_id="WLCSP-12", x=10.0, y=10.0, rotation=0.0, side="B",
        courtyard=Courtyard("file", polygons=[[(-1.0, -0.8), (1.0, -0.8), (1.0, 0.8), (-1.0, 0.8)]]),
        pads=pads,
    )
    board.footprints = [u8]
    det = bga_escape.detect_bga(u8)
    assert det is not None
    assert det["ncols"] == 4 and det["nrows"] == 3

    from eda_agent.placer.ir import derive

    board.zone_fill_by_layer_mm2 = {"In2.Cu": {"GND": 380.0}, "In4.Cu": {"GND": 380.0}}
    derive(board)
    res = bga_escape.plan_bga_escapes(
        board, {"parts": {}, "nets": {}, "diff_pairs": []},
        fixed=[], include_signals=True, microvia="auto",
    )
    sig_rows = [row for row in res["rows"] if row["net"] == "SIG1"]
    assert len(sig_rows) == 1
    assert sig_rows[0]["routed"] is True
    # Must escalate past In4.Cu (GND plane) to In3.Cu (adj2) instead of dead-ending on In4.Cu.
    sig_vias = [c for c in res["copper"] if "points" not in c and c["net"] == "SIG1"]
    assert any("In3.Cu" in v["layers"] for v in sig_vias)


def test_hdi_microvia_plane_tap_ignores_opposite_side_pad():
    board = _hdi_6l_board()
    # Huge backside GND pad TP15 sitting directly underneath F.Cu capacitor C1.
    tp15 = Footprint(
        ref="TP15", value="TP", lib_id="TestPoint", x=10.0, y=10.0, rotation=0.0, side="B",
        courtyard=Courtyard("file", polygons=[[(-4.0, -2.0), (4.0, -2.0), (4.0, 2.0), (-4.0, 2.0)]]),
        pads=[Pad("1", "GND", (0.0, 0.0), (8.0, 4.0), kind="smd", layers=("B.Cu",))],
    )
    c1 = Footprint(
        ref="C1", value="100nF", lib_id="C_0201", x=10.0, y=10.0, rotation=0.0, side="F",
        courtyard=Courtyard("file", polygons=[[(-0.4, -0.2), (0.4, -0.2), (0.4, 0.2), (-0.4, 0.2)]]),
        pads=[
            Pad("1", "1V8_GNSS", (-0.3, 0.0), (0.25, 0.25), kind="smd", layers=("F.Cu",)),
            Pad("2", "GND", (0.3, 0.0), (0.25, 0.25), kind="smd", layers=("F.Cu",)),
        ],
    )
    board.footprints = [tp15, c1]
    # Through-via tap fails because TP15 occupies B.Cu underneath; HDI microvia tap succeeds to In2.Cu.
    assert fanout.plan_power_tap(board, c1, c1.pads[1], "In2.Cu", fixed=[], allow_microvia=False) is None
    got = fanout.plan_power_tap(board, c1, c1.pads[1], "In2.Cu", fixed=[], allow_microvia=True)
    assert got is not None
    _track, vias = got
    assert isinstance(vias, list) and len(vias) == 2
    assert vias[0]["layers"] == ["F.Cu", "In1.Cu"]
    assert vias[1]["layers"] == ["In1.Cu", "In2.Cu"]


def test_congestion_unfilled_plane_zones_and_intent_lint_honesty(tmp_path):
    board = _hdi_6l_board()
    # Unfilled zones on In2.Cu and In4.Cu should count as 4.0 signal layers, NOT 6.0!
    assert congestion._effective_signal_layers(board) == 4.0
    assert "GND" in congestion._plane_nets(board)

    # Crystal X1 detection vs regular X2 connector
    x_xtal = Footprint(
        ref="X1", value="32.768kHz", lib_id="FC-135", x=2.0, y=2.0, rotation=0.0, side="F",
        courtyard=Courtyard("file", polygons=[[(-1.0, -0.5), (1.0, -0.5), (1.0, 0.5), (-1.0, 0.5)]]),
        pads=[
            Pad("1", "RTC_XIN", (-0.5, 0.0), (0.3, 0.3), kind="smd", layers=("F.Cu",)),
            Pad("2", "RTC_XOUT", (0.5, 0.0), (0.3, 0.3), kind="smd", layers=("F.Cu",)),
        ],
    )
    x_conn = Footprint(
        ref="X2", value="HDR_2P", lib_id="PinHeader_1x02", x=6.0, y=2.0, rotation=0.0, side="F",
        courtyard=Courtyard("file", polygons=[[(-1.0, -0.5), (1.0, -0.5), (1.0, 0.5), (-1.0, 0.5)]]),
        pads=[
            Pad("1", "GPIO1", (-0.5, 0.0), (0.3, 0.3), kind="smd", layers=("F.Cu",)),
            Pad("2", "GPIO2", (0.5, 0.0), (0.3, 0.3), kind="smd", layers=("F.Cu",)),
        ],
    )
    assert infer._family(x_xtal) == "crystal"
    assert infer._family(x_conn) == "connector"

    # Broadened power-net recognition
    assert is_power_net("1V8_GNSS")
    assert is_power_net("VCORE")
    assert is_power_net("VIO18")
    assert is_power_net("VRF")

    # 6L routed board with zero filled planes must flag missing_reference_plane (not fake 100.0).
    board.zones = []
    board.routing = {"tracks": [{"net": "SIG1", "layer": "F.Cu", "width": 0.1, "points": [(1.0, 1.0), (5.0, 1.0)]}], "vias": []}
    res = lint.audit(board, {"parts": {}, "nets": {}, "diff_pairs": []})
    assert any(f["rule"] == "missing_reference_plane" for f in res["findings"])
    assert res["score"] < 100.0

    # report.tools populates placer_core and refine cache key distinguishes s6a/intent.
    f_a = tmp_path / "a.kicad_pcb"
    f_a.write_text("(kicad_pcb)", encoding="utf-8")
    k1 = _baseline_cache_key(f_a, f_a, 180, s6a_enabled=False, intent_sha256="aaa")
    k2 = _baseline_cache_key(f_a, f_a, 180, s6a_enabled=True, intent_sha256="aaa")
    k3 = _baseline_cache_key(f_a, f_a, 180, s6a_enabled=True, intent_sha256="bbb")
    assert len({k1, k2, k3}) == 3
    tools = run_report.tools()
    assert tools["placer_core"] is not None
