"""Private modern runtime setup, integrity checks and real Windows stdio launch."""
import asyncio
from datetime import timedelta
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('electronics_ai_plugin_runtime', ROOT / 'scripts/plugin_runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def make_template(tmp_path):
    package = make_root(tmp_path / 'package')
    altium = tmp_path / 'X2.EXE'; altium.touch()
    config = tmp_path / 'libraries.json'
    config.write_text(json.dumps({'allowed_paths': [str(tmp_path)], 'logging': {'level': 'warn'}}))
    common = {'ELECTRONICS_AI_RUNTIME': str(tmp_path / 'native-runtime'),
              'EDA_AGENT_WORKSPACE': str(tmp_path / 'native-workspace'), 'ALTIUM_EXE': str(altium)}
    template = {'backends': {
        'eda': {'command': sys.executable, 'args': [str(package / 'eda_stdio.py'), '--toolset', 'minimal'],
                'env': common, 'timeout_seconds': 123, 'disabled_tools': ['blocked'], 'enabled_tools': None},
        'libraries': {'command': sys.executable,
                      'args': [str(package / 'suite_libraries_native.py'), str(config)], 'timeout_seconds': 180},
    }}
    return template, altium


def make_root(path):
    path.mkdir(parents=True)
    for name in ('eda_stdio.py', 'suite_libraries_native.py'):
        (path / name).touch()
    return path


def configured(tmp_path):
    template, altium = make_template(tmp_path)
    data = tmp_path / 'private'
    runtime.validate_template(template)
    runtime.persist(data, {'schema': 1, 'python': sys.executable,
                          'cloud_cache': str(tmp_path / 'cloud'), 'altium_exe': str(altium)}, template)
    return data, template


def test_current_package_remap_retains_private_policy(tmp_path):
    data, template = configured(tmp_path)
    first = make_root(tmp_path / 'version one')
    second = make_root(tmp_path / 'version two')
    _, first_manifest = runtime.launch_configuration(data, first)
    assert runtime.launch_configuration(data, first)[1] == first_manifest
    _, upgraded = runtime.launch_configuration(data, second)
    assert upgraded != first_manifest
    workers = json.loads(upgraded.read_text())['backends']
    assert set(workers) == {'eda', 'libraries'}
    assert workers['eda']['args'] == [str(second / 'eda_stdio.py'), '--toolset', 'full', '--no-dashboard']
    assert workers['eda']['env'] == dict(template['backends']['eda']['env'], EDA_AGENT_BACKEND='altium')
    assert workers['eda']['disabled_tools'] == ['blocked']
    assert workers['eda']['timeout_seconds'] == 123
    assert workers['libraries']['args'] == [str(second / 'suite_libraries_native.py'), template['backends']['libraries']['args'][1]]
    assert runtime.checked_state(data)[1] == template


def test_tampered_manifest_and_state_pair_are_refused(tmp_path):
    data, _ = configured(tmp_path)
    root = make_root(tmp_path / 'new package')
    _, manifest = runtime.launch_configuration(data, root)
    manifest.write_bytes(b'changed')
    with pytest.raises(ValueError, match='launch manifest was changed'):
        runtime.launch_configuration(data, root)
    template_path = data / 'backend-template.json'
    altered = json.loads(template_path.read_text()); altered['backends']['eda']['timeout_seconds'] = 1
    template_path.write_text(json.dumps(altered))
    with pytest.raises(ValueError, match='state/template pair'):
        runtime.checked_state(data)


@pytest.mark.parametrize('selection', ['environment', 'cli'])
def test_non_altium_backend_selection_is_refused(tmp_path, selection):
    template, _ = make_template(tmp_path)
    if selection == 'environment':
        template['backends']['eda']['env']['EDA_AGENT_BACKEND'] = 'kicad'
    else:
        template['backends']['eda']['args'] += ['--backend', 'easyeda']
    with pytest.raises(ValueError, match='Altium backend'):
        runtime.validate_template(template)


@pytest.mark.parametrize('record', [{'schema': 1, 'workspace': 'relative'}, {'schema': 2, 'workspace': 'C:/wrong'}])
def test_malformed_persisted_workspace_is_not_normalized_into_validity(tmp_path, record):
    template, _ = make_template(tmp_path)
    native = Path(template['backends']['eda']['env']['ELECTRONICS_AI_RUNTIME']); native.mkdir()
    (native / 'workspace-identity.json').write_text(json.dumps(record))
    with pytest.raises(ValueError, match='persisted native workspace identity'):
        runtime.validate_template(template)


def test_configuration_backup_retains_recoverable_previous_pair(tmp_path):
    data, template = configured(tmp_path)
    state, _ = runtime.checked_state(data)
    before = {name: (data / name).read_bytes() for name in ('state.json', 'backend-template.json')}
    template['backends']['eda']['timeout_seconds'] = 456
    runtime.persist(data, state, template)
    backup = next((data / 'backups').iterdir())
    for name, value in before.items():
        assert (backup / name).read_bytes() == value
    assert runtime.checked_state(data)[1]['backends']['eda']['timeout_seconds'] == 456


def test_data_root_is_stable_and_explicit_paths_are_absolute(tmp_path, monkeypatch):
    monkeypatch.delenv('ELECTRONICS_AI_PLUGIN_DATA', raising=False)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex'))
    assert runtime.data_directory() == tmp_path / 'codex/plugins/data/mcp-electronics-ai'
    monkeypatch.setenv('ELECTRONICS_AI_PLUGIN_DATA', str(tmp_path / 'override'))
    assert runtime.data_directory() == tmp_path / 'override'
    assert runtime.data_directory(tmp_path / 'explicit') == tmp_path / 'explicit'
    with pytest.raises(ValueError, match='absolute'):
        runtime.data_directory('relative')


def test_doctor_is_readonly_without_native_or_subprocess_when_probe_disabled(tmp_path, monkeypatch):
    data, _ = configured(tmp_path)
    root = make_root(tmp_path / 'new package')
    before = {p.relative_to(data): p.read_bytes() for p in data.rglob('*') if p.is_file()}
    monkeypatch.setattr(runtime.subprocess, 'run', lambda *a, **k: pytest.fail('Unexpected subprocess'))
    result = runtime.doctor(data, root, probe=False)
    assert result['configured'] and result['cloud_cache_allowed']
    assert not result['native_editor_probed']
    assert before == {p.relative_to(data): p.read_bytes() for p in data.rglob('*') if p.is_file()}


def test_actual_python_dependency_probe_does_not_load_native_suite():
    info = runtime.probe_python(Path(sys.executable))
    assert info['python'][:2] == [3, 12]
    assert {'mcp', 'jsonschema', 'pydantic', 'psutil', 'Pillow', 'networkx', 'numpy', 'olefile'} <= set(info['packages'])


def test_normal_serve_only_starts_current_gateway_without_installing(tmp_path, monkeypatch):
    data, _ = configured(tmp_path)
    package = make_root(tmp_path / 'new package')
    calls = []
    def execute(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(runtime.subprocess, 'run', execute)
    assert runtime.main(['--data-dir', str(data), '--plugin-root', str(package), 'serve']) == 0
    assert len(calls) == 1
    command, options = calls[0]
    assert Path(command[0]).samefile(sys.executable)
    assert command[1:4] == ['-B', '-u', str(package / 'suite_stdio.py')]
    assert command[4:7] == ['--toolset', 'minimal', '--backend-config']
    assert options['cwd'] == package
    assert options['env']['ELECTRONICS_AI_CLOUD_CACHE'] == runtime.checked_state(data)[0]['cloud_cache']
    assert not (tmp_path / 'native-runtime').exists()


def test_bootstrap_preflights_workspace_before_installing_dependencies(tmp_path, monkeypatch):
    altium = tmp_path / 'X2.EXE'; altium.touch()
    native = tmp_path / 'runtime'; native.mkdir()
    (native / 'workspace-identity.json').write_text(json.dumps({'schema': 1, 'workspace': 'relative'}))
    args = SimpleNamespace(altium_exe=altium, runtime_dir=native, workspace=None, library_dir=[])
    monkeypatch.setattr(runtime.sys, 'platform', 'win32')
    monkeypatch.setattr(runtime.subprocess, 'run', lambda *a, **k: pytest.fail('Install ran before preflight'))
    data = tmp_path / 'private'
    with pytest.raises(ValueError, match='workspace'):
        runtime.bootstrap(args, data, ROOT)
    assert not data.exists()


def fake_install(monkeypatch):
    calls = []
    def execute(command, **kwargs):
        calls.append(command)
        if command[1:3] == ['-m', 'venv']:
            python = Path(command[3]) / 'Scripts/python.exe'
            python.parent.mkdir(parents=True); python.touch()
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(runtime.sys, 'platform', 'win32')
    monkeypatch.setattr(runtime.subprocess, 'run', execute)
    monkeypatch.setattr(runtime, 'probe_python', lambda *a: {'python': [3, 12, 0]})
    return calls


def test_bootstrap_rerun_preserves_policy_and_uses_new_independent_environment(tmp_path, monkeypatch):
    altium = tmp_path / 'X2.EXE'; altium.touch()
    old = tmp_path / 'oldlibs'; old.mkdir()
    extra = tmp_path / 'newlibs'; extra.mkdir()
    data = tmp_path / 'private'; data.mkdir()
    initial = {'allowed_paths': [str(old)], 'logging': {'level': 'warn'}}
    (data / 'libraries.json').write_text(json.dumps(initial))
    args = SimpleNamespace(altium_exe=altium, runtime_dir=tmp_path / 'runtime', workspace=tmp_path / 'workspace', library_dir=[extra])
    calls = fake_install(monkeypatch)
    assert runtime.bootstrap(args, data, ROOT)['ready']
    first_state, template = runtime.checked_state(data)
    template['backends']['eda'].update(disabled_tools=['pcb_delete'], timeout_seconds=321)
    runtime.persist(data, first_state, template)
    args.runtime_dir = args.workspace = None
    assert runtime.bootstrap(args, data, ROOT)['ready']
    second_state, updated = runtime.checked_state(data)
    assert second_state['python'] != first_state['python']
    assert Path(first_state['python']).is_file()
    assert updated['backends']['eda']['disabled_tools'] == ['pcb_delete']
    assert updated['backends']['eda']['timeout_seconds'] == 321
    assert updated['backends']['eda']['env']['ELECTRONICS_AI_RUNTIME'] == str(tmp_path / 'runtime')
    current = json.loads((data / 'libraries.json').read_text())
    assert current['allowed_paths'] == [str(old), str(extra), str(data / 'cloud-libraries')]
    assert current['logging'] == initial['logging']
    assert any(json.loads(p.read_text()) == initial for p in (data / 'backups').rglob('libraries.json'))
    assert len([c for c in calls if c[1:3] == ['-m', 'venv']]) == 2
    assert all('-e' not in c for c in calls)
    assert updated['backends']['libraries']['args'] == [str(ROOT / 'suite_libraries_native.py'), str(data / 'libraries.json')]


def test_failed_bootstrap_probe_preserves_committed_configuration(tmp_path, monkeypatch):
    data, _ = configured(tmp_path)
    altium = tmp_path / 'X2.EXE'
    before = {name: (data / name).read_bytes() for name in ('state.json', 'backend-template.json')}
    fake_install(monkeypatch)
    def fail(*a): raise ValueError('probe failed')
    monkeypatch.setattr(runtime, 'probe_python', fail)
    args = SimpleNamespace(altium_exe=altium, runtime_dir=None, workspace=None, library_dir=[])
    with pytest.raises(ValueError, match='probe failed'):
        runtime.bootstrap(args, data, ROOT)
    assert before == {name: (data / name).read_bytes() for name in before}


def test_native_manifest_requires_exact_current_worker_contract(tmp_path):
    template, _ = make_template(tmp_path)
    assert runtime.validate_template(template)['allowed_paths']
    template['backends']['libraries']['args'] = [template['backends']['libraries']['args'][1]]
    with pytest.raises(ValueError, match='configuration-file argument'):
        runtime.validate_template(template)
    template['backends']['legacy'] = dict(template['backends']['eda'])
    with pytest.raises(ValueError, match='exactly eda and libraries'):
        runtime.validate_template(template)


@pytest.mark.parametrize('command', ['adopt', 'detach'])
def test_removed_setup_commands_are_not_accepted(command):
    with pytest.raises(SystemExit) as result:
        runtime.main([command])
    assert result.value.code == 2


def launcher_parameters(root, data):
    return StdioServerParameters(command='powershell.exe', args=['-NoLogo', '-NoProfile', '-NonInteractive',
        '-ExecutionPolicy', 'Bypass', '-File', str(root / 'scripts/launch_plugin.ps1'), '-DataDir', str(data)],
        env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}, cwd=str(data.parent))


@pytest.mark.skipif(sys.platform != 'win32', reason='Real Windows PowerShell launcher')
def test_unconfigured_launcher_status_needs_no_python_and_preserves_unicode(tmp_path):
    async def check():
        data = tmp_path / 'not configured'
        async with stdio_client(launcher_parameters(ROOT, data)) as streams:
            async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=20)) as client:
                info = await client.initialize()
                assert info.serverInfo.name == 'mcp-electronics-ai'
                listed = await client.list_tools()
                assert [tool.name for tool in listed.tools] == ['suite_status']
                status = await client.call_tool('suite_status', {})
                assert status.structuredContent['setup_required'] is True
                assert status.structuredContent['setup_skill'] == 'electronics-ai-setup'
                assert status.structuredContent['configured'] is False
                assert not status.structuredContent['native_editor_probed']
                name = 'شماتیک_日本語'
                invalid = await client.call_tool(name, {})
                assert invalid.isError and name in invalid.content[0].text
        assert not data.exists()
    asyncio.run(check())


