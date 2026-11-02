# Modified 2026-11-02 by M-R-Abedini; see root MODIFICATIONS.md.
"""Recovery capabilities cannot become generic UI authorization or replay."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from eda_agent.ui import dialog_recovery as recovery, dialog_report, windows
from eda_agent.tools import uiauto


@pytest.fixture
def screen(monkeypatch):
    session = {'native_session_id': 'session-1', 'pid': 12, 'process_started_at': 100,
               'main_hwnd': 1, 'workspace': 'D:/workspace', 'observer_id': 'observer'}
    dialog = windows.Window(hwnd=2, class_name='TDesignRuleCheckForm',
                            title='Design Rule Checker [mil]', pid=12,
                            controls=[windows.Control(hwnd=3, class_name='TXPBitBtn', text='Run Design Rule Check')])
    monkeypatch.setattr(recovery, 'session_identity', lambda pid: dict(session))
    monkeypatch.setattr(windows, 'dialogs', lambda pid: [dialog])
    monkeypatch.setattr(windows, 'window_pid', lambda hwnd: 12)
    recovery._observations.clear()
    return session, dialog


def binding_for(dialog):
    descriptions = [dialog_report.describe_dialog(dialog, use_ocr=False)]
    recovery.observe(12, descriptions)
    return descriptions[0]['buttons'][0].get('recovery_binding')


def test_observation_is_consumed_once_and_records_exact_handles(screen):
    _, dialog = screen
    binding = binding_for(dialog)
    assert binding['dialog_hwnd'] == 2 and binding['button_hwnd'] == 3
    target, button = recovery.resolve(12, binding, 'Run Design Rule Check')
    assert target is dialog and button.hwnd == 3
    with pytest.raises(ValueError, match='consumed'):
        recovery.resolve(12, binding, 'Run Design Rule Check')


@pytest.mark.parametrize('change', ['pid_reuse', 'native_session', 'dialog_hwnd', 'dialog_title',
                                  'button_hwnd', 'button_caption', 'disabled', 'hidden', 'duplicate'])
def test_identity_changes_refuse_before_click(screen, change):
    session, dialog = screen
    binding = binding_for(dialog)
    if change == 'pid_reuse':
        session['process_started_at'] += 1
    elif change == 'native_session':
        session['native_session_id'] = 'session-2'
    elif change == 'dialog_hwnd':
        dialog.hwnd = 20
    elif change == 'dialog_title':
        dialog.title = 'Engineering Change Order'
    elif change == 'button_hwnd':
        dialog.controls[0].hwnd = 30
    elif change == 'button_caption':
        dialog.controls[0].text = 'Execute Changes'
    elif change == 'disabled':
        dialog.controls[0].enabled = False
    elif change == 'hidden':
        dialog.visible = False
    else:
        dialog.controls.append(deepcopy(dialog.controls[0]))
    with pytest.raises(ValueError):
        recovery.resolve(12, binding, 'Run Design Rule Check')


def test_expired_capability_is_refused(screen, monkeypatch):
    _, dialog = screen
    binding = binding_for(dialog)
    observed_at = recovery._observations[binding['token']][0]
    monkeypatch.setattr(recovery.time, 'monotonic', lambda: observed_at + recovery.MAX_AGE_SECONDS + 1)
    with pytest.raises(ValueError, match='expired'):
        recovery.resolve(12, binding, 'Run Design Rule Check')


def test_durable_native_session_requires_exact_current_process(tmp_path, monkeypatch):
    from eda_agent import config
    owner = {'pid': 12, 'session_started_at': 100.0, 'session_id': 'native-1'}
    (tmp_path / 'bridge-owner.json').write_text(json.dumps(owner))
    (tmp_path / 'bridge-ready.json').write_text(json.dumps({'session_id': 'native-1'}))
    process = SimpleNamespace(name=lambda: 'X2.exe', create_time=lambda: 100.0)
    monkeypatch.setattr(recovery.psutil, 'Process', lambda pid: process)
    monkeypatch.setattr(config, 'get_config', lambda: SimpleNamespace(workspace_dir=tmp_path))
    monkeypatch.setattr(windows, 'enumerate_windows', lambda pid: [
        windows.Window(hwnd=1, class_name='TDocumentForm', title='Altium', pid=12)])
    session = recovery.session_identity(12)
    assert session['native_session_id'] == 'native-1' and session['main_hwnd'] == 1
    # Same PID after an editor restart does not inherit the saved owner/session.
    process.create_time = lambda: 101.0
    with pytest.raises(RuntimeError, match='existing native session'):
        recovery.session_identity(12)
    assert json.loads((tmp_path / 'bridge-owner.json').read_text()) == owner


def test_wizard_eco_and_unobserved_keyboard_fallback_never_get_capabilities(screen):
    _, dialog = screen
    for title, cls, caption in [('Engineering Change Order', 'TChangeManagementForm', 'Execute Changes'),
                                 ('Engineering Change Order', 'TChangeManagementForm', 'Report Changes'),
                                 ('Update From Libraries', 'TXPForm', 'OK'),
                                 ('Save Changes', '#32770', 'Cancel')]:
        dialog.title, dialog.class_name, dialog.controls[0].text = title, cls, caption
        assert binding_for(dialog) is None


@pytest.mark.asyncio
async def test_public_button_tool_uses_bound_native_control_with_bridge_unavailable(screen, monkeypatch):
    _, dialog = screen
    captured, clicked = {}, []
    class Mcp:
        def tool(self, *args, **kwargs):
            def register(fn):
                captured[fn.__name__] = fn
                return fn
            return register
    monkeypatch.setattr(uiauto, '_altium_pid', lambda: (12, None))
    monkeypatch.setattr(windows, 'click', lambda button: clicked.append(button.hwnd))
    monkeypatch.setattr(windows, 'wait_for_close', lambda *args, **kwargs: False)
    uiauto.register_uiauto_tools(Mcp())
    binding = binding_for(dialog)
    out = await captured['app_press_dialog_button']('Run Design Rule Check', recovery_binding=binding)
    assert out['ok'] and clicked == [3]
    assert out['recovery']['native_operation_completion_proven'] is False
    # A repeated client call cannot press again, even though DRC remains open.
    repeated = await captured['app_press_dialog_button']('Run Design Rule Check', recovery_binding=binding)
    assert not repeated['ok'] and clicked == [3]
