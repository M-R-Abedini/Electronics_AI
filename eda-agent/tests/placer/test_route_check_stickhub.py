# SPDX-License-Identifier: MIT
"""F0a acceptance on StickHub (slow: ~3 minutes). Opt in with ELECTRONICS_AI_SLOW=1.

Gates (plan F0a): zero router-caused DRC, accounting equals the netlist
denominator (226 connections), every unrouted connection listed with a cause,
the whole path within 5 minutes, and two runs give the same session.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from eda_agent.placer.io.kicad import read_board, run_helper
from eda_agent.placer.ir import plane_zone_layers
from eda_agent.placer.macro_proof import apply_instance
from eda_agent.placer.route.check import route_check
from eda_agent.placer.route.s6a import _instance

STICKHUB = Path(r"C:\Program Files\KiCad\10.0\share\kicad\demos\stickhub\StickHub.kicad_pcb")

pytestmark = pytest.mark.skipif(
    os.environ.get("ELECTRONICS_AI_SLOW") != "1" or not STICKHUB.is_file(),
    reason="slow KiCad + Freerouting run; set ELECTRONICS_AI_SLOW=1",
)


def test_stickhub_strict_route_check_is_clean_and_deterministic(tmp_path):
    first = route_check(STICKHUB, out_dir=tmp_path / "a")
    second = route_check(STICKHUB, out_dir=tmp_path / "b")
    for report in (first, second):
        failed = [g for g in report["gates"] if not g["passed"]]
        assert not failed, failed
        assert report["metrics"]["denominator"] == 226
        assert all(f["cause"] for f in report["failures"])
    assert first["metrics"]["session_sha256"] == second["metrics"]["session_sha256"]


def test_generated_copper_and_dsn_are_stable_across_directories(tmp_path):
    source = read_board(STICKHUB)
    instance = _instance(source.routing["tracks"][:8] + source.routing["vias"][:2])
    exported = []
    for name in ("a", "b"):
        folder = tmp_path / name
        board = folder / STICKHUB.name
        apply_instance(STICKHUB, instance, board, keep_only=None, strip="tracks")
        keep = folder / "keep.json"
        keep.write_text(json.dumps(plane_zone_layers(read_board(board))), encoding="utf-8")
        dsn = folder / "board.dsn"
        run_helper("prepare", board, keep, folder / "prepared.kicad_pcb", dsn, folder / "prepare.json")
        # The DSN header carries the destination path.
        exported.append((board.read_bytes(), dsn.read_bytes().splitlines()[1:]))
    assert exported[0] == exported[1]
