import threading
import sys
import os
import argparse
from pathlib import Path


def configure_cli(argv):
    # Set import-time selection before registering the server. Otherwise its
    # CLI re-exec switches to eda_agent.server and drops this coordination
    # wrapper (including shared locks, restart and idle-release handling).
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument('--toolset')
    parser.add_argument('--backend')
    options, _ = parser.parse_known_args(argv)
    for name, value in [('EDA_AGENT_TOOLSET', options.toolset), ('EDA_AGENT_BACKEND', options.backend)]:
        if value is not None:
            os.environ[name] = value


configure_cli(sys.argv[1:])
# Prefer deployed sources to an unrelated editable package in the interpreter.
sys.path.insert(0, str(Path(__file__).resolve().parent / 'eda-agent' / 'src'))
from suite_config import configure_environment
configure_environment()
from bridge_coordination import (engine_lock, async_engine_lock, run_engine_worker,
                                 background_engine_lock, ensure_eda, stop_eda, close_project_once)
from eda_agent.bridge.altium_bridge import AltiumBridge

original = AltiumBridge._execute_command

def coordinated(self, command, params, timeout):
    background = threading.current_thread() is getattr(self, '_keepalive_thread', None)
    if background:
        with background_engine_lock() as acquired:
            if not acquired:
                return {'engine_busy': True}
            if not (self.config.workspace_dir / 'bridge-ready.json').exists():
                return {'engine_released': True}
            # A stop can race the ready check. Bound this health-only wait so
            # it cannot hold the shared lock for five seconds ahead of a user.
            return original(self, command, params, min(timeout, 1.0))
    with engine_lock():
        return dispatch_locked(self, command, params, timeout)


def dispatch_locked(self, command, params, timeout):
    if command == 'application.stop_server':
        # A stop response is written before DelphiScript returns. Wait
        # for session-bound final shutdown evidence, without relaunching.
        stop_eda()
        return {'success': True, 'engine_release_confirmed': True}
    if command == 'suite.execute_script':
        from suite_script_overlay import execute_overlay
        return execute_overlay(self, original, params, timeout)
    health = ensure_eda(self, original)
    if command == 'suite.capture_snapshot':
        import time
        from bridge_coordination import _guard_editor, _session_started_at
        from suite_reads import capture
        board_path = params.get('board_path', '')
        if not Path(board_path).is_absolute() or not board_path.lower().endswith('.pcbdoc'):
            raise ValueError('An absolute loaded PcbDoc path is required')
        state = _guard_editor()
        editor = dict(pid=state['pid'], started=_session_started_at(state['pid']))
        started = time.time()
        # One native iterator pass; no other client's command can run
        # between sections and no focus-dependent second read is needed.
        data = original(self, 'pcb.get_pad_connectivity',
                        dict(board_path=board_path, include_components=True, limit=0), timeout)
        return capture(data, started, editor, health)
    if command == 'project.close':
        target = dict(params)
        if not target.get('project_path'):
            target['project_path'] = original(self, 'project.get_focused', {}, timeout)['project_path']
        if not target['project_path'].lower().endswith('.prjscr'):
            return close_project_once(target, timeout)
    if command == 'application.ping' and not params:
        # The foreground preflight already answered this exact query.
        return health
    return original(self, command, params, timeout)


async def coordinated_async(self, command, params=None, timeout=None):
    from eda_agent.bridge.exceptions import AltiumNotRunningError
    if not self.is_altium_running():
        raise AltiumNotRunningError()
    self._ensure_keepalive()
    if timeout is None:
        timeout = self.config.poll_timeout
    # Waiting is cancellable. After dispatch, cancellation drains the native
    # worker before releasing either admission or the Windows engine mutex.
    async with async_engine_lock():
        return await run_engine_worker(lambda: dispatch_locked(self, command, params or {}, timeout))

AltiumBridge._execute_command = coordinated
AltiumBridge.send_command_async = coordinated_async
AltiumBridge.suite_shared_reads = True
from eda_agent.server import main, mcp

