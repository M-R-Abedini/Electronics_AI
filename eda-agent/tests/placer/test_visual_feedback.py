# SPDX-License-Identifier: MIT
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from eda_agent.placer import diagnose, refine
from eda_agent.placer.intent.resolve import resolve
from eda_agent.placer.ir import Board, Courtyard, Footprint, Outline, Pad
from eda_agent.placer.mcp import PlacerService


def _board():
    pad = Pad("1", "VCC", (0, 0), (0.5, 0.5), layers=("F.Cu",))
    footprint = Footprint("R1", "r", "res", 5, 5, 0, "F", pads=[pad], uuid="r1",
                          courtyard=Courtyard("file", [[(-1, -1), (1, -1), (1, 1), (-1, 1)]]))
    return Board(name="test", footprints=[footprint], outline=[Outline([(0, 0), (20, 0), (20, 20), (0, 20)])])


def _report(tmp_path, **section):
    source = tmp_path / "test.placed.kicad_pcb"
    source.write_bytes(b"original PCB")
    _, intent = resolve(_board())
    report = {"artifacts": {"placed_board": str(source)}, "inputs": {"board": {"path": str(source)}, "intent": intent},
              "stages": {"place": {"fixed": {}}}, **section}
    (tmp_path / "run_report.json").write_text(json.dumps(report), encoding="utf-8")
    return source


def test_diagnose_reports_route_timeout_and_emits_a_crop(tmp_path, monkeypatch):
    _report(tmp_path, metrics={"router_stop": "timeout", "settled": False, "unrouted": 12},
            failures=[{"stage": "S6c", "cause": "escape_blocked", "net": "VCC",
                       "evidence": [{"description": "Pad 1 [VCC] of R1 on F.Cu", "pos": {"x": 5, "y": 5}}]}])
    monkeypatch.setattr(diagnose, "read_board", lambda _: _board())
    result = PlacerService().diagnose(str(tmp_path))
    assert result.structuredContent["route"]["status"] == "inconclusive"
    assert result.structuredContent["route"]["unlocalized_open_links"] == 11
    finding = result.structuredContent["findings"][0]
    assert not finding["cause_confirmed"] and finding["points"][0]["pad"] == "1"
    picture = next(entry for entry in result.content if entry.type == "image")
    assert base64.b64decode(picture.data).startswith(b"\x89PNG\r\n\x1a\n")


def test_refinement_preview_moves_away_from_named_pad_without_writing(tmp_path, monkeypatch):
    source = _report(tmp_path)
    board = _board()
    target = Footprint("R2", "r", "res", 5, 8, 0, "F", uuid="r2", locked=True,
                       pads=[Pad("2", "VCC", (3, 0), (0.5, 0.5), layers=("F.Cu",))])
    board.footprints.append(target)
    board.reindex()
    _, intent = resolve(board)
    report = tmp_path / "run_report.json"
    value = json.loads(report.read_text())
    value["inputs"]["intent"] = intent
    report.write_text(json.dumps(value))
    monkeypatch.setattr(refine, "read_board", lambda _: board)

    def check(candidate, **_):
        assert candidate.fp("R1").position == pytest.approx((5 - 0.5 ** 0.5, 5 - 0.5 ** 0.5))
        return []

    monkeypatch.setattr(refine.legality, "check", check)
    result = refine.refine(str(tmp_path), {"ref": "R1", "away_from_ref": "R2", "target_pad": "2", "step_mm": 1},
                           tmp_path / "trials", preview=True)
    assert result["status"] == "preview"
    assert not (tmp_path / "trials").exists()
    assert source.read_bytes() == b"original PCB"


@pytest.mark.parametrize(("after", "settled", "expected"), [(2, True, "rejected"), (0, True, "accepted"),
                                                              (0, False, "inconclusive")])
def test_refinement_guards_source_and_continues_only_from_verified_run(tmp_path, monkeypatch, after, settled, expected):
    source = _report(tmp_path)
    monkeypatch.setattr(refine, "read_board", lambda _: _board())
    monkeypatch.setattr(refine.legality, "check", lambda *a, **kw: [])

    def write(_src, _board, dest, **kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"copy")
        return dest

    monkeypatch.setattr(refine, "write_board", write)

    def route(path, *, router_timeout_s, **kwargs):
        assert router_timeout_s == 15 and Path(path) != source
        baseline = path.parent.name == "baseline"
        unrouted = 1 if baseline else after
        return {"metrics": {"settled": baseline or settled, "denominator": 3, "unrouted": unrouted,
                            "unrouted_signal": unrouted, "unrouted_plane": 0, "coverage": 1 - unrouted / 3},
                "drc": {"router_caused": [{}] if baseline else [], "new_non_routing": []}, "passed": not baseline,
                "gates": [{"name": "drc_router_caused", "passed": not baseline}],
                "artifacts": {"routed_board": str(path)}}

    monkeypatch.setattr(refine, "route_check", route)
    result = refine.refine(str(tmp_path), {"ref": "R1", "rotation_deg": 90, "confidence": 0.8},
                           tmp_path / "trials", route_timeout_s=15)
    assert result["status"] == expected
    assert result["baseline"] == 1 and result["candidate"] == after
    assert source.read_bytes() == b"original PCB"
    if expected == "accepted":
        assert Path(result["retained_board"]) != source
        assert json.loads(Path(result["run_report"]).read_text(encoding="utf-8"))["refinement_round"] == 1
    else:
        assert Path(result["retained_board"]) == source
