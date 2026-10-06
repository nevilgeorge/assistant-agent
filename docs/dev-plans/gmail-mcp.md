# Gmail MCP and session file access

Status: Phase 1 Gmail service implemented; MCP and sandbox integration remain proposed. Last updated: 2026-10-06.

## Decision and scope

Expose read-only Gmail tools from the assistant-agent app over Streamable HTTP MCP. Claude inside Docker connects directly to the existing app listener over a shared Docker network. Google OAuth credentials, refresh logic, and Gmail API calls remain in the app. Use Gmail search to select messages; let the agent explicitly download selected messages when local `ls`, `rg`, or Python analysis is useful.

Avoid downloading the entire roughly 400 MB mailbox at session startup. That size is manageable, but many small objects, cold-start delay, and unused downloads favor selective retrieval. MCP is the tool interface; Gmail supplies the search index.

## Tools

| Tool | Behavior |
| --- | --- |
| `search_emails(query, page_size, cursor?)` | Gmail search; return IDs, thread IDs, headers, snippets, and pagination. |
| `get_email(message_id, format?)` | Return decoded content and attachment metadata directly; optionally raw MIME. No sandbox files. |
| `get_thread(thread_id)` | Return bounded conversation content with explicit truncation and message IDs for individual retrieval. |
| `download_emails(message_ids, format?)` | Write selected messages to the session input directory; return paths, IDs, bytes, and per-message failures. |
| `download_attachment(message_id, attachment_id)` | Explicitly fetch an attachment and return its local path and metadata. |

Default downloads to normalized text containing message/thread IDs, sender, recipients, subject, date, and decoded body. Also support structured JSON and original EML. Attachments are fetched separately. Gmail message listing returns IDs; rich previews require follow-up retrievals with bounded concurrency.

Use explicit message IDs for downloads, not an implicit repeat of a search. Report incomplete pagination and partial failures; never imply a limited result set is exhaustive. Explain to the agent that local `rg` searches only downloaded candidates, so initial Gmail queries should be broad enough. Normalize MIME/HTML before text searches.

## Gmail API

Use the existing `gmail.readonly` scope and authenticated `userId=me` for all calls. Paths below are relative to `https://gmail.googleapis.com/gmail/v1`.

| MCP tool | Gmail API calls |
| --- | --- |
| `search_emails` | `GET /users/me/messages` with `q`, `maxResults`, and optional `pageToken`; then `GET /users/me/messages/{id}?format=metadata` for each returned ID. |
| `get_email` | `GET /users/me/messages/{id}?format=full` for parsed content, or `format=raw` for original MIME. |
| `get_thread` | `GET /users/me/threads/{id}?format=full`. Gmail does not paginate this endpoint; initially return explicit truncation and message IDs for individual retrieval instead of implementing a thread cursor. |
| `download_emails` | Fetch each selected ID through `messages.get`: `full` for normalized text/JSON, `raw` for EML; write files and return paths. |
| `download_attachment` | `GET /users/me/messages/{messageId}/attachments/{attachmentId}`; decode base64url bytes and write the file. |

`messages.list` returns only message/thread IDs, with no preview or relevance score. Bound search to one page per call: default 20 messages, maximum 50, and no automatic next-page fetch. Retrieve selected headers (`From`, `To`, `Subject`, `Date`) and a bounded snippet for those hits. Return a query-bound continuation cursor, estimated total, and explicit metadata failures; narrow broad queries before paging further.

Fetch metadata concurrently with an initial limit of five in-flight Gmail requests per user shared across tools, plus an app-wide cap; run synchronous SDK calls through bounded workers with separate thread-owned transports and refresh credentials before fan-out. Start with individual requests, connection reuse, and a 15-second search work deadline including bounded retries with backoff/jitter. Stop scheduling new requests/retries at the deadline and drain active worker calls before returning; this is not a strict response-time limit. Apply rate/quota controls separately from concurrency, preserve listing order, and return partial failures explicitly. Defer HTTP batching until measurements justify it.

Traverse MIME parts and decode bodies; externally stored body parts may also require `attachments.get`. Retrieval and download calls fetch from Gmail each time in the first implementation; caching remains follow-up work.

