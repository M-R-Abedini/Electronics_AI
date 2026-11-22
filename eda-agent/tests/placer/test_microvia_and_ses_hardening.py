# SPDX-License-Identifier: MIT
"""Tests for Microvia-in-Pad (L1-L2 / staggered L2-L3) and DSN/SES hardening."""

from __future__ import annotations

import json
import math
import uuid
from pathlib import Path

import pytest
from eda_agent.placer.io.kicad import read_board, run_helper
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
from eda_agent.placer.macro_proof import apply_instance
from eda_agent.placer.route import drc, dsn, s6a
from eda_agent.placer.route.bga_escape import plan_bga_escapes
from eda_agent.placer.route.check import route_check
from eda_agent.placer.route.copper import holes_separated, via_layer_span
from eda_agent.placer.toolchain import ToolchainError, freerouting, kicad


def _has_kicad_and_freerouting() -> bool:
    try:
        kicad()
        freerouting()
        return True
    except (ToolchainError, OSError):
        return False


_REQUIRES_KICAD_FREEROUTING = pytest.mark.skipif(
    not _has_kicad_and_freerouting(),
    reason="requires local KiCad 10 and Freerouting",
)


def _synthetic_6l_fine_bga_board(*, allow_microvias: bool) -> Board:
    """Create a 6-layer board with a 0.50 mm pitch 6x6 BGA and GND (In1.Cu) + VDD (In2.Cu) planes."""
    layers = [
        CopperLayer("F.Cu", "F.Cu", 0),
        CopperLayer("In1.Cu", "In1.Cu", 1, "power"),
        CopperLayer("In2.Cu", "In2.Cu", 2, "power"),
        CopperLayer("In3.Cu", "In3.Cu", 3),
        CopperLayer("In4.Cu", "In4.Cu", 4),
        CopperLayer("B.Cu", "B.Cu", 5),
    ]
    rows = "ABCDEF"
    pitch = 0.50
    pads: list[Pad] = []
    for r_idx, r_ch in enumerate(rows):
        for c_idx in range(1, 7):
            lx = (c_idx - 3.5) * pitch
            ly = (r_idx - 2.5) * pitch
            is_outer = r_idx in (0, 5) or c_idx in (1, 6)
            if is_outer:
                net = f"SIG_{r_ch}{c_idx}"
            elif (r_idx + c_idx) % 2 == 0:
                net = "GND"
            else:
                net = "VDD"
            pads.append(
                Pad(
                    number=f"{r_ch}{c_idx}",
                    net=net,
                    local=(lx, ly),
                    size=(0.26, 0.26),
                    shape="circle",
                    kind="smd",
                    layers=("F.Cu",),
                )
            )
    bga_fp = Footprint(
        ref="U1",
        value="BGA36_050",
        lib_id="Package_BGA:BGA-36_3.5x3.5mm_0.5mm",
        x=15.0,
        y=15.0,
        rotation=0.0,
        side="F",
        pads=pads,
        courtyard=Courtyard("file", [[(-2.0, -2.0), (2.0, -2.0), (2.0, 2.0), (-2.0, 2.0)]]),
        uuid="u-bga1",
    )
    # Companion pads so GND and VDD have >= 2 pads and form real nets.
    hdr_pads = [
        Pad("1", "GND", (0.0, 0.0), (1.0, 1.0), shape="circle", kind="smd", layers=("F.Cu",)),
        Pad("2", "VDD", (2.0, 0.0), (1.0, 1.0), shape="circle", kind="smd", layers=("F.Cu",)),
    ]
    hdr_fp = Footprint(
        ref="J1",
        value="PWR",
        lib_id="Connector:Hdr2",
        x=5.0,
        y=5.0,
        rotation=0.0,
        side="F",
        pads=hdr_pads,
        courtyard=Courtyard("file", [[(-1.0, -1.0), (3.0, -1.0), (3.0, 1.0), (-1.0, 1.0)]]),
        uuid="u-hdr1",
    )
    outline_pts = [(0.0, 0.0), (30.0, 0.0), (30.0, 30.0), (0.0, 30.0)]
    zones = [
        {
            "uuid": "z-gnd",
            "name": "GND_PLANE",
            "net": "GND",
            "layers": ["In1.Cu"],
            "outline": [outline_pts],
        },
        {
            "uuid": "z-vdd",
            "name": "VDD_PLANE",
            "net": "VDD",
            "layers": ["In2.Cu"],
            "outline": [outline_pts],
        },
    ]
    nets_map: dict[str, list[tuple[str, str]]] = {}
    for fp in (bga_fp, hdr_fp):
        for p in fp.pads:
            nets_map.setdefault(p.net, []).append((fp.ref, p.number))
    nets = [Net(name=n, netclass="Default", pads=len(ps)) for n, ps in sorted(nets_map.items())]
    nc = NetClass(
        name="Default",
        clearance=0.10,
        track_width=0.10,
        via_diameter=0.45,
        via_drill=0.25,
        microvia_diameter=0.20,
        microvia_drill=0.10,
    )
    rules = {
        "min_clearance": 0.10,
        "min_track_width": 0.10,
        "min_via_diameter": 0.45,
        "min_through_drill": 0.25,
        "min_via_annular_width": 0.05,
        "hole_to_hole": 0.15,
        "allow_microvias": allow_microvias,
        "allow_blind_buried_vias": False,
        "min_microvia_diameter": 0.20,
        "min_microvia_drill": 0.10,
    }
    board = Board(
        name="bga_6l_uvia",
        footprints=[bga_fp, hdr_fp],
        nets=nets,
        netclasses=[nc],
        outline=[Outline(outer=outline_pts, holes=[])],
        board_area_mm2=900.0,
        copper_layers=layers,
        zones=zones,
        zone_fill_by_layer_mm2={"In1.Cu": {"GND": 850.0}, "In2.Cu": {"VDD": 850.0}},
        rules=rules,
    )
    return derive(board)


