# Running agent CLIs inside Docker

Last updated: 2026-10-05.

This is the living design and context document for how assistant-agent communicates with agent CLIs inside Docker. Update it when the transport, session lifecycle, recovery, or storage design changes. It describes the implemented behavior and explicitly separates future ideas from completed work.

## Phase 1: Long-lived Claude Code process inside an existing long-running Docker container. 

### Context for a new chat

The first major change replaced the web chat's one-shot Claude invocation with a long-lived Claude Code process inside an existing long-running Docker container. The assistant-agent service sends each prompt to that process's stdin and continuously reads structured stdout through a non-TTY Docker exec socket. Multiple turns reuse the same Claude process and its in-memory context.

The implementation is scoped to Claude Code, with tools and MCP disabled. It does not use ACP, an HTTP server inside the container, or a WebSocket to the CLI. Codex support and workspace tools were intentionally left out. Browser streaming uses Server-Sent Events (SSE), independently of the Docker connection.

Transcripts currently live only in assistant-agent memory. PostgreSQL stores authentication data, but conversation persistence has only been discussed, not implemented. The latest proposed next step is to persist normalized messages from the assistant-agent service, keeping database access outside the sandbox. Saving a transcript and recovering Claude's session context are separate problems.

### Why this change was needed

Previously, the web chat used `sandbox.ask()` to execute a separate Claude command for each prompt and wait for its complete output. The goal was to keep a Claude session alive across turns and render responses incrementally in the web UI without introducing a container-side server or ACP for the MVP.

The container lifetime, Claude process lifetime, and browser connection lifetime are separate:

- Docker Compose owns the long-running container.
- assistant-agent owns each user's Claude process and conversation state.
- A browser subscribes to the conversation; disconnecting it does not stop the turn or Claude process.

### Communication flow

```text
Browser
  | POST /api/message: prompt + conversation ID
  v
assistant-agent / SessionManager
  | newline-delimited stream-json user message -> stdin
  v
Persistent non-TTY Docker exec -> Claude Code process
  | stream-json events <- stdout
  v
ClaudeProcess reader -> Conversation state + bounded event buffer
  | GET /api/conversation/stream: SSE events
  v
Browser renders assistant text incrementally
```

Each user has an independent conversation and CLI process inside the same sandbox container. Tabs and logins for the same authenticated internal `user_id` share that user's conversation. This is process-level separation within a shared container, not a separate Docker sandbox per user.

#### Starting the container and CLI

The `sandbox-1` service in the local and production Compose files runs `sleep infinity` after marking itself ready. Its default container name is `assistant-agent-sandbox-1`, configurable in the app through `SANDBOX_CONTAINER`. assistant-agent connects to the Docker daemon using the Docker Python SDK; it does not create the container on a chat request.

FastAPI's lifespan starts `SessionManager`, cleans up orphaned chat processes from an earlier app instance, and starts an idle-session sweeper. If the sandbox is unavailable at startup, cleanup is retried before a CLI can be spawned.

A conversation object can exist before a CLI process exists. On the first submitted prompt, `SessionManager.submit()` lazily starts `ClaudeProcess` through Docker exec. Later prompts reuse that process. The exec runs as user `agent` in `/workspace`, with stdin, stdout, and stderr attached and `tty=False`.

The current Claude command is:

```bash
claude -p \
  --input-format stream-json \
  --output-format stream-json \
  --verbose \
  --include-partial-messages \
  --no-session-persistence \
  --restricted \
  --tools '' \
  --disallowedTools 'mcp__*'
```

Here, `-p` is used with streaming input: keeping stdin open lets us submit successive turns to the same process. The command is wrapped with `setsid --wait` and a PID file so assistant-agent can terminate the process group and its descendants when retiring the conversation.

#### Sending prompts and reading responses

For each prompt, `ClaudeProcess.send()` writes one JSON object followed by a newline to the open exec socket:

```json
{"type":"user","message":{"role":"user","content":"The user's prompt"}}
```

It does not close stdin after sending. A write lock serializes socket writes. The session manager permits only one active turn per conversation; overlapping submissions receive `409`.

A background reader continuously drains the Docker socket. Docker's non-TTY output has multiplexed stdout/stderr frames with eight-byte headers. `DockerFrames` incrementally separates those frames, including headers or payloads split across socket reads. The reader incrementally decodes UTF-8 stdout and parses newline-delimited Claude JSON events. Stderr is drained but discarded; raw diagnostics and prompts are not logged.

`Conversation` converts Claude events into application events:

- `stream_event` text deltas append to the assistant message and emit `assistant_delta`.
- Full assistant text provides a fallback when no text deltas have been received, avoiding duplicated output.
- A successful `result` ends the turn and emits `turn_completion`; it does not close the CLI process.
- Error results, malformed output, unexpected EOF, or transport failures fail the conversation and require a new one. Partial assistant text remains in the live transcript.

