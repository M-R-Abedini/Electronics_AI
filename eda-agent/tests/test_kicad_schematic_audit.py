"""Tests for strict KiCad schematic geometric overlap & wire-crossing audit."""

from eda_agent.core.kicad_schematic import build_schematic
from eda_agent.core.kicad_schematic_audit import audit_schematic_geometry


def test_build_schematic_passes_strict_geometric_audit():
    parts = [
        {"refdes": "U1", "value": "STM32U5", "pin_count": 12},
        {"refdes": "R1", "value": "10k_0201", "pin_count": 2},
        {"refdes": "C1", "value": "100nF_10V", "pin_count": 2},
    ]
    nets = [
        {"name": "VDD_1V8", "nodes": [{"refdes": "U1", "pin": "1"}, {"refdes": "R1", "pin": "1"}, {"refdes": "C1", "pin": "1"}]},
        {"name": "GND", "nodes": [{"refdes": "U1", "pin": "2"}, {"refdes": "C1", "pin": "2"}]},
        {"name": "I2C_SCL", "nodes": [{"refdes": "U1", "pin": "3"}, {"refdes": "R1", "pin": "2"}]},
    ]
    sch = build_schematic(parts, nets, title="Audit Test", project="audit_test")
    report = audit_schematic_geometry(sch)
    assert report["ok"], report["violations"]
    assert report["violation_count"] == 0


def test_audit_catches_rotated_text_and_wire_through_symbol_and_label():
    # Minimal schematic with:
    # 1) A horizontal wire passing straight through a resistor body
    # 2) A vertical wire crossing through a global_label
    # 3) Rotated 90-degree Value text overlapping a horizontal wire
    sch = """(kicad_sch
  (version 20260306)
  (generator "eeschema")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Test:R"
      (symbol "R_0_1" (rectangle (start -1.0 -2.0) (end 1.0 2.0) (stroke (width 0.254) (type default)) (fill (type none))))
      (symbol "R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.81) (name "~") (number "1"))
        (pin passive line (at 0 -3.81 90) (length 1.81) (name "~") (number "2"))
      )
    )
  )
  (symbol (lib_id "Test:R") (at 100 100 0) (unit 1) (uuid "00000000-0000-0000-0000-000000000002")
    (property "Reference" "R1" (at 100 94 0) (effects (font (size 1.27 1.27))))
    (property "Value" "100K_0201_LONG" (at 105 100 90) (effects (font (size 1.27 1.27))))
  )
  (wire (pts (xy 90 100) (xy 115 100)) (stroke (width 0) (type default)) (uuid "00000000-0000-0000-0000-000000000003"))
  (global_label "MAIN_SCL" (shape bidirectional) (at 120 120 0) (effects (font (size 1.27 1.27)) (justify left)) (uuid "00000000-0000-0000-0000-000000000004"))
  (wire (pts (xy 125 110) (xy 125 130)) (stroke (width 0) (type default)) (uuid "00000000-0000-0000-0000-000000000005"))
)
"""
    report = audit_schematic_geometry(sch)
    assert not report["ok"]
    kinds = set(report["by_kind"])
    assert "wire_through_symbol" in kinds
    assert "wire_through_label" in kinds
    assert "wire_through_text" in kinds


def test_repair_schematic_text_overlaps_fixes_rotated_text_on_wire():
    from eda_agent.core.kicad_schematic_audit import repair_schematic_text_overlaps

    sch = """(kicad_sch
  (version 20260306)
  (generator "eeschema")
  (uuid "00000000-0000-0000-0000-000000000010")
  (paper "A4")
  (lib_symbols
    (symbol "Test:L"
      (symbol "L_0_1" (rectangle (start -1.0 -2.0) (end 1.0 2.0) (stroke (width 0.254) (type default)) (fill (type none))))
      (symbol "L_1_1"
        (pin passive line (at 0 3.81 270) (length 1.81) (name "~") (number "1"))
        (pin passive line (at 0 -3.81 90) (length 1.81) (name "~") (number "2"))
      )
    )
  )
  (symbol (lib_id "Test:L") (at 100 100 90) (unit 1) (uuid "00000000-0000-0000-0000-000000000011")
    (property "Reference" "L2" (at 100 98 90) (effects (font (size 0.85 0.85))))
    (property "Value" "2.2uH_750mA" (at 100 100 90) (effects (font (size 0.85 0.85))))
  )
  (wire (pts (xy 90 100) (xy 96.19 100)) (stroke (width 0) (type default)) (uuid "00000000-0000-0000-0000-000000000012"))
  (wire (pts (xy 103.81 100) (xy 115 100)) (stroke (width 0) (type default)) (uuid "00000000-0000-0000-0000-000000000013"))
  (wire (pts (xy 90 97) (xy 115 97)) (stroke (width 0) (type default)) (uuid "00000000-0000-0000-0000-000000000014"))
)
"""
    before = audit_schematic_geometry(sch)
    assert "wire_through_text" in before["by_kind"]
    repaired = repair_schematic_text_overlaps(sch)
    after = audit_schematic_geometry(repaired)
    assert "wire_through_text" not in after["by_kind"], after["violations"]


