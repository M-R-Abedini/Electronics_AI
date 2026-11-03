"""Configure current Electronics AI clients with atomic, backed-up writes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tomllib
import uuid

SERVER_NAME = 'mcp-electronics-ai'
CLIENTS = ('codex', 'claude-desktop', 'claude-code', 'cursor', 'windsurf', 'vscode')


def client_config_paths(home=None, appdata=None):
    """Known Windows user-scope files; explicit --config overrides profile variants."""
    explicit_home = home is not None
    home = Path(home or Path.home())
    roaming = Path(appdata or os.environ.get('APPDATA', home / 'AppData/Roaming'))
    devin = roaming / 'devin/mcp_config.json'
    return {
        'codex': (Path(os.environ['CODEX_HOME']) / 'config.toml'
                  if not explicit_home and os.environ.get('CODEX_HOME') else home / '.codex/config.toml'),
        'claude-desktop': roaming / 'Claude/claude_desktop_config.json',
        'claude-code': home / '.claude.json',
        'cursor': home / '.cursor/mcp.json',
        'windsurf': devin,
        'vscode': roaming / 'Code/User/mcp.json',
    }


def launcher_definition(destination, python, data_directory):
    """Use private runtime state; never bake a developer's backend manifest into clients."""
    root, python, data = (Path(p).resolve() for p in (destination, python, data_directory))
    if not (root / 'scripts/plugin_runtime.py').is_file() or not python.is_file():
        raise ValueError('Install the suite launcher and Python before configuring clients')
    return {'command': str(python), 'args': ['-B', str(root / 'scripts/plugin_runtime.py'),
            '--data-dir', str(data), '--plugin-root', str(root), 'serve'],
            'env': {'ELECTRONICS_AI_PLUGIN_DATA': str(data), 'PYTHONDONTWRITEBYTECODE': '1'}}


def render_client(text, client, definition):
    """Preserve unrelated settings. Refuse JSONC rather than silently dropping comments."""
    if client not in CLIENTS:
        raise ValueError('Unknown AI client: ' + client)
    if client == 'codex':
        original = tomllib.loads(text)
        servers = original.get('mcp_servers', {})
        if not isinstance(servers, dict):
            raise ValueError('mcp_servers must be a table')
        # A plugin already registers this endpoint. Do not enable a duplicate.
        plugins = original.get('plugins', {})
        if not isinstance(plugins, dict):
            raise ValueError('plugins must be a table')
        for key, value in plugins.items():
            if key.startswith('mcp-electronics-ai@') and isinstance(value, dict) and value.get('enabled'):
                return text, False
        expected = json.loads(json.dumps(original))
        new = dict(definition, startup_timeout_sec=60, tool_timeout_sec=900, enabled=True)
        expected.setdefault('mcp_servers', {})[SERVER_NAME] = new
        pieces, cursor = [], 0
        headers = list(_section_headers(text))
        for i, (start, path) in enumerate(headers):
            if path[:2] == ('mcp_servers', SERVER_NAME):
                pieces.append(text[cursor:start])
                cursor = headers[i + 1][0] if i + 1 < len(headers) else len(text)
        pieces.append(text[cursor:])
        rendered = ''.join(pieces).rstrip() + '\n\n[mcp_servers.mcp-electronics-ai]\n'
        for key, value in new.items():
            if key != 'env':
                rendered += key + ' = ' + json.dumps(value) + '\n'
        rendered += '\n[mcp_servers.mcp-electronics-ai.env]\n'
        rendered += ''.join(key + ' = ' + json.dumps(value) + '\n' for key, value in new['env'].items())
        if tomllib.loads(rendered) != expected:
            raise ValueError('Cannot safely update inline TOML; use a separate server table')
        return rendered, rendered != text
    try:
        original = json.loads(text or '{}')
    except json.JSONDecodeError as error:
        raise ValueError('Client file is not strict JSON (possibly JSONC). Use the client UI or export strict JSON; no file changed') from error
    if not isinstance(original, dict):
        raise ValueError('Client configuration must be an object')
    key = 'servers' if client == 'vscode' else 'mcpServers'
    servers = original.setdefault(key, {})
    if not isinstance(servers, dict):
        raise ValueError(key + ' must be an object')
    new = dict(definition)
    if client == 'vscode':
        new['type'] = 'stdio'
    if servers.get(SERVER_NAME) == new:
        return text, False
    servers[SERVER_NAME] = new
    return json.dumps(original, ensure_ascii=False, indent=2) + '\n', True