@mcp.tool()
async def _suite_legacy_execute(command: str, params: dict) -> dict:
    """Internal compatibility adapter; only the suite legacy namespace exposes it."""
    import json
    from coffeenmusic.server.batch_safety import validate_legacy_json_text
    from eda_agent.bridge import get_bridge
    validate_legacy_json_text(params)
    bridge = get_bridge()
    if command == 'run_altium_script':
        return await bridge.send_command_async('suite.execute_script', params,
            timeout=float(params.get('timeout_seconds', 120)))
    data = await bridge.send_command_async('legacy.' + command,
        {'arguments': params, 'pretty_params': json.dumps(params, indent=2)})
    # Business results already have their historical shape; only the common
    # EDA transport envelope is translated here.
    return {'success': True, 'result': data}


@mcp.tool()
async def _suite_copper_snapshot(board_path: str) -> dict:
    """Internal single-handler live copper capture; not part of EDA discovery."""
    from eda_agent.bridge import get_bridge
    return await get_bridge().send_command_async('pcb.get_copper_snapshot', {'board_path': board_path}, timeout=120)


@mcp.tool()
async def _suite_copper_focus(board_path: str, x_mm: float, y_mm: float,
                              layer: str, net_names: list[str], board_identity: str,
                              radius_mm: float = 2) -> dict:
    """Internal viewport cross-probe of a report-bound location."""
    from eda_agent.bridge import get_bridge
    return await get_bridge().send_command_async('pcb.focus_copper_violation', {
        'board_path': board_path, 'x_mm': x_mm, 'y_mm': y_mm, 'layer': layer,
        'net_names': net_names, 'board_identity': board_identity, 'radius_mm': radius_mm})


@mcp.tool()
async def _suite_copper_apply_rules(board_path: str, net_classes: list[dict],
                                   rules: list[dict], differential_pairs: list[dict],
                                   expected_native_snapshot: str = '',
                                   owned_class_names: list[str] | None = None,
                                   expected_native_snapshot_token: str = '') -> dict:
    """Internal bounded rule batch; the gateway checkpoints before dispatch."""
    from suite_si_pi import prepare_native_rule_batch
    from eda_agent.bridge import get_bridge
    params = prepare_native_rule_batch(board_path, net_classes, rules, differential_pairs)
    if expected_native_snapshot_token:
        import re
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', expected_native_snapshot_token):
            raise ValueError('Invalid native copper snapshot token')
        params['expected_native_snapshot_token'] = expected_native_snapshot_token
    else:
        if not expected_native_snapshot or len(expected_native_snapshot) > 24 * 1024 * 1024:
            raise ValueError('A bounded fresh native copper precondition is required')
        params['expected_native_snapshot'] = expected_native_snapshot
    owned = owned_class_names or []
    if len(owned) > 500 or len(set(owned)) != len(owned) or any(
            not isinstance(name, str) or not name.startswith('MCP_SIPI_') for name in owned):
        raise ValueError('Invalid persisted managed-class manifest')
    params['owned_class_count'] = len(owned)
    params.update({f'owned_class_{i}': name for i, name in enumerate(owned)})
    return await get_bridge().send_command_async('pcb.apply_copper_rules', params, timeout=120)

@mcp.tool()
async def _suite_layout_apply_plan(board_path: str, board_identity: str,
                                    expected_native_snapshot_token: str,
                                    moves: list[dict], operation_id: str) -> dict:
    """Private plan-bound placement. No saves, flips, retries or rollback."""
    from suite_placement_adapter import prepare_layout_native_batch
    from eda_agent.bridge import get_bridge
    params = prepare_layout_native_batch(board_path, board_identity,
        expected_native_snapshot_token, moves, operation_id)
    return await get_bridge().send_command_async('pcb.apply_placement_plan', params, timeout=180)


# Keep private compatibility commands callable only by the gateway's direct
# adapter while preserving the 457-tool public EDA discovery contract.
_public_list_tools = mcp.list_tools
async def _suite_public_tools():
    return [tool for tool in await _public_list_tools() if not tool.name.startswith('_suite_')]
mcp.list_tools = _suite_public_tools
mcp._mcp_server.list_tools()(_suite_public_tools)

if __name__ == '__main__':
    main()
