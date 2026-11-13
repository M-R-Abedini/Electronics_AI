# SPDX-License-Identifier: MIT
"""Altium PCB pad nets joined to saved schematic evidence."""

from __future__ import annotations

import pytest
from eda_agent.placer.io import altium


@pytest.mark.parametrize("source_id, matched", [("child\\A1", True), ("child\\other", False), ("", False)])
def test_native_pins_match_source_id_and_keep_pcb_net(tmp_path, monkeypatch, source_id, matched):
    pcb, project = tmp_path / "board.PcbDoc", tmp_path / "board.PrjPcb"
    pcb.write_bytes(b"pcb snapshot")
    symbols = {"U1": [
        (tmp_path / "power.SchDoc", {"unique_id": "A1", "value": "Regulator"},
         [{"Designator": "1", "Name": "VIN", "Electrical": "7"}]),
        (tmp_path / "other.SchDoc", {"unique_id": "B1"},
         [{"Designator": "1", "Name": "DATA", "Electrical": "2"}]),
    ]}
    monkeypatch.setattr(altium, "_schematic_metadata", lambda _: (symbols, [], []))
    snapshot = {
        "components": [{"designator": "U1", "id": "component", "footprint": "QFN", "comment": "PCB value",
                        "parameters": {"SOURCEUNIQUEID": source_id, "X": "10mm", "Y": "5mm",
                                       "ROTATION": "0", "LAYER": "TOP"}}],
        "pads": [{"component": "U1", "pad_name": "1", "net_name": "+12V", "x_mm": 11, "y_mm": 5,
                  "width_mm": 0.3, "height_mm": 0.5, "rotation_deg": 0, "shape": "rectangle",
                  "layer": "Top Layer"}],
        "layers": [{"name": "Top Layer", "layer_id": 1}, {"name": "Bottom Layer", "layer_id": 32}],
        "nets": ["+12V"], "coverage": {}, "missing": [],
    }
    board = altium._read_intent_board(pcb, project, snapshot)
    part = board.fp("U1")
    pad = part.pads[0]
    assert (pad.net, pad.local) == ("+12V", (1, 0))
    assert (pad.function, pad.pin_type) == (("VIN", "power") if matched else (None, None))
    assert part.sheet == ("power.SchDoc" if matched else None)
    assert not board.outline_valid
    assert any("source ID differs" in item for item in board.unknowns) is not matched
