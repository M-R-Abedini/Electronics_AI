# SPDX-License-Identifier: MIT
"""Step 3 unit tests: real-world .kicad_dru compiler covering JLCPCB, Jetson, VME, and Cynthion patterns."""

from __future__ import annotations

from pathlib import Path

from eda_agent.placer.ir import (
    Board,
    CopperLayer,
    Net,
    NetClass,
    Outline,
    derive,
)
from eda_agent.placer.route import dru


def _sample_board() -> Board:
    layers = [
        CopperLayer("F.Cu", "F.Cu", 0, "signal"),
        CopperLayer("In1.Cu", "In1.Cu", 1, "power"),
        CopperLayer("In2.Cu", "In2.Cu", 2, "signal"),
        CopperLayer("B.Cu", "B.Cu", 3, "signal"),
    ]
    b = Board(
        name="dru_test",
        footprints=[],
        outline=[Outline([(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)])],
        copper_layers=layers,
        netclasses=[NetClass("Default", 0.15, 0.15, 0.45, 0.25), NetClass("zse_50r", 0.15, 0.15, 0.45, 0.25)],
        nets=[Net("SIG1", "Default", 2), Net("RF1", "zse_50r", 2)],
    )
    derive(b)
    return b


def _sample_dsn() -> str:
    return (
        "(pcb board\n  (resolution um 10)\n  (unit um)\n"
        "  (structure\n"
        "    (layer F.Cu (type signal))\n"
        "    (layer In1.Cu (type power))\n"
        "    (layer In2.Cu (type signal))\n"
        "    (layer B.Cu (type signal))\n"
        "  )\n"
        "  (network\n"
        "    (class kicad_default \"\" SIG1\n"
        "      (rule (width 150) (clearance 150))\n"
        "    )\n"
        "    (class zse_50r \"\" RF1\n"
        "      (rule (width 150) (clearance 150))\n"
        "    )\n"
        "  )\n"
        "  (wiring)\n)\n"
    )


def test_dru_compiler_handles_real_jlcpcb_comments_and_router_floors(tmp_path: Path):
    dru_file = tmp_path / "jlcpcb.kicad_dru"
    dru_file.write_text(
        '(version 1)\n'
        '#(rule "CommentedOut"\n'
        '#  (constraint clearance (min 9.9mm))\n'
        '#)\n'
        '(rule "Edge to track"\n'
        '  (condition "A.Type == \'track\'")\n'
        '  (constraint edge_clearance (min 0.3mm)))\n'
        '(rule "Via hole size"\n'
        '  (condition "A.Type == \'via\'")\n'
        '  (constraint hole_size (min 0.3mm)))\n'
        '(rule "Clearance: hole to hole, different nets"\n'
        '  (layer outer)\n'
        '  (condition "A.Net != B.Net")\n'
        '  (constraint hole_to_hole (min 0.5mm)))\n'
        '(rule "Clearance: track to PTH hole"\n'
        '  (condition "A.isPlated() && B.Type == \'track\' && A.Net != B.Net")\n'
        '  (constraint hole_clearance (min 0.33mm)))\n'
        '(rule "Clearance: track to pad"\n'
        '  (condition "A.Type == \'pad\' && B.Type == \'track\' && A.Net != B.Net")\n'
        '  (constraint clearance (min 0.2mm)))\n'
        '(rule "Silkscreen text"\n'
        '  (layer "?.Silkscreen")\n'
        '  (constraint text_height (min 1mm)))\n',
        encoding="utf-8",
    )
    out_dsn, meta = dru.compile_dru_to_dsn(_sample_dsn(), dru_file, _sample_board())
    assert "CommentedOut" not in meta["translated"]
    assert "CommentedOut" not in meta["untranslated"]
    assert "(clearance 200)" in out_dsn
    assert "(clearance 9900)" not in out_dsn
    assert meta["effective_rules"]["edge_clearance_mm"] == 0.3
    assert meta["effective_rules"]["min_via_drill_mm"] == 0.3
    assert meta["effective_rules"]["hole_to_hole_mm"] == 0.5
    assert meta["effective_rules"]["hole_clearance_mm"] == 0.33
    assert "Silkscreen text" in meta["untranslated"]


def test_dru_compiler_handles_netclass_outer_layer_and_p5_untranslated(tmp_path: Path):
    dru_file = tmp_path / "vme_like.kicad_dru"
    dru_file.write_text(
        '(version 1)\n'
        '(rule "zse_50R_outer"\n'
        '  (layer outer)\n'
        '  (condition "A.NetClass == \'zse_50r\'")\n'
        '  (constraint track_width (min 0.1mm) (opt 0.2mm)))\n'
        '(rule "outer_clearance"\n'
        '  (layer outer)\n'
        '  (constraint clearance (min 0.22mm)))\n'
        '(rule "length_DDR"\n'
        '  (condition "A.NetClass == \'DDR\' && A.fromTo(\'IC14-*\', \'IC13-*\')")\n'
        '  (constraint length (min 42.5mm)))\n'
        '(rule "clearance_under_fpga"\n'
        '  (condition "A.intersectsArea(\'underFPGA\')")\n'
        '  (constraint clearance (min 0.1mm)))\n',
        encoding="utf-8",
    )
    out_dsn, meta = dru.compile_dru_to_dsn(_sample_dsn(), dru_file, _sample_board())
    assert "outer_clearance" in meta["translated"]
    assert "(layer F.Cu (type signal) (rule (clearance 220)))" in out_dsn
    assert "(layer B.Cu (type signal) (rule (clearance 220)))" in out_dsn
    assert "zse_50R_outer" in meta["translated"]
    assert "length_DDR" in meta["untranslated"]
    assert "clearance_under_fpga" in meta["untranslated"]
