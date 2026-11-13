# SPDX-License-Identifier: MIT
"""Pin names from KiCad schematics (plan F3b)."""

from __future__ import annotations

from eda_agent.placer.io import kicad_sch
from eda_agent.placer.ir import Board, Footprint, Pad

SHEET = """(kicad_sch (version 20250114) (generator "test")
  (lib_symbols
    (symbol "Reg:BUCK" (property "Reference" "U" (at 0 0 0))
      (symbol "BUCK_1_1"
        (pin power_in line (at 0 0 0) (length 2.54) (name "VIN" (effects (font (size 1 1)))) (number "1"))
        (pin passive line (at 0 0 0) (length 2.54) (name "SW" (effects (font (size 1 1)))) (number "2")))
      (symbol "BUCK_2_1"
        (pin input line (at 0 0 0) (length 2.54) (name "FB \\"x\\"" (effects (font (size 1 1)))) (number "3"))))
    (symbol "Device:C" (property "Reference" "C" (at 0 0 0))
      (symbol "C_0_1"
        (pin passive line (at 0 0 0) (length 2.54) (name "~") (number "1"))
        (pin passive line (at 0 0 0) (length 2.54) (name "2") (number "2")))))
  (symbol (lib_id "Reg:BUCK") (at 10 10 0) (unit 1)
    (property "Reference" "U?" (at 0 0 0))
    (instances (project "p" (path "/a" (reference "U7") (unit 1)))))
  (symbol (lib_id "Reg:BUCK") (at 10 10 0) (unit 2)
    (property "Reference" "U7" (at 0 0 0)))
  (symbol (lib_id "Device:C") (at 20 20 0) (unit 1)
    (property "Reference" "C1" (at 0 0 0)))
  (symbol (lib_id "power:GND") (at 0 0 0) (property "Reference" "#PWR01" (at 0 0 0)))
)
"""


def _board() -> Board:
    chip = Footprint("U7", "", "", 0, 0, 0, side="F",
                     pads=[Pad("1", "VIN", (0, 0), (1, 1)), Pad("2", "SW", (1, 0), (1, 1), function="LX"),
                           Pad("3", "FB", (2, 0), (1, 1)), Pad("4", None, (3, 0), (1, 1))])
    cap = Footprint("C1", "", "", 5, 0, 0, side="F", pads=[Pad("1", "VIN", (0, 0), (1, 1)), Pad("2", "GND", (1, 0), (1, 1))])
    lone = Footprint("R9", "", "", 9, 0, 0, side="F", pads=[Pad("1", "FB", (0, 0), (1, 1))])
    return Board("b", [chip, cap, lone])


def test_read_pins_joins_the_units_of_a_part_and_skips_power_symbols(tmp_path):
    sheet = tmp_path / "main.kicad_sch"
    sheet.write_text(SHEET, encoding="utf-8")
    pins = kicad_sch.read_pins(sheet)
    assert set(pins) == {"U7", "C1"}  # "#PWR01" and the unannotated "U?" are not parts
    assert pins["U7"]["1"] == kicad_sch.SchPin("VIN", "power_in")
    assert pins["U7"]["3"] == kicad_sch.SchPin('FB "x"', "input")  # second unit, escaped quotes


def test_enrich_fills_only_what_the_board_lacks(tmp_path):
    (tmp_path / "main.kicad_sch").write_text(SHEET, encoding="utf-8")
    board = _board()
    info = kicad_sch.enrich(board, tmp_path / "b.kicad_pcb")
    u7, c1 = board.fp("U7"), board.fp("C1")
    assert [(p.function, p.pin_type) for p in u7.pads] == [
        ("VIN", "power_in"), ("LX", "passive"), ('FB "x"', "input"), (None, None)]  # the board's own "LX" is kept
    assert [(p.function, p.pin_type) for p in c1.pads] == [(None, "passive"), (None, "passive")]  # "~" and "2" say nothing
    assert info["pads_named"] == 2 and info["parts_matched"] == 2 and info["parts_without_symbol"] == ["R9"]
    assert info["sheets"] == ["main.kicad_sch"]


def test_enrich_without_schematics_changes_nothing(tmp_path):
    board = _board()
    info = kicad_sch.enrich(board, tmp_path / "b.kicad_pcb")
    assert info["pads_named"] == 0 and board.fp("U7").pads[0].function is None
