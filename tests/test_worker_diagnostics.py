"""Read-only/fake transports preserve startup causes and finish failed waiters."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from mcp import types
from mcp.shared.exceptions import McpError
from mcp.shared.session import CONNECTION_CLOSED

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import suite_stdio as suite


def test_nested_exception_group_is_bounded_and_redacts_secret_causes():
    secret = 'private-env-value-123'
    try:
        try:
            raise PermissionError('launch token=' + secret + ' https://user:password@example.test/tool')
        except PermissionError as cause:
            raise RuntimeError('Bearer abcdefghijkl ' + ('long detail ' * 1000)) from cause
    except RuntimeError as error:
        nested = ExceptionGroup('startup', [ExceptionGroup('initialization', [error])])
    detail = suite.worker_diagnostic(nested, 'initializing', {'ACCESS_TOKEN': secret})
    rendered = json.dumps(detail)
    assert secret not in rendered and 'abcdefghijkl' not in rendered and 'user:password' not in rendered
    assert '[redacted]' in rendered
    assert detail['kind'] == 'process_start' and detail['phase'] == 'initializing'
    assert {item['type'] for item in detail['exceptions']} >= {'ExceptionGroup', 'RuntimeError', 'PermissionError'}
    assert all(len(item['message']) <= 600 and len(item['frames']) <= 4 for item in detail['exceptions'])
    assert all('/' not in frame['file'] and '\\' not in frame['file']
               for item in detail['exceptions'] for frame in item['frames'])
    assert detail['automatic_retry'] is False
    wide = ExceptionGroup('many causes', [ValueError('x' * 1000) for _ in range(100)])
    bounded = suite.worker_diagnostic(wide, 'initializing')
    assert len(bounded['exceptions']) == 12 and bounded['truncated']
    assert len(json.dumps(bounded)) < 12000


def test_missing_process_completes_all_queued_futures_and_preserves_start_phase(tmp_path):
    async def check():
        worker = suite.Worker(suite.BackendSpec('eda', str(tmp_path / 'missing-worker.exe')))
        replies = await asyncio.wait_for(asyncio.gather(
            worker.request('_capabilities'), worker.request('list_tools'), return_exceptions=True), 5)
        assert all(isinstance(reply, RuntimeError) for reply in replies)
        await worker.task
        detail = worker.failure_diagnostic
        assert detail['kind'] == 'process_start' and detail['phase'] == 'starting'
        assert detail['outcome'] == 'not_dispatched' and detail['last_request'] is None
        assert all(reply.diagnostic['id'] == detail['id'] for reply in replies)
        assert all(reply.diagnostic['outcome'] == 'not_dispatched' for reply in replies)
        assert worker.queue.empty() and not worker.running and worker.phase == 'stopped'
        original_task = worker.task
        with pytest.raises(RuntimeError, match='no command was replayed'):
            await worker.request('list_tools')
        assert worker.task is original_task
    asyncio.run(check())


def install_fake_transport(monkeypatch, session):
    opened = []
    @asynccontextmanager
    async def stdio(parameters, **kwargs):
        opened.append(parameters)
        yield object(), object()
    monkeypatch.setattr(suite, 'stdio_client', stdio)
    monkeypatch.setattr(suite, 'ClientSession', lambda *args, **kwargs: session)
    return opened


class FakeSession:
    def __init__(self):
        self.calls = []
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    async def initialize(self):
        self.calls.append('initialize')
        return SimpleNamespace(capabilities=types.ServerCapabilities())
    async def list_tools(self):
        self.calls.append('list_tools')
        return types.ListToolsResult(tools=[])


def test_initialization_timeout_finishes_queued_requests_without_dispatch(monkeypatch):
    async def check():
        class SlowInitialization(FakeSession):
            async def initialize(self):
                self.calls.append('initialize')
                await asyncio.Event().wait()
        session = SlowInitialization()
        opened = install_fake_transport(monkeypatch, session)
        worker = suite.Worker(suite.BackendSpec('eda', 'mock-worker', startup_timeout_seconds=.01))
        replies = await asyncio.wait_for(asyncio.gather(
            worker.request('list_tools'), worker.request('call_tool', name='pcb_place_tracks'),
            return_exceptions=True), 1)
        await worker.task
        assert all(isinstance(reply, RuntimeError) for reply in replies)
        assert worker.failure_diagnostic['kind'] == 'startup_timeout'
        assert worker.failure_diagnostic['phase'] == 'initializing'
        assert worker.failure_diagnostic['outcome'] == 'not_dispatched'
        assert session.calls == ['initialize'] and len(opened) == 1
        with pytest.raises(RuntimeError, match='startup_timeout'):
            await worker.request('list_tools')
        assert len(opened) == 1
    asyncio.run(check())


@pytest.mark.parametrize('fault,expected_kind', [
    (ConnectionAbortedError('Bearer should-be-redacted'), 'transport_closed'),
    (BrokenPipeError('native stdout closed'), 'transport_closed'),
    (UnicodeDecodeError('utf-8', b'\x81', 0, 1, 'invalid byte'), 'stdio_decoding'),
    (McpError(types.ErrorData(code=CONNECTION_CLOSED, message='Connection closed')), 'transport_closed'),
    (ExceptionGroup('request and transport failed', [TimeoutError('response timed out'),
        ConnectionAbortedError('channel aborted')]), 'transport_closed'),
])
def test_fatal_transport_finishes_active_and_queued_requests_without_replay(monkeypatch, fault, expected_kind):
    async def check():
        started, fail = asyncio.Event(), asyncio.Event()
        class FailingSession(FakeSession):
            async def call_tool(self, **arguments):
                self.calls.append(arguments['name'])
                started.set()
                await fail.wait()
                raise fault
        session = FailingSession()
        opened = install_fake_transport(monkeypatch, session)
        worker = suite.Worker(suite.BackendSpec('eda', 'mock-worker'))
        active = asyncio.create_task(worker.request('call_tool', name='pcb_place_tracks', arguments={'x': 1}))
        await asyncio.wait_for(started.wait(), 1)
        operation = dict(worker.active_request)
        queued = asyncio.create_task(worker.request('list_tools'))
        await asyncio.sleep(0)
        fail.set()
        replies = await asyncio.wait_for(asyncio.gather(active, queued, return_exceptions=True), 1)
        await worker.task
        assert all(isinstance(reply, RuntimeError) for reply in replies)
        detail = worker.failure_diagnostic
        assert detail['kind'] == expected_kind and detail['phase'] == 'dispatching'
        assert detail['last_request'] == operation
        assert operation['tool'] == 'pcb_place_tracks' and operation['gateway_operation_id']
        assert 'arguments' not in operation
        assert detail['outcome'] == replies[0].diagnostic['outcome'] == 'unknown_after_dispatch'
        assert replies[1].diagnostic['outcome'] == 'not_dispatched'
        assert replies[0].diagnostic['id'] == replies[1].diagnostic['id']
        assert 'should-be-redacted' not in str(replies[0])
        assert session.calls == ['initialize', 'pcb_place_tracks'] and len(opened) == 1
        with pytest.raises(RuntimeError, match='no command was replayed'):
            await worker.request('call_tool', name='pcb_place_tracks', arguments={'x': 1})
        assert session.calls == ['initialize', 'pcb_place_tracks'] and len(opened) == 1
    asyncio.run(check())


def test_ordinary_tool_error_does_not_stop_a_healthy_actor(monkeypatch):
    async def check():
        class ToolErrorSession(FakeSession):
            async def call_tool(self, **arguments):
                self.calls.append(arguments['name'])
                raise McpError(types.ErrorData(code=-32602, message='invalid tool arguments'))
        session = ToolErrorSession()
        install_fake_transport(monkeypatch, session)
        worker = suite.Worker(suite.BackendSpec('eda', 'mock-worker'))
        with pytest.raises(McpError, match='invalid tool arguments'):
            await worker.request('call_tool', name='invalid_tool')
        assert (await worker.request('list_tools')).tools == []
        assert worker.failure_diagnostic is None
        await worker.close()
        assert session.calls == ['initialize', 'invalid_tool', 'list_tools']
    asyncio.run(check())
