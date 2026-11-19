# SPDX-License-Identifier: MIT
"""F9 verification: doctor diagnostics, 3-client manifests, and packaging integrity."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from eda_agent.placer import doctor


def test_doctor_is_green_on_dev_environment():
    rep = doctor.run_doctor()
    assert rep["schema"] == "electronics-ai/doctor"
    assert rep["schema_version"] == 1
    assert rep["ok"] is True, f"doctor failed: {json.dumps(rep['checks'], indent=2)}"
    for key in ("platform", "python_packages", "rust_core", "kicad", "freerouting", "skill_and_mcp"):
        assert rep["checks"][key]["ok"] is True, f"check {key} failed: {rep['checks'][key]}"
    assert rep["checks"]["skill_and_mcp"]["live_tool_count"] == 11
    assert rep["checks"]["skill_and_mcp"]["skill_tools_match"] is True


def test_emit_client_manifests_for_claude_code_codex_and_antigravity(tmp_path: Path):
    written = doctor.emit_client_manifests(tmp_path, python_exe=r"C:\Apps\ElectronicsAI\python\python.exe")
    assert set(written) == {"claude-code", "codex", "antigravity", "readme"}

    claude = json.loads(Path(written["claude-code"]).read_text(encoding="utf-8"))
    ag = json.loads(Path(written["antigravity"]).read_text(encoding="utf-8"))
    codex = tomllib.loads(Path(written["codex"]).read_text(encoding="utf-8"))

    c_srv = claude["mcpServers"][doctor.SERVER_NAME]
    a_srv = ag["mcpServers"][doctor.SERVER_NAME]
    x_srv = codex["mcp_servers"][doctor.SERVER_NAME]

    # Single unified backend command + args across all 3 clients
    assert c_srv["command"] == a_srv["command"] == x_srv["command"]
    assert c_srv["args"] == a_srv["args"] == x_srv["args"]
    assert c_srv["args"] == ["-B", "-u", "-m", "eda_agent.placer.mcp"]
    assert x_srv["enabled"] is True


def test_check_clients_detects_all_three_clients(tmp_path: Path):
    # Empty synthetic home + empty synthetic repo -> none configured
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    none_state = doctor.check_clients(home=tmp_path, repo_root=fake_repo)
    assert all(not v["configured"] for v in none_state.values())

    # Write emitted manifests to synthetic user-level client locations (no shared .mcp.json)
    manifests = doctor.render_client_manifests(fake_repo)
    (tmp_path / ".claude.json").write_text(manifests["claude-code"]["content"], encoding="utf-8")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(manifests["codex"]["content"], encoding="utf-8")
    (tmp_path / ".gemini" / "antigravity").mkdir(parents=True)
    (tmp_path / ".gemini" / "antigravity" / "mcp_config.json").write_text(
        manifests["antigravity"]["content"], encoding="utf-8"
    )

    configured_state = doctor.check_clients(home=tmp_path, repo_root=fake_repo)
    assert configured_state["claude-code"]["configured"] is True
    assert configured_state["codex"]["configured"] is True
    assert configured_state["antigravity"]["configured"] is True
