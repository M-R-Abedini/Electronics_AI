"""Acceptance tests for the five transport findings, with no live Altium."""
import ast
import contextlib
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock
import uuid

import pytest
import bridge_coordination as coord
from eda_agent.bridge.altium_bridge import AltiumBridge, CommandResponse
from eda_agent.bridge.exceptions import AltiumProtocolError

ROOT=Path(__file__).resolve().parents[1]


def extract(path,name,namespace):
    tree=ast.parse(path.read_text(encoding='utf-8'))
    node=next(n for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==name)
    node.decorator_list=[]
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),namespace)
    return namespace[name]


@pytest.fixture
def ownership(tmp_path,monkeypatch):
    monkeypatch.setattr(coord,'RUNTIME',tmp_path)
    monkeypatch.setattr(coord,'WORKSPACE',tmp_path)
    monkeypatch.setattr(coord,'_editor_state',lambda:dict(pid=77,blocked=False))
    monkeypatch.setattr(coord,'_session_started_at',lambda pid:100)
    monkeypatch.setattr(coord, '_confirm_editor_return', lambda state: None)
    return tmp_path


def test_late_completion_releases_ownership_without_replaying_operation(ownership):
    request=ownership/'request.json';done=ownership/'completed_id.json'
    coord.begin_operation('id',request,done)
    done.write_text(json.dumps({'request_id':'old-id'}))
    with pytest.raises(RuntimeError):coord._guard_editor()
    done.write_text(json.dumps({'request_id':'id'}))
    coord._guard_editor()
    assert not coord._operation_marker().exists()


def test_editor_restart_retires_previous_native_ownership(ownership,monkeypatch):
    request=ownership/'request.json';request.touch()
    coord.begin_operation('id',request,ownership/'done.json')
    monkeypatch.setattr(coord,'_session_started_at',lambda pid:200)
    coord._guard_editor()
    assert not coord._operation_marker().exists() and not request.exists()


def test_switching_to_another_live_editor_does_not_retire_ownership(ownership,monkeypatch):
    coord.begin_operation('id',ownership/'request.json',ownership/'done.json')
    monkeypatch.setattr(coord,'_editor_state',lambda:dict(pid=88,blocked=False))
    with pytest.raises(RuntimeError,match='another running Altium'):coord._guard_editor()
    assert coord._operation_marker().exists()


def test_explicit_manual_reset_withdraws_requests_without_launch(ownership,monkeypatch):
    inbox=ownership/'project-close-request.json';inbox.touch()
    coord.begin_operation('id',inbox,ownership/'done.json')
    (ownership/'progress_work.json').touch();(ownership/'request_edit.json').touch()
    monkeypatch.setattr(coord,'engine_lock',contextlib.nullcontext)
    launcher=MagicMock();monkeypatch.setattr(coord.subprocess,'Popen',launcher)
    coord.reset_after_manual_stop()
    assert not coord._operation_marker().exists() and not inbox.exists()
    assert not list(ownership.glob('progress_*')) and not list(ownership.glob('request_*'))
    launcher.assert_not_called()


@pytest.mark.parametrize('data',[
    [], {}, {'id':'other','success':True}, {'id':'expected','success':'false'},
    {'id':'expected','success':1}, {'id':'expected','success':True,'protocol_version':True},
    {'id':'expected','success':False,'error':'bad'}, {'id':'expected','success':True,'error':{'code':'BAD'}},
])
def test_malformed_response_is_structured_protocol_error(data):
    with pytest.raises(AltiumProtocolError):CommandResponse.from_dict(data,expected_id='expected')


def test_polling_rejects_wrong_body_id(tmp_path):
    bridge=AltiumBridge.__new__(AltiumBridge)
    bridge.config=SimpleNamespace(workspace_dir=tmp_path,poll_interval=.001)
    bridge._dialog_probe=lambda:None
    bridge._response_path('expected').write_text(json.dumps({'id':'other','success':True}))
    with pytest.raises(AltiumProtocolError):bridge._poll_response('expected',.1)


def test_long_current_session_handler_is_busy_regardless_of_age(ownership):
    marker=ownership/'progress_long.json';marker.write_text('{}');os.utime(marker,(101,101))
    assert time.time()-marker.stat().st_mtime>600
    assert coord._handler_busy() and marker.exists()


def test_previous_editor_session_marker_is_retired(ownership):
    marker=ownership/'progress_old.json';marker.write_text('{}');os.utime(marker,(99,99))
    assert not coord._handler_busy() and not marker.exists()


def test_unreadable_ownership_is_not_treated_as_free_engine(ownership):
    coord._operation_marker().write_text('{')
    with pytest.raises(RuntimeError,match='cannot be verified'):coord._guard_editor()
