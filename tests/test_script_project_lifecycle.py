"""Managed project catalog ownership and reload behavior, without an editor."""
import hashlib
import json
from pathlib import Path
import shutil

import pytest
import script_projects as registry
import suite_config


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    root = tmp_path / 'sources'
    for relative in ('eda-agent/scripts/altium',):
        shutil.copytree(suite_config.ROOT / relative, root / relative)
    monkeypatch.setattr(suite_config, 'ROOT', root)
    runtime = tmp_path / "owner's runtime"
    monkeypatch.setenv('ELECTRONICS_AI_RUNTIME', str(runtime))
    return runtime, root


def test_reload_tracks_old_and_new_without_replacing_sources(runtime):
    folder, source = runtime
    first = suite_config.prepare_runtime()
    original = (first['eda'].parent / 'Main.pas').read_bytes()
    main = source / 'eda-agent/scripts/altium/Main.pas'
    main.write_bytes(main.read_bytes() + b'\n{ regression reload }\n')
    second = suite_config.prepare_runtime()
    assert first['eda'] != second['eda']
    assert (first['eda'].parent / 'Main.pas').read_bytes() == original
    assert {first['eda'], second['eda']} == registry.refresh_catalog(folder)
    assert second['eda'].name == 'ElectronicsAI-EDA.PrjScr'
    assert not list((folder / 'scripts').glob('legacy-*'))
    bound = (second['eda'].parent / 'Main.pas').read_text()
    assert str(second['eda']).replace("'", "''") in bound
    assert '__ELECTRONICS_AI_SCRIPT_' not in bound
    catalog = folder / registry.CATALOG
    stamp = catalog.stat().st_mtime_ns
    assert suite_config.prepare_runtime() == second
    assert catalog.stat().st_mtime_ns == stamp


def test_native_workspace_is_embedded_and_changes_generation(runtime, monkeypatch):
    folder, _ = runtime
    first_work=folder/'first workspace'
    monkeypatch.setenv('EDA_AGENT_WORKSPACE',str(first_work))
    first=suite_config.prepare_runtime()
    source=(first['eda'].parent/'Main.pas').read_text()
    assert str(first_work).replace("'","''") in source
    assert '__ELECTRONICS_AI_WORKSPACE__' not in source
    second_work=folder/'second workspace'
    monkeypatch.setenv('EDA_AGENT_WORKSPACE',str(second_work))
    second=suite_config.prepare_runtime()
    assert first['eda']!=second['eda']
    source=(second['eda'].parent/'Main.pas').read_text()
    assert str(second_work).replace("'","''") in source


def test_unreferenced_generated_monolith_does_not_change_native_identity(runtime):
    folder, root = runtime
    first=suite_config.prepare_runtime()
    mono=root/'eda-agent/scripts/altium/Altium_MCP.pas'
    mono.write_text('{ a different local monolithic build }')
    assert suite_config.prepare_runtime()==first
    assert not (first['eda'].parent/'Altium_MCP.pas').exists()
    assert 'Altium_MCP.pas' not in json.loads((first['eda'].parent/registry.MARKER).read_text())['files']


def test_suite_uses_private_pointer_without_overriding_explicit_test_pointer(runtime, monkeypatch):
    folder, _ = runtime
    import os
    # configure_environment sets these directly; reserve undo entries so a
    # test's generated script/workspace cannot leak into subsequent tests.
    for name in ('ELECTRONICS_AI_EDA_SCRIPT',):
        monkeypatch.setenv(name,os.environ.get(name,''))
    monkeypatch.setenv('EDA_AGENT_WORKSPACE',str(folder/'workspace'))
    exe=folder/'X2.EXE';exe.parent.mkdir(parents=True,exist_ok=True);exe.touch()
    monkeypatch.setenv('ALTIUM_EXE',str(exe))
    monkeypatch.delenv('EDA_AGENT_POINTER_FILE',raising=False)
    suite_config.configure_environment()
    assert Path(__import__('os').environ['EDA_AGENT_POINTER_FILE'])==folder/'workspace-path.txt'
    explicit=folder/'test-pointer'
    monkeypatch.setenv('EDA_AGENT_POINTER_FILE',str(explicit))
    suite_config.configure_environment()
    assert Path(__import__('os').environ['EDA_AGENT_POINTER_FILE'])==explicit


def test_changed_generated_file_is_preserved_and_excluded(runtime):
    folder, _ = runtime
    project = suite_config.prepare_runtime()['eda']
    main = project.parent / 'Main.pas'
    main.write_text('user edits')
    assert project not in registry.refresh_catalog(folder)
    with pytest.raises(RuntimeError, match='Generated script was changed'):
        suite_config.prepare_runtime()
    assert main.read_text() == 'user edits'


