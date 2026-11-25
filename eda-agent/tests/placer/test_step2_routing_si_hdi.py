# SPDX-License-Identifier: MIT
"""Step 2 unit tests: bug-free DSN fast-net GND-layer guard and adjacent-plane HDI microvia tap priority."""

from __future__ import annotations

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
from eda_agent.placer.route import dsn, fanout


def _make_8l_hdi_board() -> Board:
    layers = [
        CopperLayer("F.Cu", "F.Cu", 0, "signal"),
        CopperLayer("In1.Cu", "In1.Cu", 1, "power"),
        CopperLayer("In2.Cu", "In2.Cu", 2, "signal"),
        CopperLayer("In3.Cu", "In3.Cu", 3, "signal"),
        CopperLayer("In4.Cu", "In4.Cu", 4, "power"),
        CopperLayer("In5.Cu", "In5.Cu", 5, "signal"),
        CopperLayer("In6.Cu", "In6.Cu", 6, "power"),
        CopperLayer("B.Cu", "B.Cu", 7, "signal"),
    ]
    u1 = Footprint(
        "U1", "MCU", "QFN", 10.0, 10.0, 0.0, "F", locked=True,
        pads=[
            Pad("1", "USB_DP", (-1.5, -0.5), (0.35, 0.35), layers=("F.Cu",)),
            Pad("2", "USB_DN", (-1.5, 0.5), (0.35, 0.35), layers=("F.Cu",)),
            Pad("3", "GND", (1.5, 0.0), (0.4, 0.4), layers=("F.Cu",)),
        ],
        courtyard=Courtyard("file", [[(-2.5, -2.5), (2.5, -2.5), (2.5, 2.5), (-2.5, 2.5)]]), uuid="u-U1",
    )
    tp_bot = Footprint(
        "TP4", "GND", "TestPoint", 20.0, 10.0, 0.0, "B", locked=True,
        pads=[Pad("1", "GND", (0.0, 0.0), (0.8, 0.8), layers=("B.Cu",))],
        courtyard=Courtyard("file", [[(-0.8, -0.8), (0.8, -0.8), (0.8, 0.8), (-0.8, 0.8)]]), uuid="u-TP4",
    )
    zones = [
        {"uuid": "z-gnd1", "net": "GND", "layers": ["In1.Cu"], "filled": True,
         "filled_area_mm2": {"In1.Cu": 550.0},
         "outline": [[(0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (0.0, 20.0)]]},
        {"uuid": "z-1v8", "net": "1V8", "layers": ["In4.Cu"], "filled": True,
         "filled_area_mm2": {"In4.Cu": 550.0},
         "outline": [[(0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (0.0, 20.0)]]},
        {"uuid": "z-gnd6", "net": "GND", "layers": ["In6.Cu"], "filled": True,
         "filled_area_mm2": {"In6.Cu": 550.0},
         "outline": [[(0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (0.0, 20.0)]]},
    ]
    b = Board(
        name="hdi8l",
        footprints=[u1, tp_bot],
        outline=[Outline([(0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (0.0, 20.0)])],
        board_area_mm2=600.0,
        copper_layers=layers,
        netclasses=[NetClass("Default", 0.12, 0.12, 0.40, 0.20, microvia_diameter=0.20, microvia_drill=0.10)],
        nets=[Net("GND", "Default", 2), Net("1V8", "Default", 0), Net("USB_DP", "Default", 1), Net("USB_DN", "Default", 1)],
        zones=zones,
        zone_fill_by_layer_mm2={"In1.Cu": {"GND": 550.0}, "In4.Cu": {"1V8": 550.0}, "In6.Cu": {"GND": 550.0}},
        stackup=Stackup("complete", [StackupLayer(l.name, "copper") for l in layers], []),
        rules={"allow_microvias": True, "min_clearance": 0.10, "min_track_width": 0.10,
               "min_via_diameter": 0.40, "min_through_drill": 0.20,
               "min_microvia_diameter": 0.20, "min_microvia_drill": 0.10},
    )
    derive(b)
    return b


def test_restrict_fast_net_layers_excludes_power_only_layer_and_never_locks_off_pad_layer():
    b = _make_8l_hdi_board()
    sample_dsn = (
        "(pcb board\n  (resolution um 10)\n  (unit um)\n"
        "  (structure (layer F.Cu (type signal)))\n"
        "  (network\n"
        "    (net USB_DP (pins U1-1))\n"
        "    (net USB_DN (pins U1-2))\n"
        "  )\n  (wiring)\n)\n"
    )
    intent = {"diff_pairs": [{"value": {"positive": "USB_DP", "negative": "USB_DN"}}], "nets": {}}
    out_dsn, applied = dsn.restrict_fast_net_layers(sample_dsn, b, intent)
    assert sorted(applied) == ["USB_DN", "USB_DP"]
    # In3.Cu faces 1V8 only, so use_layer must contain F.Cu, In2.Cu, In5.Cu, B.Cu (NOT In3.Cu)
    assert "(circuit (use_layer F.Cu In2.Cu In5.Cu B.Cu))" in out_dsn

    # Regression guard: on a 6L board where B.Cu is NOT adjacent to GND, a net with a B.Cu SMD pad
    # must NOT be restricted to [F.Cu, In2.Cu] (which would make its B.Cu pad unroutable!).
    layers6 = [
        CopperLayer("F.Cu", "F.Cu", 0, "signal"),
        CopperLayer("In1.Cu", "In1.Cu", 1, "power"),
        CopperLayer("In2.Cu", "In2.Cu", 2, "signal"),
        CopperLayer("In3.Cu", "In3.Cu", 3, "power"),
        CopperLayer("In4.Cu", "In4.Cu", 4, "signal"),
        CopperLayer("B.Cu", "B.Cu", 5, "signal"),
    ]
    bot_fp = Footprint(
        "U2", "IC", "QFN", 10.0, 10.0, 0.0, "B", locked=True,
        pads=[Pad("1", "USB_DP", (0.0, 0.0), (0.35, 0.35), layers=("B.Cu",))],
        courtyard=Courtyard("file", [[(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)]]), uuid="u-U2",
    )
    b6 = Board(
        name="hdi6l",
        footprints=[bot_fp],
        outline=[Outline([(0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (0.0, 20.0)])],
        board_area_mm2=600.0,
        copper_layers=layers6,
        netclasses=[NetClass("Default", 0.12, 0.12, 0.40, 0.20)],
        nets=[Net("GND", "Default", 0), Net("USB_DP", "Default", 1)],
        zones=[{"uuid": "z-g", "net": "GND", "layers": ["In1.Cu"], "filled": True,
                "filled_area_mm2": {"In1.Cu": 550.0},
                "outline": [[(0.0, 0.0), (30.0, 0.0), (30.0, 20.0), (0.0, 20.0)]]}],
        zone_fill_by_layer_mm2={"In1.Cu": {"GND": 550.0}},
        stackup=Stackup("complete", [StackupLayer(l.name, "copper") for l in layers6], []),
    )
    derive(b6)
    _, applied6 = dsn.restrict_fast_net_layers(sample_dsn, b6, intent)
    assert "USB_DP" not in applied6


def test_plan_power_tap_prefers_adjacent_blind_microvia_on_hdi_board():
    b = _make_8l_hdi_board()
    u1 = b.fp("U1")
    gnd_pad_f = next(p for p in u1.pads if p.net == "GND")
    got_f = fanout.plan_power_tap(b, u1, gnd_pad_f, "In1.Cu", fixed=[], allow_microvia=True)
    assert got_f is not None
    _, via_f = got_f
    assert isinstance(via_f, dict) and via_f.get("via_type") == "micro"
    assert tuple(via_f["layers"]) == ("F.Cu", "In1.Cu")

    tp4 = b.fp("TP4")
    got_b = fanout.plan_power_tap(b, tp4, tp4.pads[0], "In6.Cu", fixed=[], allow_microvia=True)
    assert got_b is not None
    _, via_b = got_b
    assert isinstance(via_b, dict) and via_b.get("via_type") == "micro"
    assert tuple(via_b["layers"]) == ("B.Cu", "In6.Cu")
