# SPDX-License-Identifier: MIT
"""Phase 4 unit tests: Closed-loop S8 diagnose/refine/mcp, Law-P1 suggested actions, and 11-tool contract."""

from __future__ import annotations

import json
from pathlib import Path

from eda_agent.placer import diagnose, refine
from eda_agent.placer.intent.resolve import resolve
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
from eda_agent.placer.mcp import PlacerService


def _s8_board() -> Board:
    sq = [(-0.8, -0.5), (0.8, -0.5), (0.8, 0.5), (-0.8, 0.5)]
    u1 = Footprint(
        "U1", "MCU", "QFN-16_EP", 15.0, 15.0, 0.0, "F", locked=True, sheet="mcu",
        pads=[
            Pad("1", "VCC", (-2.0, 0.0), (0.5, 0.5), layers=("F.Cu",)),
            Pad("2", "GND", (2.0, 0.0), (0.5, 0.5), layers=("F.Cu",)),
            Pad("3", "SIG1", (0.0, 2.0), (0.5, 0.5), layers=("F.Cu",)),
            Pad("4", "SIG2", (0.0, -2.0), (0.5, 0.5), layers=("F.Cu",)),
            Pad("5", "SIG3", (-2.0, 2.0), (0.5, 0.5), layers=("F.Cu",)),
            Pad("6", "SIG4", (2.0, 2.0), (0.5, 0.5), layers=("F.Cu",)),
            Pad("7", "SIG5", (-2.0, -2.0), (0.5, 0.5), layers=("F.Cu",)),
            Pad("8", "GND", (0.0, 0.0), (2.6, 2.6), layers=("F.Cu",)),  # EP thermal pad
        ],
        courtyard=Courtyard("file", [[(-2.5, -2.5), (2.5, -2.5), (2.5, 2.5), (-2.5, 2.5)]]),
        uuid="u-U1",
    )
    c1 = Footprint(
        "C1", "100nF", "C_0402", 8.0, 15.0, 0.0, "F", locked=False, sheet="power",
        pads=[
            Pad("1", "GND", (-0.5, 0.0), (0.4, 0.4), layers=("F.Cu",)),
            Pad("2", "VCC", (0.5, 0.0), (0.4, 0.4), layers=("F.Cu",)),
        ],
        courtyard=Courtyard("file", [sq]),
        uuid="u-C1",
    )
    r1 = Footprint(
        "R1", "10k", "R_0402", 8.0, 18.0, 0.0, "F", locked=False, sheet="power",
        pads=[
            Pad("1", "SIG1", (-0.5, 0.0), (0.4, 0.4), layers=("F.Cu",)),
            Pad("2", "GND", (0.5, 0.0), (0.4, 0.4), layers=("F.Cu",)),
        ],
        courtyard=Courtyard("file", [sq]),
        uuid="u-R1",
    )
    b = Board(
        name="s8_board",
        footprints=[u1, c1, r1],
        outline=[Outline([(0.0, 0.0), (30.0, 0.0), (30.0, 30.0), (0.0, 30.0)])],
        board_area_mm2=900.0,
        copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)],
        netclasses=[NetClass("Default", 0.2, 0.2, 0.5, 0.25)],
        nets=[Net("VCC", "Default", 2), Net("GND", "Default", 4), Net("SIG1", "Default", 2)],
    )
    b.source = {"path": "s8_board.kicad_pcb", "sha256": "0" * 64}
    return derive(b)