def test_via_layer_span_and_holes_separated_layer_aware():
    ordered = ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"]
    v_l1_l2 = {"x": 10.0, "y": 10.0, "diameter": 0.20, "drill": 0.10, "layers": ["F.Cu", "In1.Cu"], "via_type": "micro"}
    v_l2_l3 = {"x": 10.0, "y": 10.0, "diameter": 0.20, "drill": 0.10, "layers": ["In1.Cu", "In2.Cu"], "via_type": "micro"}
    v_bot = {"x": 10.0, "y": 10.0, "diameter": 0.20, "drill": 0.10, "layers": ["In4.Cu", "B.Cu"], "via_type": "micro"}
    v_thru = {"x": 10.0, "y": 10.0, "diameter": 0.45, "drill": 0.25, "layers": ["F.Cu", "B.Cu"], "via_type": "through"}

    assert via_layer_span(v_l1_l2, ordered) == {"F.Cu", "In1.Cu"}
    assert via_layer_span(v_l2_l3, ordered) == {"In1.Cu", "In2.Cu"}
    assert via_layer_span(v_thru, ordered) == set(ordered)

    # Disjoint spans (F.Cu..In1.Cu vs In4.Cu..B.Cu) at same XY do NOT collide.
    assert holes_separated([v_l1_l2], [(10.0, 10.0, 0.10, frozenset({"In4.Cu", "B.Cu"}))], 0.15, ordered_layers=ordered)
    assert holes_separated([v_l1_l2, v_bot], [], 0.15, ordered_layers=ordered)

    # Shared In1.Cu (F.Cu..In1.Cu vs In1.Cu..In2.Cu) at same XY MUST collide (staggered only, no stacked).
    assert not holes_separated([v_l1_l2, v_l2_l3], [], 0.15, ordered_layers=ordered)

    # Microvia and through-via at same XY MUST collide.
    assert not holes_separated([v_l1_l2], [(10.0, 10.0, 0.25, frozenset(ordered))], 0.15, ordered_layers=ordered)

    # Legacy 3-tuple hole entries remain backwards-compatible and always collide as through-holes.
    assert not holes_separated([v_l1_l2], [(10.0, 10.0, 0.25)], 0.15, ordered_layers=ordered)


