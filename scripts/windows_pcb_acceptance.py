"""Read-only acceptance of an explicitly selected Electronics AI installation.

SPDX-License-Identifier: MIT
No PCB creation, save, placement, rule application or native script snippets.
Run with the configured private Python 3.12 interpreter (state.json: python).
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sys
import uuid


class AcceptanceFailure(RuntimeError):
    pass


def expected_versions(plugin_root):
    root = Path(plugin_root)
    manifest = json.loads((root / 'plugin.json').read_text(encoding='utf-8'))
    if manifest.get('name') != 'mcp-electronics-ai':
        raise ValueError('Select the installed mcp-electronics-ai plugin root')
    main = (root / 'eda-agent/scripts/altium/Main.pas').read_text(encoding='utf-8')
    version = re.search(r"SCRIPT_VERSION\s*=\s*'([^']+)'", main)
    if not version:
        raise ValueError('The selected plugin has no discoverable native script version')
    return manifest['version'], version[1]


def response_data(response):
    data = response.structuredContent
    if isinstance(data, dict):
        return data
    for item in response.content:
        if getattr(item, 'type', None) == 'text':
            try:
                data = json.loads(item.text)
            except (ValueError, TypeError):
                continue
            if isinstance(data, dict):
                return data
    raise AcceptanceFailure('The tool returned no structured JSON object')


def response_error(response):
    return '\n'.join([json.dumps(response.structuredContent, ensure_ascii=False)] + [
        item.text for item in response.content if getattr(item, 'type', None) == 'text'])


async def check_session(client, report, *, plugin_version, native_version, board_path=None):
    """Only this closed set of read-only capabilities can be invoked here."""
    report.update(checks=[], evidence={}, board_mutation_tools_invoked=False,
                  fixture_U12='not_present_in_repo',
                  native_apply_acceptance={'status': 'DEFERRED',
                      'reason': 'This read-only run has no qualified stored placement plan; '
                                'no apply, refusal/replay mutation tests or board edits were attempted.'})

    def require(condition, name, detail=None):
        report['checks'].append({'name': name, 'passed': bool(condition), 'detail': detail})
        if not condition:
            raise AcceptanceFailure(name)

    allowed = {'eda__app_context', 'eda__pcb_get_components',
               'layout__capture_context', 'layout__get_evidence'}

    async def invoke(name, arguments=None, *, expect_error=False):
        if name not in allowed:
            raise AcceptanceFailure('Capability outside the read-only acceptance allowlist')
        result = await client.call_tool('suite_invoke', {'name': name, 'arguments': arguments or {}})
        report['evidence'].setdefault('calls', []).append({
            'name': name, 'arguments': arguments or {}, 'is_error': bool(result.isError)})
        if expect_error:
            return result
        require(not result.isError, name + ': successful MCP response',
                response_error(result) if result.isError else None)
        return response_data(result)

    initialized = await client.initialize()
    report['server'] = {'name': initialized.serverInfo.name, 'version': initialized.serverInfo.version}
    require(initialized.serverInfo.name == 'Electronics AI', 'Installed server identity')
    require(initialized.serverInfo.version == plugin_version, 'Installed package version', plugin_version)
    advertised = {tool.name for tool in (await client.list_tools()).tools}
    require({'suite_catalog', 'suite_invoke', 'suite_status'} <= advertised, 'MCP gateway tools')
    requests = [('eda', 'app_context', {'eda__app_context'})]
    if board_path:
        requests += [('eda', 'pcb_get_components', {'eda__pcb_get_components'}),
                     ('layout', '', {'layout__capture_context', 'layout__get_evidence'})]
    for backend, query, needed in requests:
        result = await client.call_tool('suite_catalog', {
            'backend': backend, 'query': query, 'with_schema': True, 'limit': 100})
        require(not result.isError, 'Discover ' + backend + ' capabilities')
        catalog = response_data(result)
        require(needed <= {item['name'] for item in catalog['tools']},
                'Required ' + backend + ' capability schemas')

    context = await invoke('eda__app_context')
    report['evidence']['app_context'] = context
    require(context.get('backend') == 'altium' and context.get('bridge_answering') is True,
            'Actual native bridge answered')
    require(context.get('script_version_match') is True and
            context.get('running_script_version') == native_version and
            context.get('deployed_script_version') == native_version,
            'Compiled native version matches selected package', native_version)
    identity = context.get('coordination') or {}
    require(identity.get('script_hash_match') is True and
            identity.get('compiled_project_path') == identity.get('expected_project_path'),
            'Compiled content-addressed project matches configured sources')
    report['unsaved_detection_complete'] = context.get('unsaved_detection_complete') is True
    report['buffer_cleanliness_certified'] = False

    if not board_path:
        report['pcb_geometry_acceptance'] = {'status': 'DEFERRED',
            'reason': 'No explicit already-loaded real PCB was selected; no fixture was created.'}
        return

    board = Path(board_path)
    before = hashlib.sha256(board.read_bytes()).hexdigest()
    report['board_path'] = str(board)
    report['board_sha256_before'] = before
    try:
        packages = await invoke('eda__pcb_get_components', {
            'target_board_path': str(board), 'include_package_geometry': True})
        report['evidence']['package_geometry'] = packages
        require(packages.get('units') == 'mm' and isinstance(packages.get('components'), list),
                'Native package geometry reports millimetres and component records')
        require(str(PureWindowsPath(packages.get('resolved_board_path', ''))).casefold() ==
                str(PureWindowsPath(board)).casefold(), 'Package geometry names the requested PCB')
        # Native raw courtyard primitives are evidence, never a fabricated qualified boundary.
        capture = await invoke('layout__capture_context', {'board_path': str(board)})
        report['evidence']['capture_context'] = capture
        context_id = capture.get('context_id')
        require(isinstance(context_id, str) and re.fullmatch('[0-9a-f]{64}', context_id) is not None,
                'Server-created immutable context handle')
        stored = await invoke('layout__get_evidence', {'kind': 'contexts', 'evidence_id': context_id})
        report['evidence']['stored_context'] = stored
        require(stored.get('trusted_live') is True, 'Context came from actual native capture')
        captured_path = stored.get('context', {}).get('board_path', '')
        require(str(PureWindowsPath(captured_path)).casefold() == str(PureWindowsPath(board)).casefold(),
                'Stored context names the requested PCB')
        require(isinstance(stored.get('capture_digest'), str) and
                re.fullmatch('[0-9a-f]{64}', stored['capture_digest']) is not None,
                'Stored context has a source dependency digest')
        wrong = board.with_name('electronics-ai-unloaded-' + uuid.uuid4().hex + '.PcbDoc')
        require(not wrong.exists(), 'Wrong-target probe does not name a real file')
        rejected = await invoke('eda__pcb_get_components', {
            'target_board_path': str(wrong)}, expect_error=True)
        error = response_error(rejected)
        report['evidence']['wrong_target_rejection'] = error
        require(rejected.isError and 'ERR_TARGET_DOCUMENT_MISMATCH' in error,
                'Explicit unloaded PCB rejected without target fallback')
        report['pcb_geometry_acceptance'] = {'status': 'PASS',
            'qualification': 'Read-only capture and target refusal; no placement or courtyard qualification.'}
    finally:
        after = hashlib.sha256(board.read_bytes()).hexdigest()
        report['board_sha256_after'] = after
        require(before == after, 'Saved PCB bytes unchanged during read-only acceptance')


async def run(args, report):
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as error:
        raise ValueError('Run this helper with the configured Python interpreter in data/state.json; '
                         'it supplies the installed MCP dependencies') from error
    root, data = args.plugin_root.resolve(), args.data_dir.resolve()
    plugin_version, native_version = expected_versions(root)
    state = json.loads((data / 'state.json').read_text(encoding='utf-8'))
    python = Path(state['python'])
    if not python.is_file() or not python.is_absolute():
        raise ValueError('Configured private Python interpreter is missing or not absolute')
    report.update(plugin_root=str(root), data_dir=str(data), python=str(python),
                  started_at_utc=datetime.now(timezone.utc).isoformat())
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    environment.pop('PYTHONPATH', None)
    parameters = StdioServerParameters(command=str(python), args=[
        '-B', '-u', str(root / 'scripts/plugin_runtime.py'), '--data-dir', str(data),
        '--plugin-root', str(root), 'serve'], cwd=str(root), env=environment)
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=args.timeout)) as client:
            await check_session(client, report, plugin_version=plugin_version,
                                native_version=native_version, board_path=args.board_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plugin-root', required=True, type=Path,
                        help='Explicit installed plugin directory, not a remembered source checkout')
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--board-path', type=Path,
                        help='Optional existing real .PcbDoc ALREADY LOADED in the sole Altium instance')
    parser.add_argument('--output', required=True, type=Path, help='Private JSON acceptance report')
    parser.add_argument('--timeout', type=float, default=300, help='Per-MCP-request timeout in seconds')
    args = parser.parse_args(argv)
    if sys.platform != 'win32' or sys.version_info[:2] != (3, 12):
        parser.error('Use Windows and the configured Python 3.12 interpreter')
    if not args.plugin_root.is_absolute() or not args.data_dir.is_absolute() or not args.output.is_absolute():
        parser.error('Plugin root, data directory and report output must be absolute paths')
    if not 1 <= args.timeout <= 1800:
        parser.error('Timeout must be between 1 and 1800 seconds')
    if args.board_path and (not args.board_path.is_absolute() or
            args.board_path.suffix.casefold() != '.pcbdoc' or not args.board_path.is_file()):
        parser.error('Board path must be an existing absolute .PcbDoc; this helper will never open/create it')
    if args.output.suffix.casefold() != '.json' or args.output.exists():
        parser.error('Choose a new .json report path; existing files are never overwritten')
    report = {'schema': 1, 'status': 'FAIL', 'read_only': True,
              'scope': 'installation_health_and_optional_real_pcb_capture'}
    try:
        asyncio.run(run(args, report))
        report['status'] = 'PASS'
    except Exception as error:
        report['failure'] = {'type': type(error).__name__, 'message': str(error)}
    report['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({key: report[key] for key in ('status', 'read_only')} | {
        'report': str(args.output), 'native_apply_acceptance': report.get('native_apply_acceptance'),
        'failure': report.get('failure')}, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
