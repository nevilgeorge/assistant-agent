import os
import struct
import threading
import time

import pytest

from assistant_agent import chat
from assistant_agent.chat import ChatError, DockerFrames, SessionManager


class FakeProcess:
    def __init__(self, token, receive, failed):
        self.receive = receive
        self.failed = failed
        self.prompts = []
        self.closed = False

    def send(self, prompt):
        self.prompts.append(prompt)

    def close(self):
        self.closed = True

    def text(self, text):
        self.receive({'type': 'stream_event', 'event': {'type': 'content_block_delta',
                      'delta': {'type': 'text_delta', 'text': text}}})

    def finish(self):
        self.receive({'type': 'result', 'subtype': 'success', 'result': 'fallback'})


@pytest.fixture
def manager():
    manager = SessionManager(FakeProcess)
    yield manager
    manager.close()


def test_multiple_turns_stream_without_restarting_process(manager):
    first = manager.submit('alice', 'Remember blue')
    session = manager.get('alice')
    process = session.process
    with pytest.raises(ChatError, match='progress'):
        manager.submit('alice', 'overlap')
    process.text('Bl')
    process.text('ue')
    # Whole assistant events must not duplicate streaming deltas.
    process.receive({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Blue'}]}})
    assert session.snapshot()['transcript'][-1]['text'] == 'Blue'
    assert session.active == first['turn_id']
    process.finish()
    second = manager.submit('alice', 'What color?')
    assert session.process is process and first['turn_id'] != second['turn_id']
    assert process.prompts == ['Remember blue', 'What color?']
    process.text('Blue again')
    process.finish()
    assert [e['type'] for e in session.events].count('turn_completion') == 2
    assert session.snapshot()['active_turn'] is None


def test_isolation_reset_failure_and_capacity(manager):
    manager.max_sessions = 2
    manager.submit('alice', 'private')
    manager.submit('bob', 'other')
    assert manager.get('alice').process is not manager.get('bob').process
    with pytest.raises(ChatError) as exc:
        manager.get('carol')
    assert exc.value.status == 503
    old = manager.get('alice')
    old.process.failed('Ended')
    assert old.process.closed and old.failed
    with pytest.raises(ChatError, match='new conversation'):
        manager.submit('alice', 'follow-up')
    manager.reset('alice')
    assert old.events[-1]['type'] == 'conversation_reset'
    assert manager.get('alice').id != old.id
    with pytest.raises(ChatError, match='Reload'):
        manager.submit('alice', 'stale', old.id)


def test_timeout_does_not_apply_to_later_turn(manager):
    first = manager.submit('alice', 'one')
    session = manager.get('alice')
    session.process.finish()
    manager.submit('alice', 'two')
    manager._timeout(session, first['turn_id'])
    assert not session.failed
    manager._timeout(session, session.active)
    assert session.failed and session.process.closed
    assert session.events[-1]['type'] == 'turn_failure'


def test_limits_and_shutdown(manager):
    manager.submit('alice', 'one')
    session = manager.get('alice')
    session.bytes = 2 * 1024 * 1024
    session.process.text('overflow')
    assert session.failed and session.process.closed
    manager.close()
    assert not manager.sessions


def test_docker_frames_split_headers_and_payloads():
    frames = DockerFrames()
    data = b''
    for stream, payload in [(1, '雪'.encode()), (2, b'diagnostic'), (1, b'\n')]:
        data += bytes([stream, 0, 0, 0]) + struct.pack('>I', len(payload)) + payload
    result = []
    for byte in data:
        result.extend(frames.feed(bytes([byte])))
    assert result == [(1, '雪'.encode()), (2, b'diagnostic'), (1, b'\n')]
    with pytest.raises(ValueError):
        list(frames.feed(b'\x09\0\0\0\0\0\0\0'))


def test_result_failure_retains_partial_output(manager):
    manager.submit('alice', 'one')
    session = manager.get('alice')
    session.process.text('partial')
    session.process.receive({'type': 'result', 'subtype': 'error_max_turns', 'is_error': True})
    assert session.snapshot()['transcript'][-1]['text'] == 'partial'
    assert session.failed and session.process.closed


@pytest.mark.skipif(os.getenv('CHAT_DOCKER_INTEGRATION') != '1', reason='Opt-in real Claude test')
def test_real_claude_multiple_turns():
    manager = SessionManager()
    try:
        manager.submit('integration', 'Remember the codeword cobalt. Reply with only OK.')
        session = manager.get('integration')
        deadline = time.monotonic() + 125
        while session.active and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not session.failed and session.active is None
        process = session.process
        manager.submit('integration', 'What codeword did I give you? Reply with only that word.')
        deadline = time.monotonic() + 125
        while session.active and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not session.failed and session.active is None
        assert session.process is process
        assert 'cobalt' in session.transcript[-1]['text'].lower()
        assert any(e['type'] == 'assistant_delta' for e in session.events)
        manager.reset('integration')
        assert process.closed
        assert not process.client.api.exec_inspect(process.exec_id)['Running']
    finally:
        manager.close()


def test_transport_reads_split_unicode_json_and_drains_stderr(monkeypatch):
    import json
    import socket
    from types import SimpleNamespace
    from assistant_agent import chat

    local, remote = socket.socketpair()
    received = []
    done = threading.Event()
    failures = []

    class API:
        def exec_create(self, *args, **kwargs):
            assert kwargs['stdin'] and not kwargs['tty']
            return {'Id': 'exec'}

        def exec_start(self, *args, **kwargs):
            return local

    commands = []
    container = SimpleNamespace(id='container', status='running',
        exec_run=lambda argv, **kwargs: commands.append(argv) or SimpleNamespace(exit_code=0))
    client = SimpleNamespace(api=API(), containers=SimpleNamespace(get=lambda name: container),
                             close=lambda: None)
    monkeypatch.setattr(chat.sandbox, '_client', lambda: client)
    process = chat.ClaudeProcess('test', lambda m: received.append(m) or done.set(),
                                 lambda message: failures.append(message))
    try:
        process.send('雪 $(touch nope)')
        request = json.loads(remote.recv(4096))
        assert request['message']['content'] == '雪 $(touch nope)'
        payload = json.dumps({'type': 'assistant', 'text': '雪'}, ensure_ascii=False).encode() + b'\n'
        stderr = b'private diagnostic'
        data = b'\x02\0\0\0' + struct.pack('>I', len(stderr)) + stderr
        data += b'\x01\0\0\0' + struct.pack('>I', len(payload)) + payload
        for byte in data:
            remote.sendall(bytes([byte]))
        assert done.wait(2)
        assert received == [{'type': 'assistant', 'text': '雪'}] and not failures
    finally:
        process.close()
        remote.close()
    assert 'kill -TERM' in commands[-1][-1]


def test_transport_malformed_json_fails(monkeypatch):
    import socket
    from types import SimpleNamespace
    from assistant_agent import chat

    local, remote = socket.socketpair()
    failed = threading.Event()
    container = SimpleNamespace(id='container', status='running',
                               exec_run=lambda *a, **k: SimpleNamespace(exit_code=0))
    api = SimpleNamespace(exec_create=lambda *a, **k: {'Id': 'exec'},
                          exec_start=lambda *a, **k: local)
    monkeypatch.setattr(chat.sandbox, '_client', lambda: SimpleNamespace(api=api,
        containers=SimpleNamespace(get=lambda name: container), close=lambda: None))
    process = chat.ClaudeProcess('test', lambda m: None, lambda message: failed.set())
    try:
        payload = b'not JSON\n'
        remote.sendall(b'\x01\0\0\0' + struct.pack('>I', len(payload)) + payload)
        assert failed.wait(2)
    finally:
        process.close()
        remote.close()


def test_idle_sessions_expire_but_active_sessions_stay(manager):
    manager.submit('active', 'one')
    idle = manager.get('idle')
    idle.last_used = 0
    manager.stop = OneSweep()
    manager._sweep()
    assert 'idle' not in manager.sessions
    assert idle.events[-1]['type'] == 'conversation_reset'
    assert 'active' in manager.sessions


class OneSweep:
    """Stand-in for SessionManager.stop that lets _sweep run exactly one pass."""
    calls = 0

    def wait(self, seconds):
        self.calls += 1
        return self.calls > 1

    def set(self):
        pass


def held(lock):
    """Whether the lock is unavailable to another thread (the chat locks are re-entrant)."""
    result = []

    def probe():
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
        result.append(not acquired)

    thread = threading.Thread(target=probe)
    thread.start()
    thread.join()
    return result[0]


def probing(manager, calls):
    """A FakeProcess that records whether either chat lock is held during each I/O call."""
    class ProbeProcess(FakeProcess):
        def __init__(self, token, receive, failed):
            super().__init__(token, receive, failed)
            self.session = receive.__self__
            self.record('spawn')

        def record(self, op):
            calls.append((op, held(manager.lock), held(self.session.condition)))

        def send(self, prompt):
            self.record('send')
            super().send(prompt)

        def close(self):
            self.record('close')
            super().close()

    return ProbeProcess


class Gate:
    """Process factory that blocks inside the spawn until released."""
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.made = []

    def __call__(self, token, receive, failed):
        self.entered.set()
        assert self.release.wait(5)
        self.made.append(FakeProcess(token, receive, failed))
        return self.made[-1]


def submit_async(manager, user, prompt):
    outcome = {}

    def run():
        try:
            outcome['result'] = manager.submit(user, prompt)
        except ChatError as exc:
            outcome['error'] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


def test_no_chat_lock_held_during_process_io(manager, monkeypatch):
    calls = []
    Probe = probing(manager, calls)

    class BrokenSend(Probe):
        def send(self, prompt):
            self.record('send')
            raise OSError('closed')

    monkeypatch.setattr(chat, 'cleanup_orphans',
                        lambda: calls.append(('cleanup', held(manager.lock), False)))
    manager.factory = Probe
    manager.needs_cleanup = True
    manager.max_sessions = 8

    def started(user):
        manager.submit(user, 'one')
        return manager.get(user)

    session = started('overflow')
    session.bytes = 2 * 1024 * 1024
    session.process.text('overflow')
    assert session.process.closed

    session = started('error')
    session.process.receive({'type': 'result', 'subtype': 'error_max_turns', 'is_error': True})
    assert session.process.closed

    session = started('transport')
    session.process.failed('Ended')
    assert session.process.closed

    session = started('timeout')
    manager._timeout(session, session.active)
    assert session.process.closed

    manager.factory = BrokenSend
    session = started('send')
    assert session.failed and session.process.closed
    manager.factory = Probe

    session = started('reset')
    manager.reset('reset')
    assert session.process.closed

    session = started('sweep')
    session.process.finish()
    session.last_used = 0
    manager.stop = OneSweep()
    manager._sweep()
    assert 'sweep' not in manager.sessions and session.process.closed

    session = started('shutdown')
    manager.close()
    assert session.process.closed

    assert {op for op, *_ in calls} == {'cleanup', 'spawn', 'send', 'close'}
    assert [call for call in calls if call[1] or call[2]] == []


def test_spawn_does_not_block_other_callers(manager):
    manager.factory = gate = Gate()
    thread, outcome = submit_async(manager, 'alice', 'one')
    assert gate.entered.wait(2)
    assert not held(manager.lock)
    session = manager.get('alice')
    assert not held(session.condition)
    assert manager.get('bob') is not session
    assert session.snapshot()['active_turn'] is None
    with pytest.raises(ChatError, match='progress'):
        manager.submit('alice', 'two')
    gate.release.set()
    thread.join(2)
    assert outcome['result']['turn_id'] == session.active
    assert [e['type'] for e in session.events] == ['turn_start']
    assert session.process.prompts == ['one'] and not session.starting


@pytest.mark.parametrize('interrupt', [lambda m: m.reset('alice'), lambda m: m.close()],
                         ids=['reset', 'shutdown'])
def test_reset_or_shutdown_during_spawn_closes_new_process(manager, interrupt):
    manager.factory = gate = Gate()
    thread, outcome = submit_async(manager, 'alice', 'one')
    assert gate.entered.wait(2)
    session = manager.get('alice')
    interrupt(manager)
    assert 'alice' not in manager.sessions
    gate.release.set()
    thread.join(2)
    assert outcome['error'].status == 503
    assert gate.made[0].closed and gate.made[0].prompts == []
    assert [e['type'] for e in session.events] == ['conversation_reset']
    assert not session.starting and not session.active


def test_sweep_skips_session_that_is_spawning(manager):
    manager.factory = gate = Gate()
    thread, outcome = submit_async(manager, 'alice', 'one')
    assert gate.entered.wait(2)
    session = manager.get('alice')
    session.last_used = 0
    manager.stop = OneSweep()
    manager._sweep()
    assert manager.sessions['alice'] is session and not session.failed
    gate.release.set()
    thread.join(2)
    assert session.active == outcome['result']['turn_id']


def test_transport_failure_during_spawn_closes_new_process(manager):
    made = []

    def factory(token, receive, failed):
        made.append(FakeProcess(token, receive, failed))
        failed('Ended')
        return made[-1]

    manager.factory = factory
    with pytest.raises(ChatError, match='session ended') as exc:
        manager.submit('alice', 'one')
    session = manager.get('alice')
    assert exc.value.status == 503 and made[0].closed and made[0].prompts == []
    assert [e['type'] for e in session.events] == ['turn_failure'] and not session.starting
    with pytest.raises(ChatError, match='new conversation'):
        manager.submit('alice', 'two')


class Abort(BaseException):
    pass


@pytest.mark.parametrize('error, raised', [(RuntimeError, ChatError), (Abort, Abort)])
def test_spawn_failure_releases_reservation(manager, error, raised):
    def factory(token, receive, failed):
        raise error()

    manager.factory = factory
    with pytest.raises(raised):
        manager.submit('alice', 'one')
    session = manager.get('alice')
    assert not session.starting and not session.events
    assert session.failed is (error is RuntimeError)


def test_cleanup_runs_once_before_any_spawn(manager, monkeypatch):
    order = []

    def cleanup():
        order.append('cleanup')
        time.sleep(0.05)

    monkeypatch.setattr(chat, 'cleanup_orphans', cleanup)
    manager.factory = lambda *args: order.append('spawn') or FakeProcess(*args)
    manager.needs_cleanup = True
    threads = [submit_async(manager, user, 'one')[0] for user in ('alice', 'bob')]
    for thread in threads:
        thread.join(2)
    assert order == ['cleanup', 'spawn', 'spawn'] and not manager.needs_cleanup


def test_failed_cleanup_blocks_spawn_and_is_retried(manager, monkeypatch):
    def cleanup():
        raise RuntimeError('sandbox down')

    spawned = []
    monkeypatch.setattr(chat, 'cleanup_orphans', cleanup)
    manager.factory = lambda *args: spawned.append(args) or FakeProcess(*args)
    manager.needs_cleanup = True
    with pytest.raises(ChatError, match='unavailable') as exc:
        manager.submit('alice', 'one')
    assert exc.value.status == 503 and not spawned and manager.needs_cleanup


def test_process_close_from_two_threads_tears_down_once(monkeypatch):
    import socket
    from types import SimpleNamespace

    local, remote = socket.socketpair()
    commands = []

    def exec_run(argv, **kwargs):
        commands.append(argv[-1])
        time.sleep(0.05)
        return SimpleNamespace(exit_code=0)

    container = SimpleNamespace(id='container', status='running', exec_run=exec_run)
    api = SimpleNamespace(exec_create=lambda *a, **k: {'Id': 'exec'},
                          exec_start=lambda *a, **k: local)
    monkeypatch.setattr(chat.sandbox, '_client', lambda: SimpleNamespace(api=api,
        containers=SimpleNamespace(get=lambda name: container), close=lambda: None))
    process = chat.ClaudeProcess('test', lambda m: None, lambda message: None)
    threads = [threading.Thread(target=process.close) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    remote.close()
    assert len([command for command in commands if 'kill -TERM' in command]) == 1