def test_dsn_restrict_structure_vias_and_class_via_rule():
    sample_dsn = """(pcb "demo.dsn"
  (structure
    (layer F.Cu (type signal))
    (layer In1.Cu (type power))
    (layer In2.Cu (type power))
    (layer In3.Cu (type signal))
    (layer In4.Cu (type signal))
    (layer B.Cu (type signal))
    (via "Via[0-5]_600:300_um" "Via[0-1]_200:100_um" "Via[1-2]_200:100_um")
    (rule (width 150) (clearance 150))
  )
  (network
    (class kicad_default ""
      (circuit (use_via "Via[0-5]_600:300_um"))
      (rule (width 150) (clearance 150))
    )
    (class BGA_HDI "SIG1"
      (rule (width 100) (clearance 100))
    )
  )
  (library
    (padstack "Via[0-5]_600:300_um" (shape (circle F.Cu 600)))
    (padstack "Via[0-1]_200:100_um" (shape (circle F.Cu 200)))
    (padstack "Via[1-2]_200:100_um" (shape (circle In1.Cu 200)))
  )
  (wiring
    (via "Via[0-1]_200:100_um" 1000 2000 (net GND) (type fix))
  )
)
"""
    restricted, removed = dsn.restrict_structure_vias(sample_dsn, layer_count=6, allow_blind_micro=False)
    assert removed == ["Via[0-1]_200:100_um", "Via[1-2]_200:100_um"]
    assert '(via "Via[0-5]_600:300_um")' in restricted
    # Library padstacks and S6a fixed wiring microvias MUST remain intact.
    assert '(padstack "Via[0-1]_200:100_um"' in restricted
    assert '(padstack "Via[1-2]_200:100_um"' in restricted
    assert '(via "Via[0-1]_200:100_um" 1000 2000 (net GND) (type fix))' in restricted

    # Opt-in allow_blind_micro=True is a no-op.
    kept, removed_none = dsn.restrict_structure_vias(sample_dsn, layer_count=6, allow_blind_micro=True)
    assert kept == sample_dsn
    assert removed_none == []

    # Opt-in Phase 2 class-scoped HDI via rule.
    scoped = dsn.add_class_via_rule(restricted, "BGA_HDI", ["Via[0-1]_200:100_um", "Via[1-2]_200:100_um"])
    assert '(circuit (use_via "Via[0-1]_200:100_um" "Via[1-2]_200:100_um"))' in scoped


def test_bga_microvia_in_pad_cost_gate_and_l1_l2_l3_staggered():
    # 1) HDI cost gate: allow_microvias=False must NEVER silently emit microvias.
    board_gated = _synthetic_6l_fine_bga_board(allow_microvias=False)
    res_gated = plan_bga_escapes(board_gated, {"parts": {}, "nets": {}}, fixed=[], microvia="auto")
    assert res_gated["copper"] == []
    assert res_gated["rows"]
    assert all(r.get("failed") == "pitch_requires_microvia_or_vip" for r in res_gated["rows"])

    # 2) Explicit microvia="off" even when board allows microvias.
    board_hdi = _synthetic_6l_fine_bga_board(allow_microvias=True)
    res_off = plan_bga_escapes(board_hdi, {"parts": {}, "nets": {}}, fixed=[], microvia="off")
    assert res_off["copper"] == []
    assert all(r.get("failed") == "pitch_requires_microvia_or_vip" for r in res_off["rows"])

    # 3) allow_microvias=True + microvia="auto": L1-L2 VIPPO for GND (In1.Cu) and staggered L1-L2+L2-L3 for VDD (In2.Cu).
    res_auto = plan_bga_escapes(board_hdi, {"parts": {}, "nets": {}}, fixed=[], microvia="auto")
    routed = [r for r in res_auto["rows"] if r.get("routed")]
    assert len(routed) == 16  # 4x4 interior balls of 6x6 BGA
    l1_l2_rows = [r for r in routed if r.get("kind") == "microvia_in_pad_l1_l2"]
    l1_l2_l3_rows = [r for r in routed if r.get("kind") == "microvia_l1_l2_l3_staggered"]
    assert len(l1_l2_rows) == 8  # GND balls -> In1.Cu
    assert len(l1_l2_l3_rows) == 8  # VDD balls -> In2.Cu via staggered L1-L2 + L2-L3

    vias = [c for c in res_auto["copper"] if "points" not in c]
    tracks = [c for c in res_auto["copper"] if "points" in c]
    assert len(vias) == 8 + 16  # 8 single VIPPO + 8 * 2 staggered microvias
    assert len(tracks) == 8  # 8 In1.Cu stagger stubs
    assert all(v["via_type"] == "micro" for v in vias)
    assert all(t["layer"] == "In1.Cu" for t in tracks)

    # Every L1-L2 VIPPO is IPC-4761 Type VII filled + capped.
    l1_l2_vias = [v for v in vias if tuple(v["layers"]) == ("F.Cu", "In1.Cu")]
    l2_l3_vias = [v for v in vias if tuple(v["layers"]) == ("In1.Cu", "In2.Cu")]
    assert len(l1_l2_vias) == 16
    assert len(l2_l3_vias) == 8
    assert all(v.get("filled") is True and v.get("capped") is True for v in l1_l2_vias)

    # Verify strict staggering: no L2-L3 microvia is stacked directly on any L1-L2 microvia.
    for v2 in l2_l3_vias:
        nearest = min(math.hypot(v2["x"] - v1["x"], v2["y"] - v1["y"]) for v1 in l1_l2_vias)
        assert nearest >= 0.299  # >= diameter (0.20) + clearance (0.10)

    # Verify s6a._instance propagates via_type, filled, and capped into MVia.
    inst = s6a._instance(res_auto["copper"])
    assert len(inst.vias) == len(vias)
    assert all(mv.via_type == "micro" for mv in inst.vias)
    assert sum(mv.filled and mv.capped for mv in inst.vias) == 16


