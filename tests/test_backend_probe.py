"""Explicit initialization diagnosis never invokes a CAD tool."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('field_plugin_runtime', ROOT / 'scripts/plugin_runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def test_real_worker_initialization_probe_and_missing_executable_diagnosis(tmp_path):
    marker = tmp_path / 'no_native_call.txt'
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'backends': {'eda': {'command': sys.executable,
        'args': [str(ROOT / 'tests/suite_fake_worker.py'), str(marker)]}}}))
    result = runtime.probe_backend_initialization(manifest, timeout_seconds=10)
    assert result['initialized'] and result['tool_count'] == 6
    assert result['native_editor_probed'] is False and not marker.exists()
    manifest.write_text(json.dumps({'backends': {'eda': {'command': str(tmp_path / 'missing-exe')}}}))
    result = runtime.probe_backend_initialization(manifest, timeout_seconds=10)
    assert result['status'] == 'failed' and not result['initialized']
    assert result['diagnostic']['kind'] == 'process_start'
    assert not result['automatic_retry'] and not marker.exists()


def test_board_identity_keeps_logical_workspace_path(tmp_path, monkeypatch):
    from suite_si_pi import _path
    board = tmp_path / 'board.PcbDoc'
    def forbidden_resolve(*args, **kwargs):
        raise AssertionError('Loaded document identities must not resolve workspace junctions')
    monkeypatch.setattr(type(board), 'resolve', forbidden_resolve)
    assert _path(str(board)) == str(board)


def test_probe_imports_packaged_worker_from_unrelated_directory(tmp_path):
    """A fresh interpreter has neither checkout cwd nor pytest import paths."""
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'backends': {'eda': {'command': sys.executable,
        'args': [str(ROOT / 'tests/suite_fake_worker.py'), str(tmp_path / 'native-call')]}}}))
    program = (
        "import json, runpy; "
        f"runtime = runpy.run_path({str(ROOT / 'scripts/plugin_runtime.py')!r}); "
        f"result = runtime['probe_backend_initialization']({str(manifest)!r}, timeout_seconds=10); "
        "print(json.dumps(result))"
    )
    completed = subprocess.run([sys.executable, '-I', '-c', program], cwd=tmp_path,
                               capture_output=True, text=True, timeout=30, check=True)
    result = json.loads(completed.stdout)
    assert result['initialized'] and result['tool_count'] == 6
    assert result['native_editor_probed'] is False
