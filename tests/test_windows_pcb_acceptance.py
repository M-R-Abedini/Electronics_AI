"""Acceptance harness contract tests with a fake MCP peer; no editor is invoked."""
import asyncio
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location('windows_acceptance',
    Path(__file__).resolve().parents[1] / 'scripts/windows_pcb_acceptance.py')
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


def reply(data, error=False):
    return SimpleNamespace(structuredContent=data, content=[], isError=error)


class Peer:
    def __init__(self, board=None):
        self.calls = []
        self.board = str(board) if board else None
        self.health = {'backend': 'altium', 'bridge_answering': True,
            'script_version_match': True, 'running_script_version': 'native-current',
            'deployed_script_version': 'native-current', 'unsaved_detection_complete': False,
            'coordination': {'script_hash_match': True, 'compiled_project_path': 'current.PrjScr',
                             'expected_project_path': 'current.PrjScr'}}
        self.wrong_target_succeeds = False
        self.evidence_board = self.board

    async def initialize(self):
        return SimpleNamespace(serverInfo=SimpleNamespace(name='Electronics AI', version='0.7.0'))

    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in
            ('suite_catalog', 'suite_invoke', 'suite_status')])

    async def call_tool(self, name, arguments):
        self.calls.append((name, copy.deepcopy(arguments)))
        if name == 'suite_catalog':
            return reply({'tools': [{'name': name} for name in
                ('eda__app_context', 'eda__pcb_get_components',
                 'layout__capture_context', 'layout__get_evidence')]})
        assert name == 'suite_invoke'
        tool, params = arguments['name'], arguments['arguments']
        if tool == 'eda__app_context':
            return reply(self.health)
        if tool == 'eda__pcb_get_components':
            if params['target_board_path'] != self.board:
                return reply({'error': 'ERR_TARGET_DOCUMENT_MISMATCH'},
                             error=not self.wrong_target_succeeds)
            return reply({'units': 'mm', 'resolved_board_path': self.board, 'components': []})
        if tool == 'layout__capture_context':
            return reply({'context_id': 'a' * 64})
        if tool == 'layout__get_evidence':
            return reply({'trusted_live': True, 'capture_digest': 'b' * 64,
                          'context': {'board_path': self.evidence_board}})
        raise AssertionError('Unexpected or mutating tool: ' + tool)


def run(peer, report, board=None):
    return asyncio.run(acceptance.check_session(peer, report, plugin_version='0.7.0',
        native_version='native-current', board_path=board))


def test_default_health_run_defers_board_and_apply_without_claiming_clean_buffer():
    peer, report = Peer(), {}
    run(peer, report)
    tools = [args['name'] for name, args in peer.calls if name == 'suite_invoke']
    assert tools == ['eda__app_context']
    assert all(check['passed'] for check in report['checks'])
    assert report['native_apply_acceptance']['status'] == 'DEFERRED'
    assert report['pcb_geometry_acceptance']['status'] == 'DEFERRED'
    assert report['fixture_U12'] == 'not_present_in_repo'
    assert report['buffer_cleanliness_certified'] is False


@pytest.mark.parametrize('damage', ['native_version', 'source_generation'])
def test_old_compiled_sources_fail_before_any_board_capture(damage):
    peer, report = Peer(), {}
    if damage == 'native_version':
        peer.health['running_script_version'] = 'old-native'
    else:
        peer.health['coordination']['script_hash_match'] = False
    with pytest.raises(acceptance.AcceptanceFailure):
        run(peer, report)
    assert [args['name'] for name, args in peer.calls if name == 'suite_invoke'] == ['eda__app_context']


def test_explicit_board_readback_and_wrong_target_refusal_use_only_read_capabilities(tmp_path):
    # Opaque bytes and a fake MCP peer exercise the harness, not an Altium file parser.
    board = tmp_path / 'Existing.PcbDoc'
    board.write_bytes(b'preserved opaque fixture')
    peer, report = Peer(board), {}
    run(peer, report, board)
    tools = [args['name'] for name, args in peer.calls if name == 'suite_invoke']
    assert tools == ['eda__app_context', 'eda__pcb_get_components',
                     'layout__capture_context', 'layout__get_evidence', 'eda__pcb_get_components']
    assert report['pcb_geometry_acceptance']['status'] == 'PASS'
    assert report['board_sha256_before'] == report['board_sha256_after']
    assert board.read_bytes() == b'preserved opaque fixture'
    assert report['native_apply_acceptance']['status'] == 'DEFERRED'
    assert report['board_mutation_tools_invoked'] is False


@pytest.mark.parametrize('damage', ['fallback_success', 'wrong_evidence_target'])
def test_unloaded_target_fallback_or_misbound_evidence_cannot_pass(tmp_path, damage):
    board = tmp_path / 'Existing.PcbDoc'
    board.write_bytes(b'preserve')
    peer, report = Peer(board), {}
    if damage == 'fallback_success':
        peer.wrong_target_succeeds = True
    else:
        peer.evidence_board = str(tmp_path / 'Different.PcbDoc')
    with pytest.raises(acceptance.AcceptanceFailure):
        run(peer, report, board)
    assert report['board_sha256_before'] == report['board_sha256_after']
    assert any(not item['passed'] for item in report['checks'])