def configure_clients(client, destination, python, data_directory, *, apply=False,
                      config=None, home=None, appdata=None):
    """Preflight all clients, then atomically write each with byte-exact backups.

    --all means all supported clients, not automatic installation of those apps.
    Any preflight error leaves every client untouched. Publication failure restores
    previously published files only when they still match this operation's bytes.
    """
    selected = CLIENTS if client == 'all' else (client,)
    if config is not None and len(selected) != 1:
        raise ValueError('--config requires one specific --client')
    definition = launcher_definition(destination, python, data_directory)
    paths = client_config_paths(home, appdata)
    prepared = []
    for name in selected:
        if name not in paths:
            raise ValueError('Unknown AI client: ' + name)
        raw_path = Path(config).absolute() if config else paths[name].absolute()
        if raw_path.is_symlink():
            raise ValueError('Refusing a linked client configuration: ' + str(raw_path))
        path = raw_path.resolve()
        old = path.read_bytes() if path.exists() else None
        rendered, changed = render_client(old.decode('utf-8-sig') if old else '', name, definition)
        prepared.append((name, path, old, rendered.encode('utf-8'), changed))
    summary = {'applied': False, 'clients': [{'client': n, 'config': str(p), 'changed': c}
                for n, p, _, _, c in prepared], 'reconnect_required': True}
    if not apply:
        return summary
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    backup = Path(data_directory) / 'client-config-backups' / stamp
    backup.mkdir(parents=True, exist_ok=False)
    published = []
    try:
        for name, path, old, new, changed in prepared:
            if not changed:
                continue
            current = path.read_bytes() if path.exists() else None
            if current != old:
                raise RuntimeError('Client config changed during preparation; retry: ' + str(path))
            if old is not None:
                (backup / (name + path.suffix)).write_bytes(old)
            _atomic_write(path, new)
            published.append((path, old, new))
    except BaseException:
        for path, old, new in reversed(published):
            if path.exists() and path.read_bytes() == new:
                if old is None:
                    path.unlink()
                else:
                    _atomic_write(path, old)
        raise
    summary.update(applied=True, backup=str(backup))
    (backup / 'manifest.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    return summary


def _section_headers(text):
    """Find real table headers, ignoring header-like text in multiline strings."""
    position = 0
    multiline = None
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if multiline is None and stripped.startswith('['):
            try:
                value = tomllib.loads(stripped)
                path = []
                while isinstance(value, dict) and len(value) == 1:
                    key, value = next(iter(value.items()))
                    path.append(key)
                yield position, tuple(path)
            except tomllib.TOMLDecodeError:
                pass
        i = 0
        quote = None
        while i < len(line):
            if multiline:
                if multiline == '"""' and line[i] == '\\':
                    i += 2
                    continue
                if line.startswith(multiline, i):
                    i += 3
                    multiline = None
                else:
                    i += 1
            elif quote:
                if quote == '"' and line[i] == '\\':
                    i += 2
                elif line[i] == quote:
                    quote = None
                    i += 1
                else:
                    i += 1
            elif line[i] == '#':
                break
            elif line.startswith('"""', i) or line.startswith("'''", i):
                multiline = line[i:i + 3]
                i += 3
            elif line[i] in ('"', "'"):
                quote = line[i]
                i += 1
            else:
                i += 1
        position += len(line)




def _atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)




def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--client', choices=(*CLIENTS, 'all'))
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--destination', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--python', type=Path)
    parser.add_argument('--apply', action='store_true', help='otherwise print a read-only configuration plan')
    args = parser.parse_args()
    destination = args.destination.resolve()
    python = args.python or destination / '.venv/Scripts/python.exe'
    if not args.client:
        parser.error('--client is required')
    data = args.data_dir or Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'plugins/data/mcp-electronics-ai'
    output = configure_clients(args.client, destination, python, data.resolve(),
                               apply=args.apply, config=args.config)
    print(json.dumps(output, indent=2))


if __name__ == '__main__':
    main()
