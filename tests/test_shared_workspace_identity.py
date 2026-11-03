"""All Electronics AI clients share one durable native workspace identity."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest
import suite_config


def test_missing_override_inherits_binding_and_conflict_cannot_replace_it(tmp_path, monkeypatch):
    runtime, workspace = tmp_path / 'runtime', tmp_path / 'eda'
    monkeypatch.setenv('ELECTRONICS_AI_RUNTIME', str(runtime))
    monkeypatch.setenv('EDA_AGENT_WORKSPACE', str(workspace))
    assert suite_config.bind_workspace() == workspace
    original = (runtime / 'workspace-identity.json').read_bytes()
    monkeypatch.delenv('EDA_AGENT_WORKSPACE')
    assert suite_config.workspace_dir() == workspace
    assert suite_config.bind_workspace() == workspace
    monkeypatch.setenv('EDA_AGENT_WORKSPACE', str(tmp_path / 'other'))
    with pytest.raises(RuntimeError, match='Workspace mismatch'):
        suite_config.bind_workspace()
    assert (runtime / 'workspace-identity.json').read_bytes() == original


@pytest.mark.parametrize('value', ['{', '[]', '{}', '{"schema":1,"workspace":"relative"}', '{"schema":1,"workspace":""}'])
def test_corrupt_identity_fails_closed(tmp_path, monkeypatch, value):
    monkeypatch.setenv('ELECTRONICS_AI_RUNTIME', str(tmp_path))
    (tmp_path / 'workspace-identity.json').write_text(value)
    with pytest.raises(RuntimeError, match='Cannot validate'):
        suite_config.workspace_dir()
    assert (tmp_path / 'workspace-identity.json').read_text() == value


def test_concurrent_conflicting_startup_publishes_one_complete_identity(tmp_path):
    runtime = tmp_path / 'runtime'
    program = 'import suite_config; print(suite_config.bind_workspace())'
    processes = [subprocess.Popen([sys.executable, '-c', program], cwd=suite_config.ROOT,
        env=dict(os.environ, ELECTRONICS_AI_RUNTIME=str(runtime), EDA_AGENT_WORKSPACE=str(tmp_path / name)),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for name in ('a', 'b')]
    results = [(p, p.communicate(timeout=20)) for p in processes]
    assert sorted(p.returncode for p, _ in results) == [0, 1]
    identity = json.loads((runtime / 'workspace-identity.json').read_text())
    winner = next(stdout.strip() for p, (stdout, _) in results if p.returncode == 0)
    assert identity == {'schema': 1, 'workspace': winner}
    assert not list(runtime.glob('*.tmp'))