@pytest.mark.parametrize('damage', ['outside', 'traversal', 'wrong_kind', 'corrupt', 'wrong_hash'])
def test_damaged_or_external_markers_never_enter_catalog(runtime, damage):
    folder, _ = runtime
    project = suite_config.prepare_runtime()['eda']
    marker = project.parent / registry.MARKER
    record = json.loads(marker.read_text())
    if damage == 'outside':
        record['project_path'] = str(folder.parent / 'Altium_API.PrjScr')
    elif damage == 'traversal':
        record['files']['../private.SchDoc'] = '0' * 64
    elif damage == 'wrong_kind':
        record['kind'] = 'sandbox'
    elif damage == 'wrong_hash':
        record['files']['Main.pas'] = '0' * 64
    marker.write_text('{' if damage == 'corrupt' else json.dumps(record))
    assert project not in registry.refresh_catalog(folder)






def test_native_cleanup_excludes_current_and_requires_verified_focus():
    text = (suite_config.ROOT / 'eda-agent/scripts/altium/Project.pas').read_text()
    close = text.split('Function Proj_Close(', 1)[1].split('Procedure CleanupManagedScriptProjects(', 1)[0]
    cleanup = text.split('Procedure CleanupManagedScriptProjects(', 1)[1].split('Function Proj_GetDocuments(', 1)[0]
    assert 'UpperCase(Candidate) = UpperCase(SUITE_SCRIPT_PROJECT)' in cleanup
    assert '"only_if_unmodified":true' in cleanup
    assert 'ManagedScriptProjectCloseBlocker' in close
    assert close.index('DM_LoadDocument') < close.index('Client.ShowDocument') < close.index("RunProcess('WorkspaceManager:CloseObject')")
    assert close.index('FocusedDocument.DM_Project') < close.index("RunProcess('WorkspaceManager:CloseObject')")
    assert 'SCRIPT_PROJECT_RUNNING' in close
    assert "'FocusedProjectAndDocuments'" in close
    assert "'ProjectAndDocuments'" not in close  # closes active hardware project, ignoring FileName
    assert 'Client.ShowDocument(SourceEditor)' in close.split('Finally', 1)[1]
    blocker = text.split('Function ManagedScriptProjectCloseBlocker(', 1)[1].split('Function Proj_Close(', 1)[0]
    assert 'source editor open:' in blocker
    assert 'source modified:' not in blocker  # script-editor Modified=False is unreliable


def test_design_project_close_guard_precedes_every_side_effect():
    text = (suite_config.ROOT / 'eda-agent/scripts/altium/Project.pas').read_text()
    close = text.split('Function Proj_Close(', 1)[1].split('Procedure CleanupManagedScriptProjects(', 1)[0]
    guard = close.index("If (UpperCase(ExtractFileExt(ProjectPath)) <> '.PRJSCR'")
    refusal = close.index("'PROJECT_CLOSE_REQUIRES_UI'")
    assert guard < refusal < close.index('SaveProjectMembers(Project)')
    assert refusal < close.index('Project.DM_SetAsCurrentProject')
    assert refusal < close.index("RunProcess('WorkspaceManager:CloseObject')")
    assert 'Exit;' in close[refusal:close.index('SaveProjectMembers(Project)')]


def test_native_cleanup_finishes_before_readiness():
    text = (suite_config.ROOT / 'eda-agent/scripts/altium/Dispatcher.pas').read_text()
    startup = text.split('Procedure StartMCPServer', 1)[1]
    assert startup.index('CleanupOrphanRequests(0)') < startup.index('CleanupManagedScriptProjects(0)')
    assert startup.index('CleanupManagedScriptProjects(0)') < startup.index('Running := True')
    assert startup.index('CleanupManagedScriptProjects(0)') < startup.index("WriteFileContent(WorkspaceDir + 'bridge-ready.json'")




def test_no_marker_adoption_or_retired_project_aliases(runtime):
    folder, source = runtime
    projects = suite_config.prepare_runtime()
    assert set(projects) == {'eda'}
    assert set(registry.PROJECT_NAMES) == {'eda'}
    unowned = folder / 'scripts' / ('eda-' + '0' * 16)
    shutil.copytree(source / 'eda-agent/scripts/altium', unowned)
    assert registry.refresh_catalog(folder) == {projects['eda']}
    assert not (unowned / registry.MARKER).exists()
