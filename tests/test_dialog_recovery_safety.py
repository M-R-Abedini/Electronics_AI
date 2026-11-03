"""Modal recovery uses observed UI only; normal CAD checkpoints stay mandatory."""
import asyncio
from copy import deepcopy
from pathlib import Path
import sys

import pytest
from mcp import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from suite_checkpoints import SafetyService, is_mutation
from suite_stdio import BackendSpec, Suite


def observed_report(title='Design Rule Checker [mil]', class_name='TDesignRuleCheckForm', caption='Run Design Rule Check'):
    session = dict(native_session_id='native-1', pid=123, process_started_at=456.0,
                   main_hwnd=100, workspace='D:\\workspace', observer_id='observer-1')
    binding = {**session, 'dialog_hwnd': 101, 'dialog_title': title, 'dialog_class': class_name,
               'button_hwnd': 102, 'button_caption': caption, 'button_class': 'TXPBitBtn', 'token': 'observed'}
    return {'ok': True, 'pid': 123, 'recovery_session': session, 'dialogs': [
        {'title': title, 'class': class_name, 'hwnd': 101, 'enabled': True, 'visible': True,
         'buttons': [{'caption': caption, 'class': 'TXPBitBtn', 'hwnd': 102,
                      'enabled': True, 'visible': True, 'recovery_binding': binding}]}]}


def test_blocked_bridge_recovery_dispatches_bound_button_but_never_cad_write(tmp_path):
    async def check():
        class Worker:
            spec = BackendSpec('eda', 'unused')
            calls = []
            async def request(self, method, **kwargs):
                self.calls.append(kwargs)
                name = kwargs['name']
                data = observed_report() if name == 'app_list_open_dialogs' else (
                    {'bridge_answering': False} if name == 'app_context' else {'ok': True})
                return types.CallToolResult(content=[], structuredContent=data)

        suite = Suite([BackendSpec('eda', 'unused')])
        worker = Worker()
        suite.workers['eda'] = worker
        suite._catalogs['eda'] = [types.Tool(name=name, inputSchema={'type': 'object'})
                                  for name in ('app_press_dialog_button', 'pcb_place_tracks', 'app_drive_dialogs')]
        suite.safety = SafetyService(tmp_path, suite.native_read)
        suite.safety._last_native_checkpoint = {
            'coordination': {'native_session_id': 'native-1', 'workspace_path': 'D:\\workspace'},
            'checkpoint': {'checkpoint_id': 'prior-checkpoint', 'warnings': []}}
        response = await suite.invoke('eda__app_press_dialog_button', {'button_caption': 'Run Design Rule Check'})
        assert [c['name'] for c in worker.calls] == ['app_list_open_dialogs', 'app_press_dialog_button']
        assert worker.calls[-1]['arguments']['recovery_binding']['dialog_hwnd'] == 101
        receipt = response.meta['electronics_ai']['dialog_recovery']
        assert receipt['native_session_id'] == 'native-1'
        assert receipt['checkpoint_reused'] == 'prior-checkpoint'
        assert receipt['native_operation_completion_proven'] is False
        assert is_mutation('app_press_dialog_button', {'button_caption': 'Run Design Rule Check'})
        for name in ('pcb_place_tracks', 'app_drive_dialogs'):
            with pytest.raises(RuntimeError, match='cannot verify Altium context'):
                await suite.invoke('eda__' + name, {})
        assert not any(c['name'] in {'pcb_place_tracks', 'app_drive_dialogs'} for c in worker.calls)
    asyncio.run(check())


@pytest.mark.parametrize('title,cls,caption', [
    ('Engineering Change Order', 'TChangeManagementForm', 'Execute Changes'),
    ('Engineering Change Order', 'TChangeManagementForm', 'Report Changes'),
    ('Update From Libraries', 'TXPForm', 'OK'),
    ('Save Changes', '#32770', 'Cancel'),
    ('Design Rule Checker [mil]', 'WrongForm', 'Run'),
])
def test_nonapproved_ui_still_requires_normal_checkpoint(tmp_path, title, cls, caption):
    calls = []
    async def read(name, arguments):
        calls.append(name)
        return observed_report(title, cls, caption) if name == 'app_list_open_dialogs' else {'bridge_answering': False}
    service = SafetyService(tmp_path, read)
    async def check():
        assert await service.prepare_dialog_recovery({'button_caption': caption, 'allow_irreversible': True}) is None
        with pytest.raises(RuntimeError, match='cannot verify Altium context'):
            await service.before('app_press_dialog_button', {'button_caption': caption})
    asyncio.run(check())
    assert calls == ['app_list_open_dialogs', 'app_context']


@pytest.mark.parametrize('change', ['ambiguous', 'stale_binding', 'disabled', 'wrong_pid'])
def test_ambiguous_stale_or_disabled_recovery_is_refused(tmp_path, change):
    report = observed_report()
    arguments = {'button_caption': 'Run Design Rule Check'}
    if change == 'ambiguous':
        report['dialogs'].append(deepcopy(report['dialogs'][0]))
    elif change == 'stale_binding':
        arguments['recovery_binding'] = deepcopy(report['dialogs'][0]['buttons'][0]['recovery_binding'])
        arguments['recovery_binding']['process_started_at'] = 1
    elif change == 'disabled':
        report['dialogs'][0]['buttons'][0]['enabled'] = False
    else:
        report['pid'] = 999
    async def read(name, arguments):
        assert name == 'app_list_open_dialogs'
        return report
    with pytest.raises((ValueError, RuntimeError)):
        asyncio.run(SafetyService(tmp_path, read).prepare_dialog_recovery(arguments))


def test_checkpoint_is_not_reused_across_native_sessions(tmp_path):
    async def read(name, arguments):
        return observed_report('Error', 'TMessageForm', 'OK')
    service = SafetyService(tmp_path, read)
    service._last_native_checkpoint = {'coordination': {'native_session_id': 'old-session'},
                                     'checkpoint': {'checkpoint_id': 'old', 'warnings': []}}
    arguments, receipt, checkpoint = asyncio.run(service.prepare_dialog_recovery({'button_caption': 'OK'}))
    assert arguments['recovery_binding']['dialog_title'] == 'Error'
    assert checkpoint is None and receipt['checkpoint_reused'] is None


def test_disabled_dialog_inventory_never_grants_recovery(tmp_path):
    async def read(name, arguments):
        raise RuntimeError('Preflight requires enabled eda__app_list_open_dialogs')
    service = SafetyService(tmp_path, read)
    assert asyncio.run(service.prepare_dialog_recovery({'button_caption': 'Run'})) is None
    with pytest.raises(RuntimeError, match='requires enabled'):
        asyncio.run(service.prepare_dialog_recovery({'button_caption': 'Run', 'recovery_binding': {}}))