## Session lifecycle and files

1. Authenticate the internal user and assign a dedicated sandbox to the conversation. Replace the current shared-user container model before exposing user tokens and files.
2. Create `/srv/assistant-agent/sessions/<session-id>/input/` and mount it at `/input:ro`; keep scratch/output space separate at `/workspace`.
3. Give the app writable access to the host session directory. Docker bind sources must use host paths, even when requested by the containerized app.
4. Issue a conversation-scoped app token and launch Claude with the MCP configuration below. Check MCP discovery/readiness before reporting Gmail access as ready.
5. Download tools write temporary files and atomically publish completed files. The running sandbox sees additions through its existing read-only bind mount; no remount or container recreation is needed.
6. Fetch from Gmail on each retrieval or download call. Write each download call into a new server-generated subdirectory so repeated downloads preserve earlier files. Session files are explicit tool outputs, not a retrieval cache.
7. On reset, expiry, or teardown, revoke the token, stop the agent, destroy the assigned container, and clean up session files. Reconcile abandoned resources after app restart.

Create containers after assignment initially. A future warm pool can pre-mount a unique empty directory before assignment; start the agent only after assignment. Give each container one user assignment during its lifetime—clearing input files does not clear copied data or processes.

Files already materialized remain fixed for that session; subsequent explicit retrievals or downloads may fetch newer data. Live Gmail searches can still change during a session: this is not a transactionally frozen mailbox snapshot.


## MCP registration inside the sandbox

Provide `/run/assistant/mcp.json`:

```json
{
  "mcpServers": {
    "gmail": {
      "type": "http",
      "url": "http://app:8000/mcp/gmail",
      "headers": {
        "Authorization": "Bearer ${ASSISTANT_MCP_TOKEN}"
      }
    }
  }
}
```

Pass the token through Docker exec's environment, not shell arguments or logs. Add `--mcp-config /run/assistant/mcp.json --strict-mcp-config` to the existing streaming Claude launch. Remove `--disallowedTools 'mcp__*'` and explicitly preauthorize the five `mcp__gmail__...` tools with `--allowedTools`.

Configure built-in local analysis separately from MCP:

| Configuration | Purpose |
| --- | --- |
| `--restricted` | Ignore normal user/project settings and confine built-in file tools to working directories; command tools require explicit opt-in. |
| `--tools "Read,Glob,Grep,Bash"` | Replace `--tools ''` to enable file reading/search and explicitly opt Bash back in. Omit Edit/Write. This flag controls built-ins, not MCP tools. |
| `--add-dir /input` | Add the existing input mount to working directories when launching from `/workspace`; this does not make it read-only. |
| `--allowedTools` | Preapprove the five Gmail tools plus `Read(//input/**)`, `Bash(ls *)`, and `Bash(rg *)` for local analysis. |
| `--permission-mode dontAsk` | Deny operations that would require a prompt, while allowing preapproved and normally permission-free operations. Do not use global permission bypass. |
| Docker `/input:ro` | Enforce read-only input files at the OS level, including for shell subprocesses. |

Bash patterns are permission checks, not filesystem isolation: `Bash(rg *)` permits arbitrary arguments, including preprocessor options that can run other programs, and file-tool rules do not constrain arbitrary scripts. Docker mounts, permissions, and container isolation remain the enforcement boundary. Broad Python approval would authorize arbitrary code inside that boundary and is a separate decision. Validate allowed reads/searches, denied writes, and noninteractive permission failures against the image's pinned CLI version.

Claude discovers tool descriptions and schemas from the endpoint automatically. No Gmail SDK, local MCP server, or shim is needed inside the sandbox.

## Authentication and accepted risks