def test_mcp_tool_count_stays_11_and_inspect_enriched(tmp_path: Path, monkeypatch):
    tools = PlacerService.list_tools()
    assert len(tools) == 11
    names = {t["name"] for t in tools}
    assert names == {
        "inspect", "score", "intent_draft", "intent_validate",
        "place_board", "place_cluster", "gallery", "diagnose",
        "refine", "apply", "revert",
    }

    b = _s8_board()
    dru_file = tmp_path / "s8_board.kicad_dru"
    dru_file.write_text(
        '(version 1)\n(rule "Via annular"\n  (condition "A.Type == \'via\'")\n  (constraint annular_width (min 0.14mm)))\n',
        encoding="utf-8",
    )
    b.source = {"path": str(tmp_path / "s8_board.kicad_pcb"), "sha256": "0" * 64}
    monkeypatch.setattr("eda_agent.placer.mcp.read_board", lambda _: b)
    summary = PlacerService(tmp_path).inspect(str(tmp_path / "s8_board.kicad_pcb"))
    assert summary["schematic_hierarchy"]["sheet_count"] == 2
    assert summary["schematic_hierarchy"]["sheets"] == ["mcu", "power"]
    assert summary["qfn_thermal_pads"] and summary["qfn_thermal_pads"][0]["ref"] == "U1"
    assert summary["dru_rules"]["min_via_annular_width_mm"] == 0.14


def test_intent_draft_supports_structured_law_p1_patches(tmp_path: Path, monkeypatch):
    b = _s8_board()
    monkeypatch.setattr("eda_agent.placer.mcp.read_board", lambda _: b)
    out = tmp_path / "patched.design_intent.json"
    res = PlacerService(tmp_path).intent_draft(
        str(tmp_path / "s8_board.kicad_pcb"),
        out_path=str(out),
        patches=[
            {"kind": "part_role", "ref": "R1", "value": "pull_up", "confidence": 0.95, "confirmed_by_user": True},
        ],
    )
    assert res["applied_patches"] == 1
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["parts"]["R1"]["role"]["value"] == "pull_up"
    assert saved["parts"]["R1"]["role"]["confirmed_by_user"] is True


def test_diagnose_surfaces_lint_warnings_dru_pagination_and_law_p1_actions(tmp_path: Path, monkeypatch):
    b = _s8_board()
    src = tmp_path / "s8_board.placed.kicad_pcb"
    src.write_bytes(b"pcb")
    _, intent_info = resolve(b)
    report = {
        "kind": "place_board",
        "artifacts": {"placed_board": str(src)},
        "inputs": {"board": {"path": str(src)}, "intent": intent_info},
        "stages": {
            "place": {"fixed": {"U1": "locked"}},
            "dsn_dru_compiled": {"translated": [], "untranslated": ["length_DDR"]},
        },
        "metrics": {
            "lint": {
                "score": 82.0,
                "critical_errors": 0,
                "warnings": 2,
                "findings": [
                    {
                        "rule": "decap_loop",
                        "severity": "warning",
                        "net": "VCC",
                        "refs": ["C1", "U1"],
                        "location_mm": [8.0, 15.0],
                        "detail": "decap copper path C1->U1 is 7.2 mm",
                    },
                    {
                        "rule": "return_via",
                        "severity": "warning",
                        "net": "SIG1",
                        "refs": [],
                        "location_mm": [12.0, 14.0],
                        "detail": "layer transition via missing return via",
                    },
                ],
            },
        },
        "failures": [],
    }
    (tmp_path / "run_report.json").write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(diagnose, "read_board", lambda _: b)

    val, _ = diagnose.diagnose(str(tmp_path), limit=10)
    kinds = {f["kind"] for f in val["findings"]}
    assert "lint_decap_loop" in kinds
    assert "lint_return_via" in kinds
    assert "dru_untranslated" in kinds
    # Suggested actions must be Law-P1-safe (symbolic refs only, zero raw x/y coords).
    assert val["suggested_actions"]
    for act in val["suggested_actions"]:
        blob = json.dumps(act)
        assert "x_mm" not in blob and "y_mm" not in blob
        assert act["tool"] in ("placer__refine", "placer__place_cluster")

    # Test filtering and pagination
    filt, _ = diagnose.diagnose(str(tmp_path), limit=1, kinds=["lint_return_via"], offset=0)
    assert filt["reported_findings"] == 1
    assert filt["findings"][0]["kind"] == "lint_return_via"


