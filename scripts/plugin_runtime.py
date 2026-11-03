"""Explicit plugin setup and read-only diagnosis; ordinary serving never installs.

Private configuration persists outside the plugin cache. Each launch resolves
the current package's workers, so installing an updated plugin updates its code.
SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
sys.dont_write_bytecode = True
import uuid
import tempfile

ROOT = Path(__file__).resolve().parents[1]
STATE_SCHEMA = 1


def install_runtime(python, root):
    """Install a regular wheel, never an editable dependency on a disposable cache."""
    root = Path(root).resolve()
    lines = []
    for line in (root / 'requirements-runtime.txt').read_text(encoding='utf-8').splitlines():
        if line.strip() == '-e ./eda-agent':
            lines.append((root / 'eda-agent').as_uri())
        elif line.startswith('-c '):
            # pip parses option lines with POSIX shlex even on Windows.
            lines.append('-c "' + (root / line[3:].strip()).as_posix() + '"')
        else:
            lines.append(line)
    with tempfile.TemporaryDirectory(prefix='electronics-ai-runtime-install-') as scratch:
        requirements = Path(scratch) / 'requirements.txt'
        requirements.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(requirements)],
                       cwd=root, check=True, stdout=sys.stderr)


def data_directory(explicit=None):
    if explicit:
        path = Path(explicit)
    elif os.environ.get('ELECTRONICS_AI_PLUGIN_DATA'):
        path = Path(os.environ['ELECTRONICS_AI_PLUGIN_DATA'])
    else:
        codex = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
        path = codex / 'plugins/data/mcp-electronics-ai'
    if not path.is_absolute():
        raise ValueError('Plugin data directory must be absolute')
    return path.resolve()


def read_object(path):
    value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError(f'Expected a JSON object: {path}')
    return value


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_bytes(json.dumps(value, sort_keys=True, indent=2).encode('utf-8'))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def existing_file(value, name):
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise ValueError(f'{name} must be an existing absolute file path')
    return path.resolve()


def probe_python(python):
    # Import only dependencies; never import native suite configuration here.
    script = ('import sys,json,importlib.metadata as m; '
              'import mcp,jsonschema,pydantic,psutil,PIL,networkx,numpy,olefile; '
              'exec("import win32api,comtypes" if sys.platform == "win32" else ""); '
              'print(json.dumps({"python":list(sys.version_info[:3]),'
              '"packages":{n:m.version(n) for n in ["mcp","jsonschema","pydantic","psutil","Pillow","networkx","numpy","olefile"]}}))')
    completed = subprocess.run([str(python), '-I', '-B', '-c', script], capture_output=True,
                               text=True, timeout=45, check=False)
    if completed.returncode:
        raise ValueError('Selected Python lacks required runtime dependencies; run bootstrap or install requirements-runtime.txt')
    info = json.loads(completed.stdout)
    if info['python'][:2] != [3, 12]:
        raise ValueError('The tested plugin runtime requires Python 3.12')
    return info


def validate_template(template):
    backends = template.get('backends')
    if not isinstance(backends, dict) or set(backends) != {'eda', 'libraries'}:
        raise ValueError('Electronics AI requires exactly eda and libraries backends')
    allowed = {'command', 'args', 'env', 'cwd', 'timeout_seconds',
               'startup_timeout_seconds', 'enabled', 'enabled_tools', 'disabled_tools'}
    for name, spec in backends.items():
        if not isinstance(spec, dict) or set(spec) - allowed:
            raise ValueError(f'Invalid or unsupported backend settings: {name}')
        existing_file(spec.get('command', ''), name + ' executable')
        if not isinstance(spec.get('args'), list) or not all(isinstance(x, str) for x in spec['args']):
            raise ValueError(f'{name} args must be a list of strings')
        if not isinstance(spec.get('env', {}), dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in spec.get('env', {}).items()):
            raise ValueError(f'{name} environment must map strings to strings')
        if 'enabled' in spec and not isinstance(spec['enabled'], bool):
            raise ValueError(f'{name} enabled must be boolean')
        for key in ('enabled_tools', 'disabled_tools'):
            if key == 'enabled_tools' and spec.get(key) is None:
                continue
            if key in spec and (not isinstance(spec[key], list) or not all(isinstance(x, str) for x in spec[key])):
                raise ValueError(f'{name} {key} must be tool names')
        for key in ('timeout_seconds', 'startup_timeout_seconds'):
            if key in spec and (isinstance(spec[key], bool) or not isinstance(spec[key], (float, int)) or not 0 < spec[key] <= 86400):
                raise ValueError(f'{name} invalid {key}')
    for name, filenames in (('eda', {'eda_stdio.py'}),):
        args = backends[name]['args']
        if not args or Path(args[0]).name not in filenames:
            raise ValueError(f'{name}: the coordinated eda_stdio.py wrapper is required')
        if backends[name].get('env', {}).get('EDA_AGENT_BACKEND', 'altium') != 'altium':
            raise ValueError('This plugin requires the Altium backend')
        for index, argument in enumerate(args):
            if argument.startswith('--backend=') and argument != '--backend=altium':
                raise ValueError('This plugin requires the Altium backend')
            if argument == '--backend' and (index + 1 == len(args) or args[index + 1] != 'altium'):
                raise ValueError('This plugin requires the Altium backend')
    env = backends['eda'].get('env', {})
    for key in ('ELECTRONICS_AI_RUNTIME', 'EDA_AGENT_WORKSPACE'):
        if not isinstance(env.get(key), str) or not Path(env[key]).is_absolute():
            raise ValueError(f'The native worker needs an explicit absolute {key}')
    library = backends['libraries']
    configuration = existing_file(library_configuration_path(library), 'Library configuration')
    settings = read_object(configuration)
    if not isinstance(settings.get('allowed_paths'), list) or not all(isinstance(x, str) and Path(x).is_absolute() for x in settings['allowed_paths']):
        raise ValueError('Library allowed_paths must contain absolute path strings')
    validate_workspace(Path(env['ELECTRONICS_AI_RUNTIME']), Path(env['EDA_AGENT_WORKSPACE']))
    return settings


def library_configuration_path(spec):
    """Require the native Python library worker and its configuration."""
    args = spec.get('args', [])
    if len(args) == 2 and Path(args[0]).name == 'suite_libraries_native.py':
        return args[1]
    raise ValueError('The native library service needs its configuration-file argument')


def validate_workspace(runtime, workspace):
    if not runtime.is_absolute() or not workspace.is_absolute():
        raise ValueError('Native runtime and workspace must be absolute')
    identity = runtime / 'workspace-identity.json'
    if identity.is_file():
        recorded = read_object(identity)
        raw = recorded.get('workspace')
        if recorded.get('schema') != 1 or not isinstance(raw, str) or not Path(raw).is_absolute():
            raise ValueError('Invalid persisted native workspace identity; no configuration changed')
        if os.path.normcase(str(Path(raw).resolve())) != os.path.normcase(str(workspace.resolve())):
            raise ValueError('Persisted native workspace identity conflicts with the selected manifest')


def remap_template(template, plugin_root):
    """Upgrade-safe resolution: runtime settings persist; source paths do not."""
    value = json.loads(json.dumps(template))
    root = Path(plugin_root).resolve()
    for name, script in (('eda', root / 'eda_stdio.py'),):
        if not script.is_file():
            raise ValueError(f'Plugin package is missing its {name} wrapper')
        worker = value['backends'][name]
        worker['args'][0] = str(script)
        worker['cwd'] = str(root)
        # Do not inherit a KiCad/EasyEDA selection from an unrelated client.
        worker.setdefault('env', {}).setdefault('EDA_AGENT_BACKEND', 'altium')
        if name == 'eda':
            args = worker['args']
            for index, argument in enumerate(args):
                if argument == '--toolset' and index + 1 < len(args):
                    args[index + 1] = 'full'
                    break
                if argument.startswith('--toolset='):
                    args[index] = '--toolset=full'
                    break
            else:
                args += ['--toolset', 'full']
            if '--no-dashboard' not in args:
                args.append('--no-dashboard')
    library = value['backends']['libraries']
    configuration = library_configuration_path(library)
    library['command'] = value['backends']['eda']['command']
    library['args'] = [str(root / 'suite_libraries_native.py'), configuration]
    # The gateway constructs NativeLibraries in process; runtime configuration
    # remains private while the selected package provides all executable code.
    return value


def load_state(data):
    state = read_object(data / 'state.json')
    if state.get('schema') != STATE_SCHEMA:
        raise ValueError('Unsupported plugin state schema; run setup again')
    existing_file(state.get('python', ''), 'Python runtime')
    cloud = Path(state.get('cloud_cache', ''))
    if not cloud.is_absolute():
        raise ValueError('Cloud cache must be absolute')
    template = read_object(data / 'backend-template.json')
    return state, template


def persist(data, state, template):
    data.mkdir(parents=True, exist_ok=True)
    if (data / 'state.json').exists() or (data / 'backend-template.json').exists():
        backup = data / 'backups' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
        backup.mkdir(parents=True)
        for name in ('state.json', 'backend-template.json'):
            if (data / name).exists():
                shutil.copy2(data / name, backup / name)
    # Store the pair in state last; on interruption state hash prevents use of
    # an uncommitted template rather than guessing or launching another engine.
    state['backend_template_sha256'] = hashlib.sha256(json.dumps(template, sort_keys=True).encode()).hexdigest()
    atomic_json(data / 'backend-template.json', template)
    atomic_json(data / 'state.json', state)


def checked_state(data):
    state, template = load_state(data)
    digest = hashlib.sha256(json.dumps(template, sort_keys=True).encode()).hexdigest()
    if state.get('backend_template_sha256') != digest:
        raise ValueError('Plugin state/template pair is incomplete; restore its backup or run setup')
    return state, template


def doctor(data, plugin_root=ROOT, probe=True):
    output = {'configured': False, 'ready': False, 'data_directory': str(data),
              'platform': sys.platform, 'native_editor_probed': False,
              'altium_is_separate_prerequisite': True, 'issues': []}
    try:
        state, template = checked_state(data)
        output['configured'] = True
        library_settings = validate_template(template)
        resolved = remap_template(template, plugin_root)
        output['backends'] = {name: {'enabled': spec.get('enabled', True),
                                     'executable_exists': Path(spec['command']).is_file()}
                              for name, spec in resolved['backends'].items()}
        output['altium_executable_exists'] = Path(state['altium_exe']).is_file()
        if not output['altium_executable_exists']:
            output['issues'].append('The configured Altium executable is missing')
        cloud = Path(state['cloud_cache'])
        output['cloud_cache_allowed'] = any(cloud.resolve().is_relative_to(Path(path).resolve()) for path in library_settings['allowed_paths'])
        if not output['cloud_cache_allowed']:
            output['issues'].append('Cloud cache is outside the library worker allowlist')
        if probe:
            output['python_runtime'] = probe_python(state['python'])
        if sys.platform != 'win32':
            output['issues'].append('Native Altium automation requires Windows')
        output['ready'] = not output['issues']
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        output['issues'].append(str(error))
    return output


def probe_backend_initialization(manifest, *, timeout_seconds=30, plugin_root=ROOT):
    """Initialize/list the EDA MCP only. Never call a native/editor tool."""
    import asyncio
    # Direct invocation places scripts/, not the plugin root, on sys.path.
    # The launcher is also supported from an unrelated working directory.
    sys.path.insert(0, str(Path(plugin_root).resolve()))
    from suite_stdio import Worker, load_specs, worker_diagnostic
    spec = next((s for s in load_specs(Path(manifest)) if s.name == 'eda'), None)
    if spec is None or not spec.enabled:
        return {'initialized': False, 'status': 'disabled', 'native_editor_probed': False}
    async def run():
        worker = Worker(spec)
        try:
            async with asyncio.timeout(timeout_seconds):
                await worker.request('_capabilities')
                catalog = await worker.request('list_tools')
            return {'initialized': True, 'status': 'ready', 'tool_count': len(catalog.tools),
                    'native_editor_probed': False}
        except Exception as error:
            diagnostic = getattr(error, 'diagnostic', None) or worker.failure_diagnostic
            if diagnostic is None:
                diagnostic = worker_diagnostic(error, worker.phase, dict(os.environ, **spec.env))
            return {'initialized': False, 'status': 'failed', 'diagnostic': diagnostic,
                    'native_editor_probed': False, 'automatic_retry': False}
        finally:
            # This actor only initializes/lists tools. Cancelling its bounded
            # diagnostic cannot interrupt/replay any native design operation.
            if worker.task is not None and not worker.task.done():
                worker.task.cancel()
                await asyncio.gather(worker.task, return_exceptions=True)
            await worker.close()
    return asyncio.run(run())


def launch_configuration(data, plugin_root=ROOT):
    state, template = checked_state(data)
    validate_template(template)
    resolved = remap_template(template, plugin_root)
    encoded = json.dumps(resolved, sort_keys=True, indent=2).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    manifest = data / 'launch-manifests' / (digest + '.json')
    if manifest.exists():
        if manifest.read_bytes() != encoded:
            raise ValueError('A generated launch manifest was changed; inspect it before reconnecting')
    else:
        atomic_json(manifest, resolved)
    return state, manifest


def bootstrap(args, data, plugin_root=ROOT):
    if sys.platform != 'win32' or sys.version_info[:2] != (3, 12):
        raise ValueError('Bootstrap must run on Windows using Python 3.12')
    altium = existing_file(args.altium_exe, 'Altium executable')
    root = Path(plugin_root).resolve()
    previous = None
    if (data / 'state.json').exists():
        _, previous = checked_state(data)
        validate_template(previous)
    prior_env = previous['backends']['eda']['env'] if previous else {}
    for relative in ('requirements-runtime.txt', 'constraints-windows-py312.txt',
                     'eda-agent/pyproject.toml', 'eda_stdio.py', 'suite_stdio.py',
                     'suite_libraries_native.py'):
        existing_file(root / relative, 'Packaged ' + relative)
    runtime = Path(args.runtime_dir or prior_env.get('ELECTRONICS_AI_RUNTIME') or
                   Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'ElectronicsAI')
    identity = runtime / 'workspace-identity.json'
    if args.workspace:
        workspace = Path(args.workspace)
    elif prior_env.get('EDA_AGENT_WORKSPACE'):
        workspace = Path(prior_env['EDA_AGENT_WORKSPACE'])
    elif identity.is_file():
        raw = read_object(identity).get('workspace')
        if not isinstance(raw, str):
            raise ValueError('Invalid persisted native workspace identity')
        workspace = Path(raw)
    else:
        workspace = runtime / 'workspace'
    validate_workspace(runtime, workspace)
    runtime, workspace = runtime.resolve(), workspace.resolve()
    if previous:
        for key, selected in (('ELECTRONICS_AI_RUNTIME', runtime), ('EDA_AGENT_WORKSPACE', workspace)):
            if os.path.normcase(str(selected)) != os.path.normcase(str(Path(prior_env[key]).resolve())):
                raise ValueError('Bootstrap cannot replace the existing native workspace identity')
    cloud = data / 'cloud-libraries'
    roots = [str(Path(path).resolve()) for path in args.library_dir]
    if not all(Path(path).is_dir() for path in roots):
        raise ValueError('Library directories must already exist')
    library_config = data / 'libraries.json'
    library_settings = (read_object(library_config) if library_config.is_file() else
                        read_object(library_configuration_path(previous['backends']['libraries'])) if previous else
                        {'allowed_paths': []})
    if not isinstance(library_settings.get('allowed_paths'), list) or not all(isinstance(path, str) and Path(path).is_absolute() for path in library_settings['allowed_paths']):
        raise ValueError('Existing library configuration has an invalid allowlist')
    existing_roots = library_settings['allowed_paths']
    library_settings['allowed_paths'] = list(dict.fromkeys(existing_roots + roots + [str(cloud)]))
    identifier = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    environment = data / 'runtimes' / identifier / 'python-3.12'
    python = environment / 'Scripts/python.exe'
    data.mkdir(parents=True, exist_ok=True)
    if not python.exists():
        subprocess.run([sys.executable, '-m', 'venv', str(environment)], check=True)
    install_runtime(python, root)
    probe_python(python)
    cloud.mkdir(parents=True, exist_ok=True)
    if library_config.exists():
        saved = data / 'backups' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]) / library_config.name
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(library_config, saved)
    atomic_json(library_config, library_settings)
    common = {'ALTIUM_EXE': str(altium), 'ELECTRONICS_AI_RUNTIME': str(runtime),
              'EDA_AGENT_WORKSPACE': str(workspace), 'EDA_AGENT_NO_DASHBOARD': '1',
              'EDA_AGENT_BACKEND': 'altium'}
    template = {'backends': {
        'eda': {'command': str(python), 'args': [str(root / 'eda_stdio.py'), '--toolset', 'full', '--no-dashboard'],
                'env': common, 'timeout_seconds': 900},
        'libraries': {'command': str(python), 'args': [str(root / 'suite_libraries_native.py'), str(library_config)], 'timeout_seconds': 180},
    }}
    if previous:
        for name, worker in template['backends'].items():
            old = previous['backends'][name]
            for key in ('enabled', 'enabled_tools', 'disabled_tools', 'timeout_seconds', 'startup_timeout_seconds'):
                if key in old:
                    worker[key] = old[key]
            if name == 'eda':
                worker['env'] = dict(old['env'], ALTIUM_EXE=str(altium))
    validate_template(template)
    state = {'schema': STATE_SCHEMA, 'python': str(python), 'cloud_cache': str(cloud),
             'altium_exe': str(altium), 'mode': 'bootstrapped',
             'configured_at_utc': datetime.now(timezone.utc).isoformat()}
    persist(data, state, template)
    return doctor(data, root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--plugin-root', type=Path, default=ROOT)
    parser.add_argument('--client', choices=('codex', 'claude-desktop', 'claude-code', 'cursor', 'windsurf', 'vscode', 'all'),
                        help='Configure clients after successful setup (otherwise no client files change)')
    sub = parser.add_subparsers(dest='operation', required=True)
    diagnose = sub.add_parser('doctor')
    diagnose.add_argument('--probe-backends', action='store_true',
                          help='Read-only MCP initialize/list-tools probe; no native editor commands')
    fresh = sub.add_parser('bootstrap')
    fresh.add_argument('--altium-exe', type=Path, required=True)
    fresh.add_argument('--library-dir', type=Path, action='append', default=[])
    fresh.add_argument('--runtime-dir', type=Path)
    fresh.add_argument('--workspace', type=Path)
    sub.add_parser('serve')
    clients = sub.add_parser('configure-clients')
    clients.add_argument('--apply', action='store_true')
    clients.add_argument('--config', type=Path)
    args = parser.parse_args(argv)
    data = data_directory(args.data_dir)
    try:
        if args.operation == 'bootstrap':
            output = bootstrap(args, data, args.plugin_root)
        elif args.operation == 'doctor':
            output = doctor(data, args.plugin_root)
            if args.probe_backends and output['ready']:
                _, manifest = launch_configuration(data, args.plugin_root)
                output['backend_initialization'] = {'eda': probe_backend_initialization(manifest, plugin_root=args.plugin_root)}
                output['backend_initialization_probed'] = True
                if not output['backend_initialization']['eda']['initialized']:
                    output['ready'] = False
                    output['issues'].append('EDA backend MCP initialization failed; inspect its diagnostic')
        elif args.operation == 'configure-clients':
            if not args.client:
                raise ValueError('configure-clients requires --client')
            sys.path.insert(0, str(args.plugin_root))
            from configure_suite import configure_clients
            state, _ = checked_state(data)
            output = configure_clients(args.client, args.plugin_root, state['python'], data,
                                       apply=args.apply, config=args.config)
        else:
            state, manifest = launch_configuration(data, args.plugin_root)
            env = dict(os.environ, ELECTRONICS_AI_CLOUD_CACHE=state['cloud_cache'],
                       ELECTRONICS_AI_PLUGIN_DATA=str(data), PYTHONDONTWRITEBYTECODE='1')
            completed = subprocess.run([state['python'], '-B', '-u', str(args.plugin_root / 'suite_stdio.py'),
                                        '--toolset', 'minimal', '--backend-config', str(manifest)],
                                       cwd=args.plugin_root, env=env, check=False)
            return completed.returncode
        if args.client and args.operation == 'bootstrap':
            if output.get('ready', output.get('configured', False)):
                sys.path.insert(0, str(args.plugin_root))
                from configure_suite import configure_clients
                state, _ = checked_state(data)
                output['client_configuration'] = configure_clients(args.client, args.plugin_root, state['python'], data, apply=True)
        print(json.dumps(output, indent=2))
        return 1 if args.operation in {'doctor', 'bootstrap'} and not output['ready'] else 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print('Electronics AI setup: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