- Generate a fresh opaque token with `secrets.token_urlsafe(48)` when launching the conversation's agent. Store only `hashlib.sha256(raw_token.encode("utf-8")).hexdigest()`, reusing the existing `web_store.token_hash()` pattern. SHA-256 is appropriate for this high-entropy random token; password hashing and a salt are unnecessary. The stored hash is not a usable bearer credential.
- Add a separate `SandboxAccessToken` table with `id`, uniquely indexed `token_hash` (`VARCHAR(64)`), `user_id` (foreign key), `conversation_id`, `sandbox_id`, `created_at`, `expires_at`, and nullable `revoked_at`. Do not add the token to `WebSession`: browser-login and agent-conversation lifetimes differ. Initially, the tool allowlist lives in server code, and `conversation_id` is a correlation identifier because conversations are in memory.
- Pass the raw token as `ASSISTANT_MCP_TOKEN` in the Docker exec environment for the Claude process, not the container-wide environment, image, or shell command. Claude expands it into the MCP Authorization header; descendants may inherit it. Do not log it or persist the raw value in the database.
- On every request, hash the bearer token, look up the grant, and reject missing, expired, revoked, or inactive-conversation grants. Authorize the requested tool independently of MCP protocol session bookkeeping. Revoke grants on launch failure and conversation teardown, invalidate abandoned grants during restart recovery, and issue a new token when replacing the agent process; hashes cannot recover old tokens.
- Resolve credentials, Gmail `me`, and output directory from that identity. Do not accept user IDs, sandbox IDs, or arbitrary output paths as tool arguments. Keep browser cookies and Google tokens outside the sandbox.
- Treat the app token as accessible to sandbox code. It permits only this conversation's Gmail operations, never general app or Docker control. Enforce request, concurrency, response-size, and download quotas; sanitize attachment names and prevent path traversal. Exclude secrets and email bodies from routine logs.
- **Accepted MVP exposure:** the sandbox can reach other routes and listening ports on the shared app interface. Docker service discovery, `expose`, and omitted published ports do not enforce MCP-only access. Existing route authentication remains essential.
- **Accepted transport limitation:** plain HTTP over a Docker bridge is not encrypted. Private networking is not authentication. Sandbox-to-sandbox, database, host-service, and EC2 metadata reachability must be reviewed explicitly; do not describe the shared network as a complete isolation boundary.
- The app currently holds the Docker socket, increasing the impact of app compromise. Never mount it into the sandbox.

Direct HTTP is the simplest supported transport. A reverse proxy on separate sandbox/backend networks is the next option for MCP-only routing and TLS; it helps only if direct app access is removed. A dedicated MCP listener requires actual interface/firewall restrictions. A stdio shim adds packaging and protocol translation and does not inherently improve credential isolation; reserve it for client compatibility or a Unix-socket-only design.

## Development Plan

Use each phase below as the scope for a separate Codex implementation plan. Phase 1 is implemented; phases 2–6 are pending. Include the phase's tests with its implementation; phase 6 verifies the integrated system. Phases 1 and 2 can be implemented independently; phase 3 depends on phase 2, phase 4 on phases 1–3, and phase 5 on phases 2–4. Keep agent tools disabled until phase 5.

