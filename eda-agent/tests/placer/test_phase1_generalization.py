# SPDX-License-Identifier: MIT
"""Phase 1 generalization unit tests: IR canonicalization, multi-sheet hierarchy, intent & S1 guards."""

from __future__ import annotations

from pathlib import Path

from eda_agent.placer.eval import legality
from eda_agent.placer.intent import infer, validate
from eda_agent.placer.io import kicad_sch
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
    infer_layer_roles,
)
from eda_agent.placer.place import problem


def _rect_cy(w: float, h: float) -> Courtyard:
    return Courtyard("file", [[(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]])


def _pad(num: str, net: str | None, x: float = 0.0, y: float = 0.0, *, kind: str = "smd",
         layers: tuple[str, ...] = ("F.Cu",), fn: str | None = None, ptype: str | None = None,
         drill: tuple[float, float] | None = None) -> Pad:
    return Pad(number=num, net=net, local=(x, y), size=(0.5, 0.5), kind=kind,
               layers=layers, function=fn, pin_type=ptype, drill=drill)


def _base_board(fps: list[Footprint], *, netclasses: list[NetClass] | None = None,
                nets: list[Net] | None = None, zones: list[dict] | None = None,
                layers: list[CopperLayer] | None = None,
                outline: list[Outline] | None = None) -> Board:
    if layers is None:
        layers = [
            CopperLayer("F.Cu", "F.Cu", 0),
            CopperLayer("In1.Cu", "In1.Cu", 1),
            CopperLayer("In2.Cu", "In2.Cu", 2),
            CopperLayer("B.Cu", "B.Cu", 3),
        ]
    if outline is None:
        outline = [Outline([(0, 0), (50, 0), (50, 40), (0, 40)])]
    if nets is None:
        seen: dict[str, int] = {}
        for fp in fps:
            for p in fp.pads:
                if p.net:
                    seen[p.net] = seen.get(p.net, 0) + 1
        nets = [Net(name=n, netclass="Default", pads=c) for n, c in sorted(seen.items())]
    b = Board(
        name="phase1_test",
        footprints=fps,
        outline=outline,
        board_area_mm2=2000.0,
        copper_layers=layers,
        netclasses=netclasses or [NetClass("Default", 0.15, 0.15, 0.5, 0.25)],
        nets=nets,
        zones=zones or [],
        source={"sha256": "0" * 64},
    )
    return derive(b)


def test_active_tcxo_named_y1_does_not_hijack_rail_decaps():
    u1 = Footprint("U1", "ECP5", "Package_BGA:BGA256", 20, 20, 0, courtyard=_rect_cy(10, 10), pads=[
        _pad("1", "3V3FPGA", -2, 0, fn="VCC", ptype="power_in"),
        _pad("2", "GND", 0, 0, fn="GND", ptype="power_in"),
        _pad("3", "CLK_IN", 2, 0, fn="CLK0", ptype="input"),
    ])
    y1 = Footprint("Y1", "TCXO", "Oscillator:Oscillator_SMD_Kyocera_KC2520Z-4Pin", 10, 20, 0,
                   courtyard=_rect_cy(2.5, 2.0), pads=[
        _pad("1", "Y1_OE", -1, -1, fn="OE"),
        _pad("2", "GND", -1, 1, fn="GND"),
        _pad("3", "CLK_IN", 1, 1, fn="OUT"),
        _pad("4", "3V3FPGA", 1, -1, fn="VDD"),
    ])
    c1 = Footprint("C1", "100nF", "Capacitor_SMD:C_0402", 22, 20, 0, courtyard=_rect_cy(1.0, 0.6), pads=[
        _pad("1", "3V3FPGA", -0.4, 0), _pad("2", "GND", 0.4, 0),
    ])
    c2 = Footprint("C2", "10uF", "Capacitor_SMD:C_0603", 24, 20, 0, courtyard=_rect_cy(1.6, 0.8), pads=[
        _pad("1", "3V3FPGA", -0.6, 0), _pad("2", "GND", 0.6, 0),
    ])
    board = _base_board([u1, y1, c1, c2])
    intent = infer.draft(board)
    assert validate.validate(intent, board)["valid"]
    assert intent["parts"]["Y1"]["role"]["value"] == "oscillator"
    assert intent["parts"]["C1"]["role"]["value"] == "decap"
    assert intent["parts"]["C1"]["target"]["value"]["ref"] == "U1"
    assert intent["parts"]["C2"]["role"]["value"] != "crystal_load_cap"
    assert intent["nets"]["3V3FPGA"]["class"]["value"] != "SENSITIVE_ANALOG"


def test_board_netclasses_and_indexed_diff_pairs():
    u1 = Footprint("U1", "SOC", "Package_QFP:LQFP-64", 15, 20, 0, courtyard=_rect_cy(10, 10), pads=[
        _pad("1", "RF_OUT", 5, -2), _pad("2", "PCIE_TXP0", 5, 0), _pad("3", "PCIE_TXN0", 5, 1),
        _pad("4", "RMII_TXD0", 5, 3), _pad("5", "+12V", -5, 0, fn="VIN", ptype="power_in"),
        _pad("6", "GND", -5, 2, fn="GND", ptype="power_in"),
    ])
    j1 = Footprint("J1", "CONN", "Connector:Conn_01x06", 35, 20, 0, courtyard=_rect_cy(4, 10), pads=[
        _pad("1", "RF_OUT", 0, -2), _pad("2", "PCIE_TXP0", 0, 0), _pad("3", "PCIE_TXN0", 0, 1),
        _pad("4", "RMII_TXD0", 0, 3), _pad("5", "+12V", 0, 4), _pad("6", "GND", 0, 5),
    ])
    ncs = [
        NetClass("Default", 0.15, 0.15, 0.5, 0.25),
        NetClass("RF50", 0.15, 0.36, 0.5, 0.25),
        NetClass("PCIe", 0.15, 0.15, 0.5, 0.25, diff_pair_width=0.15, diff_pair_gap=0.15),
        NetClass("RMII", 0.15, 0.15, 0.5, 0.25),
        NetClass("Power_12V", 0.25, 0.80, 0.8, 0.40),
    ]
    nets = [
        Net("RF_OUT", "RF50", 2),
        Net("PCIE_TXP0", "PCIe", 2),
        Net("PCIE_TXN0", "PCIe", 2),
        Net("RMII_TXD0", "RMII", 2),
        Net("+12V", "Power_12V", 2),
        Net("GND", "Default", 2),
    ]
    board = _base_board([u1, j1], netclasses=ncs, nets=nets)
    assert [(p.positive, p.negative) for p in find_diff_pairs(["PCIE_TXP0", "PCIE_TXN0", "CANH", "CANL"])] == [
        ("CANH", "CANL"), ("PCIE_TXP0", "PCIE_TXN0"),
    ]
    intent = infer.draft(board)
    assert validate.validate(intent, board)["valid"]
    assert intent["nets"]["RF_OUT"]["class"]["value"] == "RF"
    assert intent["nets"]["PCIE_TXP0"]["class"]["value"] == "DIFF_PAIR"
    assert intent["nets"]["RMII_TXD0"]["class"]["value"] == "HIGH_SPEED"
    assert intent["nets"]["+12V"]["class"]["value"] == "POWER_HIGH_CURRENT"


def test_discrete_fet_mppt_buck_detected():
    u1 = Footprint("U1", "LM5107", "Package_SO:SOIC-8", 15, 20, 0, courtyard=_rect_cy(5, 5), pads=[
        _pad("1", "+10V", -2, -1), _pad("2", "HS_GATE", 2, -1), _pad("3", "LS_GATE", 2, 0),
        _pad("4", "SW_NODE", 2, 1), _pad("5", "GND", -2, 1),
    ])
    q1 = Footprint("Q1", "CSD18540", "Package_TO_SOT_SMD:TDSON-8", 25, 16, 0, courtyard=_rect_cy(5, 6), pads=[
        _pad("1", "HS_GATE", -2, 0), _pad("2", "VIN_SOLAR", 0, -2), _pad("3", "SW_NODE", 0, 2),
    ])
    q4 = Footprint("Q4", "CSD18540", "Package_TO_SOT_SMD:TDSON-8", 25, 24, 0, courtyard=_rect_cy(5, 6), pads=[
        _pad("1", "LS_GATE", -2, 0), _pad("2", "SW_NODE", 0, -2), _pad("3", "GND", 0, 2),
    ])
    l1 = Footprint("L1", "22uH", "Inductor_THT:L_Toroid", 35, 20, 0, courtyard=_rect_cy(8, 8), pads=[
        _pad("1", "SW_NODE", -3, 0), _pad("2", "+12V_BAT", 3, 0),
    ])
    board = _base_board([u1, q1, q4, l1])
    intent = infer.draft(board)
    assert validate.validate(intent, board)["valid"]
    assert intent["parts"]["U1"]["role"]["value"] == "converter_ic"
    assert intent["nets"]["SW_NODE"]["class"]["value"] == "SW_NODE"


def test_hierarchical_reusable_sheets_and_multi_unit_ic(tmp_path: Path):
    root_sch = tmp_path / "demo.kicad_sch"
    sub_sch = tmp_path / "phy.kicad_sch"
    root_sch.write_text(
        '(kicad_sch (uuid "root-uuid")\n'
        '  (sheet (at 10 10) (size 20 20) (uuid "sh-a")\n'
        '    (property "Sheetname" "PHY_A") (property "Sheetfile" "phy.kicad_sch"))\n'
        '  (sheet (at 40 10) (size 20 20) (uuid "sh-b")\n'
        '    (property "Sheetname" "PHY_B") (property "Sheetfile" "phy.kicad_sch"))\n'
        ')\n',
        encoding="utf-8",
    )
    sub_sch.write_text(
        '(kicad_sch (uuid "sub-uuid")\n'
        '  (lib_symbols (symbol "Device:C" (pin passive line (at 0 0 0) (name "~") (number "1"))))\n'
        '  (symbol (lib_id "Device:C") (at 50 50 0)\n'
        '    (instances (project "demo"\n'
        '      (path "/root-uuid/sh-a" (reference "C1"))\n'
        '      (path "/root-uuid/sh-b" (reference "C2")))))\n'
        '  (symbol (lib_id "Device:C") (at 60 50 0)\n'
        '    (instances (project "demo"\n'
        '      (path "/root-uuid/sh-a" (reference "U1"))\n'
        '      (path "/root-uuid/sh-b" (reference "U1")))))\n'
        ')\n',
        encoding="utf-8",
    )
    u1 = Footprint("U1", "FPGA", "Package_BGA:BGA256", 20, 20, 0, courtyard=_rect_cy(8, 8), pads=[
        _pad("1", "+3V3", -1, 0, fn="VCC", ptype="power_in"), _pad("2", "GND", 1, 0, fn="GND", ptype="power_in"),
    ])
    c1 = Footprint("C1", "100nF", "Capacitor_SMD:C_0402", 10, 10, 0, courtyard=_rect_cy(1, 0.6), pads=[
        _pad("1", "+3V3", -0.4, 0), _pad("2", "GND", 0.4, 0),
    ])
    c2 = Footprint("C2", "100nF", "Capacitor_SMD:C_0402", 30, 10, 0, courtyard=_rect_cy(1, 0.6), pads=[
        _pad("1", "+3V3", -0.4, 0), _pad("2", "GND", 0.4, 0),
    ])
    board = _base_board([u1, c1, c2])
    kicad_sch.enrich(board, tmp_path / "demo.kicad_pcb")
    assert c1.sheet == "PHY_A"
    assert c2.sheet == "PHY_B"
    assert set(u1.unit_sheets.keys()) == {"PHY_A", "PHY_B"}


def test_unfilled_plane_zones_grounded_holes_multipolygon_and_rf_balun_cavity():
    zones = [{"uuid": "z1", "net": "GND", "layers": ["In1.Cu"], "outline_area_mm2": 1800.0, "filled": False}]
    mh1 = Footprint("H1", "MountingHole_Pad", "MountingHole:MountingHole_3.2mm_Pad", 5, 5, 0,
                    courtyard=_rect_cy(4, 4), pads=[
        _pad("1", "GND", 0, 0, kind="thru_hole", layers=("F.Cu", "B.Cu"), drill=(3.2, 3.2)),
    ])
    j_edge = Footprint("J1", "USB", "Connector_USB:USB_C", 0.5, 20, 0, courtyard=_rect_cy(4, 8), pads=[
        _pad("1", "VBUS", 0, 0),
    ])
    t3 = Footprint("T3", "TXIF_BALUN", "mainboard:Balun_TDK_TFSZ0605", 20, 20, 0, courtyard=_rect_cy(0.8, 0.6), pads=[
        _pad("1", "GND", 0, 0),
    ])
    j10 = Footprint("J10", "SWH", "mainboard:Murata_SWH_MM8930-26xx", 22, 20, 0, courtyard=_rect_cy(2.0, 2.0), pads=[
        _pad("1", "GND", 0, 0),
    ])
    j_tall = Footprint("J2", "Header_2x10", "Connector_PinHeader:PinHeader_2x10", 30, 20, 0,
                       courtyard=_rect_cy(5, 25), pads=[_pad("1", "GND", 0, 0)])
    outline = [
        Outline([(0, 0), (24, 0), (24, 40), (0, 40)]),
        Outline([(26, 0), (50, 0), (50, 40), (26, 40)]),
    ]
    board = _base_board([mh1, j_edge, t3, j10, j_tall], zones=zones, outline=outline)
    roles = {r.layer: r for r in infer_layer_roles(board)}
    assert roles["In1.Cu"].role == "plane" and roles["In1.Cu"].net == "GND"
    fixed = problem.fixed_parts(board)
    assert "H1" in fixed
    assert "J1" in fixed
    assert legality.is_shield_cavity_eligible(t3) is True
    assert legality.is_shield_cavity_eligible(j10) is True
    assert legality.is_shield_cavity_eligible(j_tall) is False