def test_build_schematic_groups_parallel_decoupling_caps_into_wired_banks():
    from eda_agent.libimport.kicad.sexpr import find_all, loads

    cap_sym = """(symbol "Test:C_Small"
  (pin_numbers (hide yes)) (pin_names (offset 0.254) (hide yes))
  (property "Reference" "C" (at 1.27 1.27 0) (effects (font (size 0.85 0.85))))
  (property "Value" "C_Small" (at 1.27 -1.27 0) (effects (font (size 0.85 0.85))))
  (property "Footprint" "" (at 0 0 0) (effects (font (size 0.85 0.85)) (hide yes)))
  (property "Datasheet" "~" (at 0 0 0) (effects (font (size 0.85 0.85)) (hide yes)))
  (symbol "C_Small_0_1"
    (polyline (pts (xy -1.524 -0.508) (xy 1.524 -0.508)) (stroke (width 0.381) (type default)) (fill (type none)))
    (polyline (pts (xy -1.524 0.508) (xy 1.524 0.508)) (stroke (width 0.381) (type default)) (fill (type none)))
  )
  (symbol "C_Small_1_1"
    (pin passive line (at 0 3.81 270) (length 2.54) (name "~" (effects (font (size 0.85 0.85)))) (number "1" (effects (font (size 0.85 0.85)))))
    (pin passive line (at 0 -3.81 90) (length 2.54) (name "~" (effects (font (size 0.85 0.85)))) (number "2" (effects (font (size 0.85 0.85)))))
  )
)"""
    parts = [
        {"refdes": "C51", "value": "4.7uF_10V"},
        {"refdes": "C54", "value": "1.0uF_10V"},
        {"refdes": "C58", "value": "4.7uF_10V"},
        {"refdes": "C49", "value": "4.7uF_10V"},
        {"refdes": "C53", "value": "100nF"},
    ]
    nets = [
        {"name": "VSYS", "nodes": [{"refdes": "C51", "pin": "1"}, {"refdes": "C54", "pin": "1"}, {"refdes": "C58", "pin": "1"}]},
        {"name": "VBAT_SENSE", "nodes": [{"refdes": "C49", "pin": "1"}, {"refdes": "C53", "pin": "1"}]},
        {"name": "GND", "nodes": [
            {"refdes": "C51", "pin": "2"}, {"refdes": "C54", "pin": "2"}, {"refdes": "C58", "pin": "2"},
            {"refdes": "C49", "pin": "2"}, {"refdes": "C53", "pin": "2"},
        ]},
    ]
    sch = build_schematic(
        parts, nets,
        symbols={"Test:C_Small": cap_sym},
        part_symbol={p["refdes"]: "Test:C_Small" for p in parts},
        project="TestProj",
    )
    report = audit_schematic_geometry(sch)
    assert report["ok"], report["violations"]
    tree = loads(sch)
    # No global_labels should be emitted for these parallel shunt capacitor banks
    assert len(find_all(tree, "global_label")) == 0
    # Shared bus rails must have junctions
    assert len(find_all(tree, "junction")) >= 4


def test_hierarchy_audit_detects_and_repairs_missing_sheet_pages(tmp_path):
    """KiCad 10 shows an auto-repair popup when hierarchical (sheet ...) blocks
    lack (instances (project ... (path "/<root_uuid>" (page "N"))))."""
    from eda_agent.core.kicad_schematic_audit import (
        audit_schematic_hierarchy,
        repair_hierarchical_sheet_instances,
    )

    root_uuid = "11111111-1111-4111-8111-111111111111"
    s1_uuid = "22222222-2222-4222-8222-222222222222"
    s2_uuid = "33333333-3333-4333-8333-333333333333"
    sub1_uuid = "44444444-4444-4444-8444-444444444444"
    sub2_uuid = "55555555-5555-4555-8555-555555555555"

    root_sch = tmp_path / "Demo.kicad_sch"
    root_sch.write_text(f"""(kicad_sch
\t(version 20260306)
\t(generator "eeschema")
\t(uuid "{root_uuid}")
\t(paper "A4")
\t(lib_symbols)
\t(sheet
\t\t(at 40 40)
\t\t(size 40 20)
\t\t(uuid "{s1_uuid}")
\t\t(property "Sheetname" "02_Power" (at 40 39 0) (effects (font (size 1.27 1.27))))
\t\t(property "Sheetfile" "02_Power.kicad_sch" (at 40 61 0) (effects (font (size 1.27 1.27))))
\t)
\t(sheet
\t\t(at 100 40)
\t\t(size 40 20)
\t\t(uuid "{s2_uuid}")
\t\t(property "Sheetname" "03_MCU" (at 100 39 0) (effects (font (size 1.27 1.27))))
\t\t(property "Sheetfile" "03_MCU.kicad_sch" (at 100 61 0) (effects (font (size 1.27 1.27))))
\t)
\t(sheet_instances
\t\t(path "/"
\t\t\t(page "1")
\t\t)
\t)
)
""", encoding="utf-8")

    (tmp_path / "02_Power.kicad_sch").write_text(
        f'(kicad_sch (version 20260306) (generator "eeschema") (uuid "{sub1_uuid}") (paper "A4") (lib_symbols))\n',
        encoding="utf-8",
    )
    (tmp_path / "03_MCU.kicad_sch").write_text(
        f'(kicad_sch (version 20260306) (generator "eeschema") (uuid "{sub2_uuid}") (paper "A4") (lib_symbols))\n',
        encoding="utf-8",
    )

    before = audit_schematic_hierarchy(root_sch)
    kinds = {v.kind for v in before}
    assert "hierarchy_missing_sheet_page" in kinds

    assert repair_hierarchical_sheet_instances(root_sch, project_name="Demo") is True
    after = audit_schematic_hierarchy(root_sch)
    assert after == [], after
    txt = root_sch.read_text(encoding="utf-8")
    assert f'(path "/{root_uuid}"' in txt
    assert '(page "2")' in txt
    assert '(page "3")' in txt
