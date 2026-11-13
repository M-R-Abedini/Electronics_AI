# SPDX-License-Identifier: MIT
"""Altium PCB pad nets joined to saved schematic evidence."""

from __future__ import annotations

import pytest
from eda_agent.fileio.altium_project import read_project_sheets
from eda_agent.fileio.altium_sch import _parse_fields, read_schematic_pins
from eda_agent.placer.intent import infer, validate
from eda_agent.placer.io import altium
from eda_agent.placer.ir import Board, Footprint


@pytest.mark.parametrize("source_id, matched", [("child\\A1", True), ("child\\other", False), ("", False)])
def test_native_pins_match_source_id_and_keep_pcb_net(tmp_path, monkeypatch, source_id, matched):
    pcb, project = tmp_path / "board.PcbDoc", tmp_path / "board.PrjPcb"
    pcb.write_bytes(b"pcb snapshot")
    symbols = {"U1": [
        (tmp_path / "power.SchDoc", {"unique_id": "A1", "value": "Regulator", "x": 0, "y": 125},
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
    assert part.schematic_position == ((0, 125) if matched else None)
    assert not board.outline_valid
    assert any("source ID differs" in item for item in board.unknowns) is not matched


def test_legacy_schematic_pin_fields_and_explicit_project_documents(tmp_path):
    project = tmp_path / "design.PrjPcb"
    sheet = tmp_path / "Power.SchDoc"
    sheet.touch()
    (tmp_path / "PrintOnly.SchDoc").touch()
    project.write_text("[Document1]\nDocumentPath=Power.SchDoc\n[Print]\nDocumentPath=PrintOnly.SchDoc\n")
    assert read_project_sheets(project) == [sheet]
    row = _parse_fields(b"|RECORD=2|OWNERINDEX=1|DESIGNATOR=2|LOCATION.X=20|LOCATION.Y=30|PINLENGTH=10")
    assert read_schematic_pins(sheet, records=[row])[0]["designator"] == "2"


def test_native_intent_validation_rejects_missing_pad_connectivity():
    board = Board("legacy", [Footprint("U1", "", "", 0, 0, 0)], source={"format": "altium_saved_intent"})
    result = validate.validate(infer.draft(board), board)
    assert not result["valid"]
    assert result["errors"] == ["native Altium snapshot has no decoded component-pad net connections"]