| Phase | Design sections |
| --- | --- |
| 1. Gmail service | [Tools](#tools), [Gmail API](#gmail-api) |
| 2. Dedicated sandbox lifecycle | [Session lifecycle and files](#session-lifecycle-and-files), [Authentication and accepted risks](#authentication-and-accepted-risks) |
| 3. Conversation access tokens | [Authentication and accepted risks](#authentication-and-accepted-risks) |
| 4. MCP endpoint and downloads | [Tools](#tools), [Gmail API](#gmail-api), [Session lifecycle and files](#session-lifecycle-and-files) |
| 5. Claude registration and permissions | [MCP registration inside the sandbox](#mcp-registration-inside-the-sandbox) |
| 6. End-to-end validation and rollout | [Session lifecycle and files](#session-lifecycle-and-files), [MCP registration inside the sandbox](#mcp-registration-inside-the-sandbox), [Authentication and accepted risks](#authentication-and-accepted-risks) |

### Phase 1 — Gmail service

- Add `gmail_service.py` for search, metadata previews, message/thread retrieval, attachments, and MIME normalization. Reuse `web_store.load_refreshed()` and the bounded Google worker.
- Implement the documented page/response limits, shared per-user and app-wide concurrency controls, deadlines, rate/quota controls, and bounded retries. Return explicit truncation and partial failures. No caching or automatic page prefetch.
- **Complete when:** tests with representative Gmail responses cover pagination, nested MIME/body parts, raw EML, attachments, revoked OAuth, partial failures, and concurrency/deadline behavior without blocking the event loop.

Implemented in `src/assistant_agent/gmail_service.py`, instantiated as `app.state.gmail` in the web lifespan and closed after active operations drain. The async service accepts trusted internal user IDs and exposes `search_emails`, `get_email` (`full` or `raw`), `get_thread`, and `get_attachment`. Results are typed dataclasses; raw MIME and attachment results contain exact bytes. No routes, files, MCP registration, or agent tools are enabled by this phase.

`GmailLimits` supplies constructor-configurable defaults:

| Limit | Default |
| --- | --- |
| Per-user Gmail concurrency | 5, shared across methods |
| App-wide Gmail concurrency | 4, also constrained by the existing shared Google worker |
| Quota-unit budgets | 80 units/second per user; 400 app-wide; one second of burst capacity |
| Work deadlines | 15 seconds for search; 30 for other retrievals, including credential preparation and queueing |
| Per-attempt network timeout | Minimum of 10 seconds and remaining work budget |
| Retries | 3 total SDK attempts; exponential backoff with jitter and `Retry-After` |
| Message body text | 256 KiB of UTF-8 text |
| Serialized normalized response | 1 MiB, measured with compact UTF-8 JSON |
| Thread messages | First 50 in Gmail's returned order; IDs and omission counts identify remaining content |
| Decoded raw message / attachment | 32 MiB; oversized binary results fail instead of being truncated |
| Combined decoded body parts | 32 MiB per message |
| MIME traversal | 30 nested levels, 1,000 parts |
| Search preview | 512 snippet characters; 4 KiB per selected header |

Quota accounting charges each SDK attempt: list costs 5 units, message/attachment retrieval 20, and thread retrieval 40. User quota responses cool down that user's operations; explicit project quota responses cool down all Gmail operations. The SDK's API retries and automatic credential refresh are disabled; the underlying HTTP library may reconnect after connection failures within an SDK attempt. Google changed its documented quotas for newer projects in May 2026, so these budgets must be compared with the deployment project's actual quotas before rollout.

Search continuation cursors are signed and bound to the internal user and exact query; restart invalidates them. Search returns ordered IDs even when preview retrieval fails, separate pagination and metadata-incomplete indicators, and safe per-message errors. Text/metadata/thread truncation is explicit. MIME decoding and HTML normalization use the bounded worker; ordinary attachments are inventoried but not automatically fetched. Declared-size and decoded-size checks bound returned data, not the SDK's buffered upstream HTTP responses. All retrievals fetch anew; the service does not cache Gmail content or write files.

Cancellation stops queued Gmail requests and further preview scheduling, while active synchronous SDK calls drain with their capacity retained. Quota waits release app-wide concurrency slots so a throttled user does not reserve capacity needed by other users. Attached MIME subtrees are excluded from body normalization; filename-less inline text remains readable body content. Service and lifecycle tests cover these behaviors using representative responses and the real SDK's request construction with a fake HTTP transport; live-account validation remains in phase 6.

### Phase 2 — Dedicated sandbox lifecycle

- Move from the shared Compose sandbox to containers created after conversation assignment. Pass explicit container handles through execution, readiness, and teardown; add Docker exec environment support.
- Create session-scoped host directories, app write access, `/input:ro`, and separate `/workspace`. Update local/production Compose configuration for image selection, host paths, and direct app connectivity; preserve resource limits and hardening.
- Handle failed creation, reset, expiry, and restart reconciliation. Destroy containers after assignment ends; do not reuse them across users. Keep existing chat streaming behavior.
- **Complete when:** lifecycle integration tests demonstrate distinct user mounts, successful app-side file publication visible in the sandbox, rejected sandbox writes to `/input`, and cleanup after failures. Existing chat tests pass.

### Phase 3 — Conversation access tokens

- Add the `SandboxAccessToken` model and Alembic migration; implement generation, hash-only storage, validation, expiration, and revocation in `sandbox_access.py`.
- Bind grants to the active user/conversation/container; use server-side tool permissions and destination resolution. Wire revocation into lifecycle failures, teardown, account disconnection, and restart recovery.
- **Complete when:** migration and authorization tests cover valid, missing, expired, revoked, inactive, and cross-user grants. Browser sessions remain independent; raw tokens never enter database records or logs.

### Phase 4 — MCP endpoint and downloads

- Add `gmail_mcp.py` with the official Python SDK's Streamable HTTP ASGI integration and FastAPI lifespan. Expose the five tools at `/mcp/gmail`, authenticating and authorizing every request through phase 3.
- Add `session_files.py` for text/JSON/EML and attachment materialization. Use server-generated per-call directories, atomic publication, byte limits, safe filenames, and explicit per-item outcomes. Fetch again on each call; preserve prior downloads.
- **Complete when:** HTTP MCP tests cover discovery and all tool contracts, unauthorized calls, path traversal, download limits, partial failures, and repeated downloads. Retrieval-only tools create no sandbox files; credentials and output paths come only from the authenticated grant.

### Phase 5 — Claude registration and permissions

- Supply the MCP JSON, inject `ASSISTANT_MCP_TOKEN` into the assigned Claude exec environment, and configure explicit MCP registration/tool permissions plus local read/search access as documented.
- Replace the blanket MCP denial, retain restricted mode, and use noninteractive permission handling. Surface discovery/authentication failures before reporting Gmail readiness; add brief guidance on search versus explicit download and the limits of local search.
- **Complete when:** the pinned CLI discovers and calls the tools from its assigned container, reads downloads with `ls`/`rg`, rejects input writes, and handles unapproved operations without hanging for input. Verify Google credentials are absent from the sandbox. Broad Python approval remains a separate decision.

### Phase 6 — End-to-end validation and rollout

- Exercise authenticated search → preview → retrieval or explicit download → local analysis using a test Google account, including pagination, attachments, OAuth failure, reset, expiry, and app restart.
- Verify two-user isolation and token revocation across the integrated system. Record actual app, database, other-sandbox, host, and metadata-service reachability alongside the accepted direct-listener/plain-HTTP risks.
- Measure cold session startup and search latency under concurrent users; tune limits from evidence. Document required configuration, migration/startup order, operational cleanup, and failure diagnostics without email bodies or secrets. Update the existing Docker lifecycle note to match the implemented architecture.
- **Complete when:** the integrated checks pass in the target Docker setup and deployment instructions are reviewable. Production deployment is a separate explicit action.

Caching, S3 ingestion, warm pools, HTTP batching, gateway/TLS isolation, and broader code-execution permissions remain follow-up work; they are not prerequisites for completing these phases.

## Follow-up: caching and S3 ingestion

Caching is outside the first implementation. Later, cache Gmail content by user/message, deduplicate downloads, and reuse retrieved content between `get_email` and `download_emails`. Define freshness, isolation, size limits, and eviction before adding cache reuse; add cache-specific tests in that follow-up.

The earlier S3 option remains useful for non-email files or exhaustive mailbox analysis: resolve the authenticated user's exact `user-data/<user-id>/` prefix (confirm whether the stated leading `/` is literal), refresh a host cache before a new session, and mount it read-only. Do not refresh files under an active session. One directory per user is sufficient only if refresh cannot overlap an active session; overlapping sessions need immutable generations. Persistent caches need quotas and eviction. Full S3 ingestion is deferred from this MCP implementation.

## References

- [Claude MCP configuration](https://code.claude.com/docs/en/mcp) and [CLI flags](https://code.claude.com/docs/en/cli-reference)
- [Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)
- [Gmail search](https://developers.google.com/workspace/gmail/api/guides/filtering), [message listing](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list), and [usage limits/billing thresholds](https://developers.google.com/workspace/gmail/api/reference/quota)
- [Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/) and [bridge networking](https://docs.docker.com/engine/network/drivers/bridge/)