Conversation and manager locks protect in-memory state. Process spawning, sending, and closing occur outside those locks so Docker I/O does not block browser stream polling or unrelated conversations.

#### Delivering output to the web UI

| Endpoint | Purpose |
| --- | --- |
| `POST /api/message` | Accept a prompt; return `202` with conversation and turn IDs. |
| `GET /api/conversation` | Return the current in-memory transcript, sequence, active turn, and failure state. |
| `GET /api/conversation/stream?conversation_id=...&after=...` | Stream normalized events using SSE. |
| `POST /api/conversation/reset` | Retire the current conversation and terminate its CLI process. |

Endpoints use the authenticated browser session. Mutations also require a CSRF token. Events carry conversation ID, turn ID, and a monotonically increasing sequence; SSE uses that sequence as the event ID. Reconnection supports `Last-Event-ID`. If the requested cursor falls outside the replay window or the conversation ID is stale, the server sends `reload` so the browser fetches a fresh snapshot.

The browser renders messages as plain text and updates the assistant bubble as deltas arrive. Its HTTP/SSE connection does not own the Docker socket. The reader continues consuming Claude output if a tab closes or its network connection drops; a returning tab can load the current snapshot and subscribe again while the conversation remains alive.

### Lifecycle and current limits

- Each turn has a 120-second deadline. Timeout fails the conversation and terminates its process. Timers are tied to turn IDs so an old timeout cannot terminate a later turn.
- `CHAT_IDLE_SECONDS` defaults to `1800`. A sweeper checks every 30 seconds and retires inactive conversations; it skips active turns and processes being started.
- `CHAT_MAX_SESSIONS` defaults to `4`. The cap includes conversation objects that have not started a CLI yet.
- Each conversation allows 2 MiB of cumulative prompt/response text. The event replay buffer holds the latest 2,048 events.
- Reset, account disconnect, idle expiry, and app shutdown retire the relevant process. Process teardown uses the recorded process group, then closes the exec socket and Docker client.
- Startup cleanup removes orphaned chat process groups from an earlier app instance.
- Run exactly one app worker/replica per sandbox. Ownership is process-local, and startup cleanup would interfere with another app instance's processes.

### Persistence and recovery boundaries

`--no-session-persistence` is still enabled. The running Claude process retains context between turns, but this implementation does not use Claude's on-disk session persistence or resume support. App restart loses the in-memory transcript, and startup cleanup terminates surviving chat processes. Container restart or CLI failure also loses the live session. There is no automatic prompt retry, transcript reconstruction, or session resume.

Removing `--no-session-persistence` alone would not implement recovery: assistant-agent would still need to track Claude session IDs, retain the session files, and explicitly resume the right session. Browser reconnection currently recovers access to a live conversation, not a failed CLI session.

The proposed PostgreSQL follow-up is to write from assistant-agent, where authenticated user identity, conversation IDs, turn IDs, and normalized text are already available. The discussion suggested conversation and message tables, recording user input before sending, periodically checkpointing assistant text, and finalizing messages as completed, failed, or interrupted. This remains a proposal; no transcript schema or writes have been added.

Tools and MCP remain disabled because users share a container and workspace. Enabling file or command tools requires revisiting user isolation first.

### Code map and validation

Paths below are relative to the repository root:

| File | Responsibility |
| --- | --- |
| `src/assistant_agent/chat.py` | Docker stream framing, persistent Claude process, conversation events/state, session manager, cleanup, and limits. |
| `src/assistant_agent/web.py` | App lifecycle, authenticated chat endpoints, and SSE delivery. |
| `src/assistant_agent/templates/connected.html` | Chat UI, prompt submission, snapshot loading, and SSE handling. |
| `src/assistant_agent/sandbox.py` | Shared Docker configuration/client helpers and legacy one-shot helpers. `ask()` remains available but is no longer the web chat path. |
| `compose.yaml` and `deploy/compose.prod.yaml` | Container lifecycle and app configuration. |
| `tests/test_chat.py` | Session/transport tests and opt-in real Docker/Claude integration. |
| `tests/test_web_auth.py` | Chat authentication, isolation, endpoints, and SSE tests. |

The initial implementation was validated against a real Docker container and Claude CLI: two turns reused the same process, Claude recalled context from the first turn, text deltas were emitted, and reset terminated the exec. Automated tests also cover framing, split Unicode/JSON, failure handling, isolation, capacity, timeouts, cleanup, and process-I/O concurrency.

To run the opt-in integration test with Docker and Claude credentials configured:

```bash
CHAT_DOCKER_INTEGRATION=1 uv run pytest -q tests/test_chat.py -k real_claude
```