@pytest.mark.skipif(sys.platform != 'win32', reason='Real Windows PowerShell launcher')
def test_configured_launcher_relays_unicode_stderr_and_repeated_launch(tmp_path):
    data, _ = configured(tmp_path)
    package = make_root(tmp_path / 'installed package')
    (package / 'scripts').mkdir()
    for name in ('plugin_runtime.py', 'launch_plugin.ps1'):
        shutil.copyfile(ROOT / 'scripts' / name, package / 'scripts' / name)
    (package / 'suite_stdio.py').write_text('''import asyncio,json,sys
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
server=Server("mcp-electronics-ai")
@server.list_tools()
async def tools():
 return [types.Tool(name="echo",inputSchema={"type":"object","properties":{"text":{"type":"string"}}})]
@server.call_tool()
async def call(name,arguments):
 print("relay diagnostic",file=sys.stderr,flush=True)
 return types.CallToolResult(content=[types.TextContent(type="text",text=arguments["text"])],structuredContent={"text":arguments["text"]})
async def main():
 async with stdio_server() as streams: await server.run(*streams,server.create_initialization_options())
asyncio.run(main())
''', encoding='utf-8')
    async def check():
        for index in range(2):
            with (tmp_path / f'child-{index}.log').open('w', encoding='utf-8') as log:
                async with stdio_client(launcher_parameters(package, data), errlog=log) as streams:
                    async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=30)) as client:
                        await client.initialize()
                        value = 'شماتیک، مقاومت Ω 日本語'
                        response = await client.call_tool('echo', {'text': value})
                        assert response.structuredContent == {'text': value}
            assert 'relay diagnostic' in (tmp_path / f'child-{index}.log').read_text()
        assert len(list((data / 'launch-manifests').glob('*.json'))) == 1
        assert not (tmp_path / 'native-runtime').exists()
    asyncio.run(check())