def test_refine_preserves_s6a_config_and_supports_new_primitives(tmp_path: Path, monkeypatch):
    b = _s8_board()
    src = tmp_path / "s8_board.placed.kicad_pcb"
    src.write_bytes(b"pcb")
    _, intent_info = resolve(b)
    report = {
        "kind": "place_board",
        "config": {"s6a": True, "side_policy": "level2_passives_back", "floorplan": True},
        "artifacts": {"placed_board": str(src)},
        "inputs": {"board": {"path": str(src)}, "intent": intent_info},
        "stages": {"place": {"fixed": {"U1": "locked"}}},
    }
    (tmp_path / "run_report.json").write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(refine, "read_board", lambda _: b.copy())
    monkeypatch.setattr(refine.legality, "check", lambda *a, **kw: [])

    # 1) Preview side_flip allowed under level2_passives_back
    flip_res = refine.refine(str(tmp_path), {"ref": "C1", "side_flip": True, "confidence": 0.9}, tmp_path / "t1", preview=True)
    assert flip_res["status"] == "preview"
    assert flip_res["pose_before"]["side"] == "F" and flip_res["pose_after"]["side"] == "B"

    # 2) Preview passive_align rotates C1 so VCC pad faces U1's VCC pad
    # C1 currently has pad 1=GND at x-0.5, pad 2=VCC at x+0.5; U1's VCC is at x=13 (right), GND at x=17 (farther right).
    # Move C1 with rotation=180 first so passive_align fixes it back to 0!
    b_rot = b.copy()
    b_rot.fp("C1").rotation = 180.0
    monkeypatch.setattr(refine, "read_board", lambda _: b_rot.copy())
    align_res = refine.refine(
        str(tmp_path),
        {"ref": "C1", "passive_align": True, "toward_ref": "U1", "step_mm": 0.5, "block_refs": ["R1"], "confidence": 0.9},
        tmp_path / "t2",
        preview=True,
    )
    assert align_res["status"] == "preview"
    assert align_res["pose_after"]["rotation_deg"] == 0

    # 3) Accepted refine round MUST preserve s6a=True and side_policy in output run_report.json!
    monkeypatch.setattr(refine, "read_board", lambda _: b.copy())

    def fake_write(_s, _b, dest, **kw):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"copy")
        return dest

    monkeypatch.setattr(refine, "write_board", fake_write)
    monkeypatch.setattr(
        "eda_agent.placer.route.s6a.apply_to_board",
        lambda bp, _in, _od, **kw: {"applied": False, "s6a_board": None},
    )

    def fake_route(path, *, router_timeout_s, **kw):
        is_base = path.parent.name == "baseline"
        unr = 2 if is_base else 0
        return {
            "metrics": {"settled": True, "denominator": 4, "unrouted": unr, "unrouted_signal": unr,
                        "unrouted_plane": 0, "coverage": 1.0 - unr / 4.0},
            "drc": {"router_caused": [], "new_non_routing": []},
            "passed": True,
            "gates": [{"name": "drc_router_caused", "passed": True}],
            "artifacts": {"routed_board": str(path)},
        }

    monkeypatch.setattr(refine, "route_check", fake_route)
    acc = refine.refine(
        str(tmp_path),
        {"ref": "C1", "toward_ref": "U1", "step_mm": 1.0, "block_refs": ["R1"], "confidence": 0.9},
        tmp_path / "t3",
        route_timeout_s=15,
    )
    assert acc["status"] == "accepted"
    assert acc["moved_block_refs"] == ["R1"]
    out_rep = json.loads(Path(acc["run_report"]).read_text(encoding="utf-8"))
    assert out_rep["config"]["s6a"] is True
    assert out_rep["config"]["side_policy"] == "level2_passives_back"
