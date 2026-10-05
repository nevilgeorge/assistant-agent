"""Live-only Claude conversations, owned independently of HTTP connections."""
from __future__ import annotations

import codecs
import json
import logging
import os
import shlex
import struct
import socket
import threading
import time
import uuid
from collections import deque

from assistant_agent import sandbox

logger = logging.getLogger(__name__)


class ChatError(RuntimeError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


class DockerFrames:
    """Incremental Docker non-TTY stdout/stderr demultiplexer."""
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        while len(self.buffer) >= 8:
            stream, length = self.buffer[0], struct.unpack('>I', self.buffer[4:8])[0]
            if stream not in (1, 2) or length > 16 * 1024 * 1024:
                raise ValueError('Invalid Docker stream frame')
            if len(self.buffer) < 8 + length:
                break
            payload = bytes(self.buffer[8:8 + length])
            del self.buffer[:8 + length]
            yield stream, payload


class ClaudeProcess:
    def __init__(self, token, receive, failed):
        self.client = sandbox._client()
        self.closed = False
        self.close_lock = threading.Lock()
        try:
            self._open(token, receive, failed)
        except Exception:
            if hasattr(self, 'socket'):
                self.close()
            else:
                self.client.close()
            raise

    def _open(self, token, receive, failed):
        self.container = self.client.containers.get(sandbox.container_name())
        if self.container.status != 'running':
            raise sandbox.SandboxError('Sandbox is not running')
        self.path = f'/tmp/assistant-chat-{token}.pid'
        argv = ['claude', '-p', '--input-format', 'stream-json', '--output-format',
                'stream-json', '--verbose', '--include-partial-messages',
                '--no-session-persistence', '--restricted', '--tools', '',
                '--disallowedTools', 'mcp__*']
        inner = f'echo $$ > {shlex.quote(self.path)}; exec {shlex.join(argv)}'
        self.exec_id = self.client.api.exec_create(
            self.container.id, ['bash', '-lc', f'exec setsid --wait bash -lc {shlex.quote(inner)}'],
            stdin=True, stdout=True, stderr=True, tty=False, user='agent',
            workdir=sandbox.WORKSPACE)['Id']
        self.socket = self.client.api.exec_start(self.exec_id, socket=True, tty=False)
        # Docker SDK wraps the underlying socket; use its public IO interface where possible.
        self.io = getattr(self.socket, '_sock', self.socket)
        self.io.settimeout(None)
        self.closed = False
        ready = self.container.exec_run(['bash', '-lc',
            f'for i in {{1..100}}; do test -s {shlex.quote(self.path)} && exit 0; '
            'sleep 0.05; done; exit 1'], user='agent')
        if ready.exit_code:
            self.close()
            raise sandbox.SandboxError('Claude process did not initialize')
        self.write_lock = threading.Lock()
        self.thread = threading.Thread(target=self._read, args=(receive, failed), daemon=True)
        self.thread.start()

    def send(self, prompt):
        message = {'type': 'user', 'message': {'role': 'user', 'content': prompt}}
        with self.write_lock:
            self.io.sendall((json.dumps(message) + '\n').encode())

    def _read(self, receive, failed):
        frames = DockerFrames()
        decoder = codecs.getincrementaldecoder('utf-8')()
        pending = ''
        try:
            while not self.closed:
                data = self.io.recv(65536)
                if not data:
                    raise EOFError('Claude stream closed')
                for stream, payload in frames.feed(data):
                    if stream == 2:
                        # Drain diagnostics without exposing prompts or credentials in logs.
                        continue
                    pending += decoder.decode(payload)
                    if len(pending) > 2 * 1024 * 1024:
                        raise ValueError('Claude protocol line exceeds limit')
                    while '\n' in pending:
                        line, pending = pending.split('\n', 1)
                        if line.strip():
                            receive(json.loads(line))
        except Exception:
            if not self.closed:
                logger.warning('Claude transport failed')
                failed('The assistant session ended. Start a new conversation.')

    def close(self):
        # Sessions close their process from several threads; only the first caller tears down.
        with self.close_lock:
            if self.closed:
                return
            self.closed = True
        try:
            # Kill the recorded process group, including any remaining descendants.
            self.container.exec_run(['bash', '-lc',
                f'p=$(cat {shlex.quote(self.path)} 2>/dev/null); '
                'case "$p" in ""|*[!0-9]*) exit 0;; esac; '
                'kill -TERM -- "-$p" 2>/dev/null; sleep 0.2; '
                'kill -KILL -- "-$p" 2>/dev/null; '
                f'rm -f {shlex.quote(self.path)}'], user='agent')
        except Exception:
            logger.warning('Could not terminate Claude process group')
        finally:
            try:
                self.io.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.socket.close()
            self.client.close()


def cleanup_orphans():
    """Retire process groups left by an earlier app instance (single-worker deployment)."""
    sandbox.run("for f in /tmp/assistant-chat-*.pid; do "
                '[ -f "$f" ] || continue; p=$(cat "$f"); '
                'case "$p" in ""|*[!0-9]*) continue;; esac; '
                'if tr "\\0" " " < /proc/"$p"/cmdline 2>/dev/null | '
                'grep -q -- --input-format; then '
                'kill -KILL -- "-$p" 2>/dev/null; fi; rm -f "$f"; done')


class Conversation:
    def __init__(self):
        self.id = uuid.uuid4().hex
        self.transcript = []
        self.events = deque(maxlen=2048)
        self.sequence = 0
        self.active = None
        self.starting = False
        self.failed = False
        self.process = None
        self.timer = None
        self.last_used = time.monotonic()
        self.bytes = 0
        self.text_streamed = False
        self.turn_started = None
        self.condition = threading.Condition(threading.RLock())

    def emit(self, kind, **data):
        self.sequence += 1
        event = dict(type=kind, conversation_id=self.id, turn_id=self.active,
                     sequence=self.sequence, **data)
        self.events.append(event)
        self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return dict(conversation_id=self.id, transcript=[dict(m) for m in self.transcript],
                        sequence=self.sequence, active_turn=self.active, failed=self.failed)

    # The condition guards in-memory state only. Process I/O (spawn, send, close) always runs
    # after it is released, because the event loop takes it on every stream poll.

    def _fail_locked(self, message: str) -> None:
        """State half of fail(); the caller holds the condition and closes the process after."""
        if self.failed:
            return
        self.failed = True
        logger.info('Claude conversation %s failed', self.id)
        if self.timer:
            self.timer.cancel()
        self.emit('turn_failure', message=message)
        self.active = None

    def fail(self, message: str, turn_id: str | None = None) -> None:
        with self.condition:
            if self.failed or (turn_id is not None and self.active != turn_id):
                return
            self._fail_locked(message)
            process = self.process
        if process:
            process.close()

    def receive(self, message):
        with self.condition:
            if not self.active or self.failed:
                return
            self._receive_locked(message)
            process = self.process if self.failed else None
        if process:
            process.close()

    def _receive_locked(self, message) -> None:
        kind = message.get('type')
        if kind == 'stream_event':
            event = message.get('event', {})
            delta = event.get('delta', {})
            if event.get('type') == 'content_block_delta' and delta.get('type') == 'text_delta':
                self.append(delta.get('text', ''))
                self.text_streamed = True
        elif kind == 'assistant' and not self.text_streamed:
            for block in message.get('message', {}).get('content', []):
                if block.get('type') == 'text':
                    self.append(block.get('text', ''))
        elif kind == 'result':
            if message.get('is_error') or message.get('subtype') != 'success':
                self._fail_locked('The assistant could not complete the turn. Start a new conversation.')
                return
            if not self.transcript[-1]['text'] and message.get('result'):
                self.append(message['result'])
            if self.failed:
                return
            self.timer.cancel()
            logger.info('Claude conversation %s completed turn in %.2fs',
                        self.id, time.monotonic() - self.turn_started)
            self.emit('turn_completion')
            self.active = None
            self.last_used = time.monotonic()

    def append(self, text):
        if self.failed:
            return
        size = len(text.encode())
        if self.bytes + size > 2 * 1024 * 1024:
            self._fail_locked('Conversation limit reached. Start a new conversation.')
            return
        self.bytes += size
        self.transcript[-1]['text'] += text
        self.emit('assistant_delta', text=text)

    def retire(self, reason: str = 'Conversation reset.') -> ClaudeProcess | None:
        """State half of close(); returns the process for the caller to close unlocked."""
        with self.condition:
            if self.timer:
                self.timer.cancel()
            self.failed = True
            self.emit('conversation_reset', message=reason)
            logger.info('Claude conversation %s retired', self.id)
            self.active = None
            return self.process

    def close(self, reason='Conversation reset.'):
        process = self.retire(reason)
        if process:
            process.close()


class SessionManager:
    def __init__(self, process_factory=ClaudeProcess):
        self.factory = process_factory
        self.sessions = {}
        # Like Conversation.condition, this guards state only and is never held across process I/O.
        self.lock = threading.RLock()
        # Serializes orphan cleanup, which would kill any process spawned before it finishes.
        self.cleanup_lock = threading.Lock()
        self.stop = threading.Event()
        self.needs_cleanup = False
        self.max_sessions = int(os.getenv('CHAT_MAX_SESSIONS', '4'))
        self.idle_seconds = int(os.getenv('CHAT_IDLE_SECONDS', '1800'))
        if self.max_sessions < 1 or self.idle_seconds < 1:
            raise ValueError('Chat capacity and idle timeout must be positive')

    def start(self):
        try:
            cleanup_orphans()
        except Exception:
            self.needs_cleanup = True
            logger.warning('Sandbox unavailable during chat startup cleanup')
        self.sweeper = threading.Thread(target=self._sweep, daemon=True)
        self.sweeper.start()

    def _sweep(self):
        while not self.stop.wait(30):
            expired = []
            with self.lock:
                for user, session in list(self.sessions.items()):
                    with session.condition:
                        if (not session.active and not session.starting
                                and time.monotonic() - session.last_used >= self.idle_seconds):
                            self.sessions.pop(user)
                            expired.append(session.retire('Conversation expired after inactivity.'))
            _close_all(expired)

    def get(self, user):
        with self.lock:
            if user not in self.sessions:
                if len(self.sessions) >= self.max_sessions:
                    raise ChatError('All conversation slots are busy. Try again later.', 503)
                self.sessions[user] = Conversation()
            return self.sessions[user]

    def submit(self, user, prompt, conversation_id=None):
        with self.lock:
            session = self.get(user)
            with session.condition:
                if conversation_id and conversation_id != session.id:
                    raise ChatError('Conversation reset. Reload before sending.')
                if session.failed:
                    raise ChatError('Start a new conversation before sending.')
                if session.active or session.starting:
                    raise ChatError('A response is already in progress.')
                if session.bytes + len(prompt.encode()) > 2 * 1024 * 1024:
                    raise ChatError('Conversation limit reached. Start a new conversation.')
                process = session.process
                if process:
                    result = self._begin_turn(session, prompt)
                else:
                    # Reserve the session so the spawn can run with no lock held.
                    session.starting = True
        if not process:
            process, result = self._spawn(session, prompt)
        try:
            process.send(prompt)
        except Exception:
            session.fail('The assistant session ended. Start a new conversation.')
        return result

    def _spawn(self, session: Conversation, prompt: str) -> tuple[ClaudeProcess, dict]:
        """Start the Claude process for a reserved session with no chat lock held, then begin the turn."""
        process = None
        begun = False
        try:
            try:
                with self.cleanup_lock:
                    if self.needs_cleanup:
                        cleanup_orphans()
                        self.needs_cleanup = False
                process = self.factory(session.id, session.receive, session.fail)
            except Exception:
                with session.condition:
                    session.failed = True
                raise ChatError('The assistant is unavailable. Start a new conversation.', 503) from None
            with session.condition:
                session.process = process
                # The session may have failed or been reset while the process was starting.
                if session.failed:
                    raise ChatError('The assistant session ended. Start a new conversation.', 503)
                result = self._begin_turn(session, prompt)
                begun = True
                return process, result
        finally:
            with session.condition:
                session.starting = False
            if process and not begun:
                process.close()

    def _begin_turn(self, session: Conversation, prompt: str) -> dict:
        """Record the turn and arm its timeout; the caller holds session.condition."""
        session.active = uuid.uuid4().hex
        session.text_streamed = False
        session.turn_started = time.monotonic()
        logger.info('Claude conversation %s started turn', session.id)
        session.bytes += len(prompt.encode())
        session.transcript.extend([dict(role='user', text=prompt, turn_id=session.active),
                                   dict(role='assistant', text='', turn_id=session.active)])
        session.emit('turn_start', text=prompt)
        turn_id = session.active
        session.timer = threading.Timer(sandbox.REQUEST_TIMEOUT_SECONDS,
            lambda: self._timeout(session, turn_id))
        session.timer.daemon = True
        session.timer.start()
        return dict(conversation_id=session.id, turn_id=turn_id)

    def _timeout(self, session, turn_id):
        session.fail('The assistant timed out. Start a new conversation.', turn_id=turn_id)

    def reset(self, user):
        with self.lock:
            session = self.sessions.pop(user, None)
            process = session.retire() if session else None
        _close_all([process])

    def close(self):
        self.stop.set()
        if hasattr(self, 'sweeper'):
            self.sweeper.join(timeout=2)
        with self.lock:
            processes = [session.retire() for session in self.sessions.values()]
            self.sessions.clear()
        _close_all(processes)


def _close_all(processes: list[ClaudeProcess | None]) -> None:
    for process in processes:
        if process:
            process.close()
