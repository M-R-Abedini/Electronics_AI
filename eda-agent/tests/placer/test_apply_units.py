# SPDX-License-Identifier: MIT
"""Unit and safety tests for F7 safe transactional apply & revert."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from eda_agent.placer import apply as placer_apply
from eda_agent.placer.apply import ApplyError, apply_run, list_applies, revert_apply
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
from eda_agent.placer.toolchain import sha256_file


def _make_fake_run(
    tmp_path: Path,
    *,
    passed: bool = True,
    with_routed: bool = True,
    with_sidecar: bool = True,
) -> tuple[Path, Path, Path]:
    """Create an isolated synthetic board + run_report.json for testing apply/revert."""
    src_dir = tmp_path / "project"
    src_dir.mkdir(parents=True, exist_ok=True)
    src_board = src_dir / "demo.kicad_pcb"
    src_board.write_text("(kicad_pcb (version 20240108) (original))\n", encoding="utf-8")
    if with_sidecar:
        src_board.with_suffix(".kicad_pro").write_text('{"project": "orig"}\n', encoding="utf-8")

    run_dir = tmp_path / "run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    placed_board = run_dir / "demo.placed.kicad_pcb"
    placed_board.write_text("(kicad_pcb (version 20240108) (placed))\n", encoding="utf-8")

    if with_routed:
        route_dir = run_dir / "route"
        (route_dir / "output").mkdir(parents=True, exist_ok=True)
        routed_board = route_dir / "output" / "demo.routed.kicad_pcb"
        routed_board.write_text("(kicad_pcb (version 20240108) (routed))\n", encoding="utf-8")
        (route_dir / "run_report.json").write_text(
            json.dumps({"artifacts": {"routed_board": str(routed_board)}, "passed": passed}),
            encoding="utf-8",
        )

    rep = {
        "kind": "place_board",
        "passed": passed,
        "gates": [{"name": "legality_violations", "value": 0 if passed else 1, "op": "==", "threshold": 0, "passed": passed}],
        "inputs": {"board": {"path": str(src_board), "sha256": sha256_file(src_board)}},
        "artifacts": {"run_dir": str(run_dir), "placed_board": str(placed_board)},
    }
    report_path = run_dir / "run_report.json"
    report_path.write_text(json.dumps(rep), encoding="utf-8")
    return src_board, run_dir, placed_board


def test_copy_apply_keeps_original_untouched_and_reverts_cleanly(tmp_path: Path):
    src_board, run_dir, _ = _make_fake_run(tmp_path, passed=True, with_routed=True)
    orig_sha = sha256_file(src_board)
    hist = tmp_path / "history"

    m = apply_run(run_dir, history_dir=hist)
    target = Path(m["target_path"])
    assert m["in_place"] is False
    assert m["artifact_kind"] == "routed"
    assert target != src_board and target.is_file()
    assert target.with_suffix(".kicad_pro").is_file()
    assert sha256_file(src_board) == orig_sha  # P8: original untouched
    assert "(routed)" in target.read_text(encoding="utf-8")

    rev = revert_apply(m["apply_id"], history_dir=hist)
    assert rev["reverted"] is True
    assert rev["within_budget"] is True
    assert rev["elapsed_ms"] < 2000.0
    assert rev["input_untouched"] is True
    assert not target.exists()
    assert not target.with_suffix(".kicad_pro").exists()
    assert sha256_file(src_board) == orig_sha


def test_in_place_apply_and_revert_restores_exact_sha256_under_2s(tmp_path: Path):
    src_board, run_dir, _ = _make_fake_run(tmp_path, passed=True, with_routed=False)
    orig_sha = sha256_file(src_board)
    hist = tmp_path / "history"

    m = apply_run(run_dir, history_dir=hist, in_place=True)
    assert m["in_place"] is True
    assert m["artifact_kind"] == "placed"
    assert Path(m["target_path"]) == src_board
    assert sha256_file(src_board) == m["after_sha256"]
    assert sha256_file(src_board) != orig_sha
    assert len(list_applies(hist)) == 1

    rev = revert_apply("latest", history_dir=hist)
    assert rev["reverted"] is True
    assert rev["within_budget"] is True
    assert rev["elapsed_ms"] < 2000.0
    assert rev["restored_sha256"] == orig_sha
    assert sha256_file(src_board) == orig_sha

    # Double revert is rejected cleanly.
    with pytest.raises(ApplyError, match="already reverted"):
        revert_apply(m["apply_id"], history_dir=hist)


def test_apply_enforces_gates_p8_stale_target_and_post_apply_edit_guard(tmp_path: Path):
    src_board, run_dir, _ = _make_fake_run(tmp_path, passed=False)
    hist = tmp_path / "history"

    # 1. Failed gate blocked by default
    with pytest.raises(ApplyError, match="failed gates"):
        apply_run(run_dir, history_dir=hist)

    # 2. P8 blocks targeting source path without in_place=True
    with pytest.raises(ApplyError, match="principle P8"):
        apply_run(run_dir, history_dir=hist, target_path=src_board, allow_failed_gates=True)

    # 3. Stale target blocked when source board changed after run
    src_board.write_text("(kicad_pcb (user_edited_concurrently))\n", encoding="utf-8")
    with pytest.raises(ApplyError, match="stale target board"):
        apply_run(run_dir, history_dir=hist, in_place=True, allow_failed_gates=True)

    # 4. Post-apply manual edit blocks revert unless force=True
    m = apply_run(run_dir, history_dir=hist, in_place=True, allow_failed_gates=True, force_stale=True)
    src_board.write_text("(kicad_pcb (manual_post_apply_edit))\n", encoding="utf-8")
    with pytest.raises(ApplyError, match="edited after apply"):
        revert_apply(m["apply_id"], history_dir=hist)

    rev = revert_apply(m["apply_id"], history_dir=hist, force=True)
    assert rev["reverted"] is True
    assert "(user_edited_concurrently)" in src_board.read_text(encoding="utf-8")

    # 5. Path traversal apply_id rejected
    with pytest.raises(ApplyError, match="invalid apply_id"):
        revert_apply("../escape", history_dir=hist)


def test_copy_apply_backs_up_preexisting_target_sidecar(tmp_path: Path):
    _, run_dir, _ = _make_fake_run(tmp_path, passed=True, with_routed=False)
    hist = tmp_path / "history"
    target = tmp_path / "project" / "custom_out.kicad_pcb"
    pre_sidecar = target.with_suffix(".kicad_pro")
    pre_sidecar.write_text('{"project": "preexisting_custom"}\n', encoding="utf-8")
    pre_sha = sha256_file(pre_sidecar)

    m = apply_run(run_dir, history_dir=hist, target_path=target)
    assert sha256_file(pre_sidecar) != pre_sha

    rev = revert_apply(m["apply_id"], history_dir=hist)
    assert rev["reverted"] is True
    assert not target.exists()
    assert pre_sidecar.is_file() and sha256_file(pre_sidecar) == pre_sha


def _synth_board(x: float, side: str) -> Board:
    sq = [(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)]
    fp = Footprint(
        ref="R1", value="10k", lib_id="lib:R", x=x, y=5.0, rotation=90.0, side=side,
        pads=[Pad("1", "A", (-0.5, 0.0), (0.5, 0.5), layers=("F.Cu",))],
        courtyard=Courtyard("file", [sq]), uuid="u-R1",
    )
    return derive(Board(
        name="t", footprints=[fp], outline=[Outline([(0, 0), (20, 0), (20, 20), (0, 20)])],
        board_area_mm2=400.0, copper_layers=[CopperLayer("F.Cu", "F.Cu", 0), CopperLayer("B.Cu", "B.Cu", 1)],
        netclasses=[NetClass("Default", 0.2, 0.2, 0.6, 0.3)], nets=[Net("A", "Default", 1)],
    ))


def test_altium_plan_enforces_level1_no_flip_and_emits_delta_bundle(tmp_path: Path, monkeypatch):
    src_board, run_dir, _ = _make_fake_run(tmp_path, passed=True, with_routed=False)
    hist = tmp_path / "history"

    # Flip from F -> B must be blocked at assembly level 1
    monkeypatch.setattr(
        placer_apply, "read_board",
        lambda p: _synth_board(2.0, "F") if Path(p).resolve() == src_board.resolve() else _synth_board(8.0, "B"),
    )
    with pytest.raises(ApplyError, match="assembly level 1"):
        apply_run(run_dir, history_dir=hist, adapter="altium_plan")

    # Same-side move succeeds and writes apply + inverse revert plan with Y inverted (+Y up for Altium)
    monkeypatch.setattr(
        placer_apply, "read_board",
        lambda p: _synth_board(2.0, "F") if Path(p).resolve() == src_board.resolve() else _synth_board(8.0, "F"),
    )
    m = apply_run(run_dir, history_dir=hist, adapter="altium_plan")
    assert m["adapter"] == "altium_plan"
    apply_doc = json.loads(Path(m["altium_bundle"]["apply_plan"]).read_text(encoding="utf-8"))
    revert_doc = json.loads(Path(m["altium_bundle"]["revert_plan"]).read_text(encoding="utf-8"))
    assert apply_doc["operations"] == [{"ref": "R1", "x_mm": 8.0, "y_mm": -5.0, "rotation_deg": 90.0, "layer": "Top"}]
    assert revert_doc["operations"] == [{"ref": "R1", "x_mm": 2.0, "y_mm": -5.0, "rotation_deg": 90.0, "layer": "Top"}]

    rev = revert_apply(m["apply_id"], history_dir=hist)
    assert rev["reverted"] is True and rev["within_budget"] is True


def test_mcp_service_exposes_and_runs_apply_and_revert(tmp_path: Path):
    src_board, run_dir, _ = _make_fake_run(tmp_path, passed=True, with_routed=True)
    orig_sha = sha256_file(src_board)
    service = PlacerService(tmp_path / "mcp_data")

    names = {t["name"] for t in service.list_tools()}
    assert {"apply", "revert"} <= names

    dry = service.apply(str(run_dir), dry_run=True)
    assert dry["dry_run"] is True and dry["artifact_kind"] == "routed"

    applied = service.apply(str(run_dir), in_place=True)
    assert sha256_file(src_board) == applied["after_sha256"]

    reverted = service.revert(applied["apply_id"])
    assert reverted["reverted"] is True
    assert reverted["within_budget"] is True
    assert sha256_file(src_board) == orig_sha