def _write_6l_hdi_kicad_fixture(tmp_dir: Path) -> Path:
    """Create a real KiCad 10 6-layer HDI board with 0.50 mm BGA, interior cutout, and Power netclass."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    pcb_path = tmp_dir / "hdi_bga6l.kicad_pcb"
    pro_path = tmp_dir / "hdi_bga6l.kicad_pro"

    pro_data = {
        "board": {
            "design_settings": {
                "rules": {
                    "allow_blind_buried_vias": False,
                    "allow_microvias": True,
                    "min_clearance": 0.10,
                    "min_copper_edge_clearance": 0.30,
                    "min_hole_clearance": 0.15,
                    "min_hole_to_hole": 0.15,
                    "min_microvia_diameter": 0.20,
                    "min_microvia_drill": 0.10,
                    "min_through_hole_diameter": 0.20,
                    "min_track_width": 0.10,
                    "min_via_annular_width": 0.05,
                    "min_via_diameter": 0.45,
                },
                "defaults": {
                    "board_outline_line_width": 0.10,
                },
            }
        },
        "net_settings": {
            "classes": [
                {
                    "name": "Default",
                    "clearance": 0.10,
                    "track_width": 0.15,
                    "via_diameter": 0.50,
                    "via_drill": 0.30,
                    "microvia_diameter": 0.20,
                    "microvia_drill": 0.10,
                },
                {
                    "name": "Power",
                    "clearance": 0.12,
                    "track_width": 0.25,
                    "via_diameter": 0.45,
                    "via_drill": 0.20,
                    "microvia_diameter": 0.20,
                    "microvia_drill": 0.10,
                },
            ],
            "netclass_assignments": {
                "VDD": ["Power"],
            },
        },
    }
    pro_path.write_text(json.dumps(pro_data, indent=2), encoding="utf-8")

    # Build a clean 6-layer KiCad 9/10 s-expression PCB with:
    # - 5x5 (25-ball) 0.50 mm pitch BGA (U1) at (115, 115): interior balls include 2x GND (In1.Cu)
    #   and 2x VDD (In2.Cu); outer signal balls connect to header J1 at (104, 112).
    # - Interior Edge.Cuts slot cutout at (123..126, 108..112) to verify edge_cutout obstacle export.
    nets = [(0, ""), (1, "GND"), (2, "VDD"), (3, "SIG_A1"), (4, "SIG_E5")]
    net_id = {name: idx for idx, name in nets}
    net_lines = "\n".join(f'  (net {idx} "{name}")' for idx, name in nets)

    bga_pads = []
    rows = "ABCDE"
    pitch = 0.50
    for r_idx, r_ch in enumerate(rows):
        for c_idx in range(1, 6):
            lx = round((c_idx - 3.0) * pitch, 4)
            ly = round((r_idx - 2.0) * pitch, 4)
            num = f"{r_ch}{c_idx}"
            if num in ("B2", "D4"):
                net_name = "GND"
            elif num in ("B4", "D2"):
                net_name = "VDD"
            elif num == "A1":
                net_name = "SIG_A1"
            elif num == "E5":
                net_name = "SIG_E5"
            else:
                net_name = ""
            nid = net_id[net_name]
            net_sexp = f' (net {nid} "{net_name}")' if net_name else ""
            bga_pads.append(
                f'    (pad "{num}" smd circle (at {lx} {ly}) (size 0.26 0.26) '
                f'(layers "F.Cu" "F.Paste" "F.Mask"){net_sexp} (uuid "{uuid.uuid4()}"))'
            )

    hdr_pads = [
        f'    (pad "1" thru_hole circle (at 0 0) (size 1.2 1.2) (drill 0.6) (layers "*.Cu" "*.Mask") (net 1 "GND") (uuid "{uuid.uuid4()}"))',
        f'    (pad "2" thru_hole circle (at 0 2.54) (size 1.2 1.2) (drill 0.6) (layers "*.Cu" "*.Mask") (net 2 "VDD") (uuid "{uuid.uuid4()}"))',
        f'    (pad "3" smd rect (at 0 5.08) (size 1.0 0.8) (layers "F.Cu" "F.Paste" "F.Mask") (net 3 "SIG_A1") (uuid "{uuid.uuid4()}"))',
        f'    (pad "4" smd rect (at 0 7.62) (size 1.0 0.8) (layers "F.Cu" "F.Paste" "F.Mask") (net 4 "SIG_E5") (uuid "{uuid.uuid4()}"))',
    ]

    def _zone(net_num: int, net_name: str, layer: str) -> str:
        return f"""  (zone
    (net {net_num})
    (net_name "{net_name}")
    (layer "{layer}")
    (uuid "{uuid.uuid4()}")
    (hatch edge 0.5)
    (connect_pads (clearance 0.15))
    (min_thickness 0.15)
    (fill yes (thermal_gap 0.2) (thermal_bridge_width 0.2))
    (polygon (pts (xy 101 101) (xy 129 101) (xy 129 129) (xy 101 129)))
    (filled_polygon (layer "{layer}") (pts (xy 101 101) (xy 129 101) (xy 129 129) (xy 101 129)))
  )"""

    pcb_text = f"""(kicad_pcb
  (version 20241229)
  (generator "electronics_ai_test")
  (generator_version "10.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (4 "In1.Cu" power)
    (6 "In2.Cu" power)
    (8 "In3.Cu" signal)
    (10 "In4.Cu" signal)
    (2 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (44 "Edge.Cuts" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user)
    (49 "F.Fab" user)
  )
  (setup
    (stackup
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "prepreg") (thickness 0.1))
      (layer "In1.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 2" (type "core") (thickness 0.2))
      (layer "In2.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 3" (type "core") (thickness 0.8))
      (layer "In3.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 4" (type "core") (thickness 0.2))
      (layer "In4.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 5" (type "prepreg") (thickness 0.1))
      (layer "B.Cu" (type "copper") (thickness 0.035))
    )
    (pad_to_mask_clearance 0)
    (allow_soldermask_bridges_in_footprints no)
  )
{net_lines}
  (gr_rect (start 100 100) (end 130 130) (stroke (width 0.1) (type solid)) (fill none) (layer "Edge.Cuts") (uuid "{uuid.uuid4()}"))
  (gr_rect (start 123 108) (end 126 112) (stroke (width 0.1) (type solid)) (fill none) (layer "Edge.Cuts") (uuid "{uuid.uuid4()}"))
  (footprint "Package_BGA:BGA-16_0.5mm"
    (layer "F.Cu")
    (uuid "{uuid.uuid4()}")
    (at 115 115)
    (property "Reference" "U1" (at 0 -2.5 0) (layer "F.SilkS") (uuid "{uuid.uuid4()}") (effects (font (size 1 1) (thickness 0.15))))
    (property "Value" "BGA16" (at 0 2.5 0) (layer "F.Fab") (uuid "{uuid.uuid4()}") (effects (font (size 1 1) (thickness 0.15))))
    (fp_rect (start -1.5 -1.5) (end 1.5 1.5) (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd") (uuid "{uuid.uuid4()}"))
{chr(10).join(bga_pads)}
  )
  (footprint "Connector:Hdr4"
    (layer "F.Cu")
    (uuid "{uuid.uuid4()}")
    (at 104 112)
    (property "Reference" "J1" (at 0 -2 0) (layer "F.SilkS") (uuid "{uuid.uuid4()}") (effects (font (size 1 1) (thickness 0.15))))
    (property "Value" "HDR4" (at 0 9.5 0) (layer "F.Fab") (uuid "{uuid.uuid4()}") (effects (font (size 1 1) (thickness 0.15))))
    (fp_rect (start -1.2 -1.2) (end 1.2 8.8) (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd") (uuid "{uuid.uuid4()}"))
{chr(10).join(hdr_pads)}
  )
{_zone(1, "GND", "In1.Cu")}
{_zone(2, "VDD", "In2.Cu")}
)
"""
    pcb_path.write_text(pcb_text, encoding="utf-8", newline="\n")
    return pcb_path


@_REQUIRES_KICAD_FREEROUTING
def test_kicad10_e2e_microvia_l1_l2_l3_drc_dsn_freerouting_ses_and_hardening(tmp_path: Path):
    pcb_path = _write_6l_hdi_kicad_fixture(tmp_path / "src")

    # 1) Read board & verify microvia rules + netclass microvia fields are extracted from .kicad_pro.
    board = read_board(pcb_path)
    assert board.rules.get("allow_microvias") is True
    assert board.rules.get("min_microvia_diameter") == pytest.approx(0.20, abs=1e-4)
    assert board.rules.get("min_microvia_drill") == pytest.approx(0.10, abs=1e-4)
    default_nc = board.netclass_of("SIG_A1")
    assert default_nc is not None
    assert default_nc.microvia_diameter == pytest.approx(0.20, abs=1e-4)
    assert default_nc.microvia_drill == pytest.approx(0.10, abs=1e-4)

    # 2) Run S6a BGA escape with microvia="auto" -> write L1-L2 VIPPO and staggered L1-L2 + L2-L3.
    intent = {"parts": {}, "nets": {}, "diff_pairs": []}
    planned = s6a.plan(board, intent, pairs=False, links=False, taps=False, escapes=True, microvia="auto")
    copper = planned["copper"]
    assert len([c for c in copper if "points" not in c and c.get("via_type") == "micro"]) == 6  # 2x L1-L2 + 2x (L1-L2 + L2-L3)

    s6a_pcb = tmp_path / "s6a" / pcb_path.name
    apply_instance(pcb_path, s6a._instance(copper), s6a_pcb, keep_only=None, strip="tracks")

    # 3) Verify ZERO KiCad 10 DRC errors touching the microvias / S6a copper.
    drc_rep = drc.run(s6a_pcb, tmp_path / "s6a" / "drc.json")
    via_errors = [
        v for v in drc_rep.get("violations", [])
        if v.get("severity", "error") == "error"
        and v.get("type") not in ("via_dangling", "silk_over_copper", "silk_overlap")
    ]
    assert via_errors == [], f"unexpected KiCad DRC errors on S6a microvias: {via_errors}"

    # 4) Run full route_check (prepare -> edge_cutout keepouts -> DSN restrict_structure_vias -> Freerouting -> finish -> DRC).
    rc = route_check(s6a_pcb, out_dir=tmp_path / "route", reference_board=pcb_path, router_timeout_s=60)
    assert rc["passed"] is True, f"route_check failed: {rc['gates']}"
    assert rc["metrics"]["coverage"] == 1.0
    assert rc["stages"]["obstacle_keepouts"]["by_kind"].get("edge_cutout", 0) >= 6
    assert rc["stages"]["dsn_restricted_vias"], "expected non-through via padstacks restricted in DSN structure"

    # 5) Read back routed PCB and confirm both L1-L2 and L2-L3 microvias survived Freerouting + SES import!
    routed_board = read_board(rc["artifacts"]["routed_board"])
    routed_micros = [v for v in routed_board.routing["vias"] if v.get("via_type") == "micro"]
    l1_l2 = [v for v in routed_micros if tuple(v["layers"]) == ("F.Cu", "In1.Cu")]
    l2_l3 = [v for v in routed_micros if tuple(v["layers"]) == ("In1.Cu", "In2.Cu")]
    assert len(l1_l2) == 4
    assert len(l2_l3) == 2

    # 6) Verify SES hardening: bogus duplicate placement entry is sanitized cleanly, and custom-drill restoration runs.
    ses_orig = Path(rc["artifacts"]["session"]).read_text(encoding="utf-8")
    ses_dirty = ses_orig.replace(
        "(placement\n",
        '(placement\n    (component "Ghost:FP"\n      (place "REF**_1" 100000 -100000 front 0)\n    )\n',
        1,
    )
    dirty_ses_path = tmp_path / "dirty.ses"
    dirty_ses_path.write_text(ses_dirty, encoding="utf-8")
    dirty_out = tmp_path / "dirty_out.kicad_pcb"
    dirty_json = tmp_path / "dirty_finish.json"
    run_helper("finish", s6a_pcb, dirty_ses_path, dirty_out, dirty_json)
    fin = json.loads(dirty_json.read_text(encoding="utf-8"))
    assert fin["imported"] is True
    assert "REF**_1" in fin["ses_placement_skipped"]
    assert isinstance(fin["via_drills_restored"], dict)
    assert isinstance(fin["via_drills_restored"]["count"], int)

    # 7) Verify custom-drill restoration on KiCad 10 ImportSpecctraSES:
    #    KiCad 10 clears a via's drill when it matches Default's drill (0.30 mm), causing a Power-class
    #    net (VDD, class drill 0.20 mm) with Via[0-5]_500:300_um to resolve to 0.20 mm instead of 0.30 mm.
    #    _restore_via_drills restores the exact 0.30 mm drill from the SES padstack name.
    ses_drill = ses_orig.replace(
        '(net "SIG_A1"',
        '(net "VDD"\n        (via "Via[0-5]_500:300_um" 1080000 -1060000)\n      )\n      (net "SIG_A1"',
        1,
    )
    assert ses_drill != ses_orig, "expected quoted SIG_A1 net in route.ses"
    drill_ses_path = tmp_path / "drill.ses"
    drill_ses_path.write_text(ses_drill, encoding="utf-8")
    drill_out = tmp_path / "drill_out.kicad_pcb"
    drill_json = tmp_path / "drill_finish.json"
    run_helper("finish", s6a_pcb, drill_ses_path, drill_out, drill_json)
    fin_drill = json.loads(drill_json.read_text(encoding="utf-8"))
    assert fin_drill["imported"] is True
    assert fin_drill["via_drills_restored"]["count"] >= 1
    drill_board = read_board(drill_out)
    vdd_thru = [
        v for v in drill_board.routing["vias"]
        if v["net"] == "VDD" and v.get("via_type", "through") == "through"
    ]
    assert vdd_thru, "expected injected VDD through-via in imported board"
    assert vdd_thru[0]["drill"] == pytest.approx(0.30, abs=1e-4)


def test_auto_hdi_signals_escapes_inner_ring_on_fine_pitch_bga():
    """On a fine-pitch HDI BGA (pitch <= 0.65 mm, allow_microvias=True), inner-ring signal
    balls physically cannot escape on F.Cu and must auto-receive microvia-in-pad even when
    include_signals=False (default in S6a)."""
    from eda_agent.placer.ir import Board, CopperLayer, Courtyard, Footprint, Net, NetClass, Outline, Pad, derive
    from eda_agent.placer.route.bga_escape import plan_bga_escapes

    pads = []
    nets = []
    # 5x5 0.40 mm pitch WLCSP with signal nets on inner ring (ring >= 1) and outer ring (ring == 0)
    for r in range(5):
        for c in range(5):
            name = f"SIG_{r}_{c}"
            pads.append(Pad(
                f"{chr(65 + r)}{c + 1}", name,
                ((c - 2) * 0.4, (r - 2) * 0.4), (0.22, 0.22),
                shape="Circle", kind="smd", layers=("F.Cu",),
            ))
            nets.append(Net(name, "Default", 2))
    u1 = Footprint(
        "U1", "WLCSP25", "Lib:WLCSP25", 10.0, 10.0, 0.0, "F", locked=True, pads=pads,
        courtyard=Courtyard("file", [[(-1.2, -1.2), (1.2, -1.2), (1.2, 1.2), (-1.2, 1.2)]]),
        uuid="u-U1",
    )
    board = derive(Board(
        name="hdi_auto_sig", footprints=[u1],
        outline=[Outline([(0, 0), (20, 0), (20, 20), (0, 20)])],
        board_area_mm2=400.0,
        copper_layers=[
            CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("In1.Cu", "In1.Cu", 1),
            CopperLayer("In2.Cu", "In2.Cu", 2), CopperLayer("B.Cu", "B.Cu", 3),
        ],
        netclasses=[NetClass("Default", 0.09, 0.09, 0.20, 0.10, microvia_diameter=0.20, microvia_drill=0.10)],
        nets=nets,
        rules={
            "min_clearance": 0.09, "min_track_width": 0.09, "copper_edge_clearance": 0.15,
            "min_via_annular_width": 0.05, "hole_to_hole": 0.15,
            "min_microvia_diameter": 0.20, "min_microvia_drill": 0.10, "allow_microvias": True,
        },
    ))
    res = plan_bga_escapes(board, {"parts": {}}, fixed=[], include_signals=False, microvia="auto")
    # 3x3 = 9 inner balls must be escaped with microvia_in_pad_l1_l2; 16 outer signal balls stay on F.Cu
    assert len(res["rows"]) == 9
    assert all(row["routed"] and row["kind"] == "microvia_in_pad_l1_l2" for row in res["rows"])


def test_inspect_summary_stackup_guidance_for_hdi_bga_and_plane():
    """placer__inspect must emit non-hardcoded stackup.guidance warning when zones==0 and
    recommend 4l_hdi_bga_escape when a fine-pitch BGA (<= 0.65 mm) is present on a 4L board."""
    from eda_agent.placer.ir import Board, CopperLayer, Courtyard, Footprint, Net, NetClass, Outline, Pad, derive
    from eda_agent.placer.mcp import _inspect_summary

    pads = [
        Pad(f"{chr(65 + r)}{c + 1}", f"N_{r}_{c}", ((c - 2) * 0.4, (r - 2) * 0.4), (0.22, 0.22),
            shape="Circle", kind="smd", layers=("F.Cu",))
        for r in range(5) for c in range(5)
    ]
    u1 = Footprint(
        "U1", "WLCSP25", "Lib:WLCSP25", 10.0, 10.0, 0.0, "F", locked=True, pads=pads,
        courtyard=Courtyard("file", [[(-1.2, -1.2), (1.2, -1.2), (1.2, 1.2), (-1.2, 1.2)]]),
        uuid="u-U1",
    )
    board = derive(Board(
        name="hdi_4l_no_plane", footprints=[u1],
        outline=[Outline([(0, 0), (20, 0), (20, 20), (0, 20)])],
        board_area_mm2=400.0,
        copper_layers=[
            CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("In1.Cu", "In1.Cu", 1),
            CopperLayer("In2.Cu", "In2.Cu", 2), CopperLayer("B.Cu", "B.Cu", 3),
        ],
        netclasses=[NetClass("Default", 0.09, 0.09, 0.20, 0.10)],
        nets=[Net("GND", "Default", 2)],
    ))
    summary = _inspect_summary(board)
    guidance = summary["stackup"]["guidance"]
    assert summary["stackup"]["policy"] == "needs_decision"
    assert guidance["gnd_routing_mode"] == "point_to_point_traces_warning"
    assert guidance["fine_pitch_bgas"][0]["ref"] == "U1"
    assert guidance["options"][0]["id"] == "4l_hdi_bga_escape"


