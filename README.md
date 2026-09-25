# assistant-agent

A small local web app that connects a Google account and stores **read-only**
credentials for Gmail and Google Calendar, so the rest of the agent can use them
later without re-solving auth.

Open `http://localhost:8000`, click **Connect a Google Account**, approve the
consent screen, and a refreshable credential lands in `data/tokens.json`.

## Scopes requested

| Scope | Grants |
|---|---|
| `openid` | ID token, used to identify the account |
| `.../auth/userinfo.email` | the account's email address |
| `.../auth/gmail.readonly` | read mail (**restricted** scope) |
| `.../auth/calendar.readonly` | read calendars and events (**sensitive** scope) |

Nothing here can send, delete, or modify anything.

## Setup

### 1. Create a Google OAuth client

1. In the [Google Cloud Console](https://console.cloud.google.com), create or pick
   a project.
2. **APIs & Services → Library** → enable the **Gmail API** and the
   **Google Calendar API**.
3. Configure the OAuth consent screen: user type **External** (or **Internal** if
   this is a Google Workspace account — see the warning below). Add the four
   scopes above, and add your own address under **Test users**.
4. **Credentials → Create credentials → OAuth client ID → Web application.**
   Add this **exact** authorized redirect URI:

   ```
   http://localhost:8000/auth/google/callback
   ```

   Google matches redirect URIs exactly — no trailing slash, and
   `http://127.0.0.1:8000/...` counts as a *different* URI.

### 2. Configure the app

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"   # -> SESSION_SECRET
```

Fill `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `SESSION_SECRET` into `.env`.
Both `.env` and `data/` are gitignored.

### 3. Run

```bash
uv sync
uv run assistant-agent serve
```

Then open **<http://localhost:8000>** — use `localhost`, not `127.0.0.1`, or the
session cookie won't be sent back to the callback and the CSRF check will fail.

On first connect you'll see a **"Google hasn't verified this app"** interstitial.
That's expected for an unverified personal app: choose **Advanced → Continue**.

## ⚠️ Refresh tokens expire after 7 days while the app is in "Testing"

If the OAuth consent screen's publishing status is **Testing**, Google expires
refresh tokens after 7 days. Everything works, then a week later API calls start
failing with `invalid_grant` — which looks exactly like a bug. Options:

1. **Workspace account → user type "Internal"** — no expiry, no unverified-app
   screen, no test-user list. Best if available.
2. **Publish the app ("In production")** — refresh tokens stop expiring. You stay
   unverified (still click through the interstitial, capped at ~100 users), which
   is the right trade for a personal agent.
3. Stay in Testing and reconnect weekly.

The app handles this gracefully either way: a dead refresh token shows a
**Needs reconnect** badge rather than crashing.

## Commands

```bash
uv run assistant-agent serve      # run the web app (default)
uv run assistant-agent accounts   # list connected accounts and their scopes
uv run assistant-agent verify     # make a real Gmail + Calendar call
uv run assistant-agent export-2026  # export the connected account's 2026 data
uv run assistant-agent export-2026 --calendar-only  # export only 2026 Calendar events
```

`verify` is the proof that the stored credentials actually work — it refreshes
them if needed, prints your Gmail address and message count via
`users.getProfile`, and lists your next three calendar events.

`export-2026` saves full Gmail message JSON to `data/emails/2026/<id>.json`
and Calendar event JSON to `data/calendar/2026/<calendar-hash>/<event-hash>.json`.
Each event file contains its source `calendarId` alongside the event. The UTC
window is January 1, 2026 (inclusive) through January 1, 2027 (exclusive).
Calendar events that overlap the window, including expanded recurring instances,
are included. Spam, Trash, cancelled events, and separately fetched attachment
bytes are excluded. The command limits all API requests to four in flight and
two starts per second. It retries transient errors, skips saved records on
reruns, and writes `data/export-2026-report.json` with counts and incomplete
work. A partial run exits with status 1; rerun the command to resume. Use
`--email ADDRESS` if more than one account is connected, and `--output PATH` to
choose a different output directory.

Add `--calendar-only` to skip Gmail entirely, including its message listing.
Calendar-only runs leave existing email files and the full-export report alone
and write `data/export-2026-calendar-report.json` instead.

## Where credentials live

`data/tokens.json`, keyed by account email (directory `0700`, file `0600`,
written atomically).

The app's own `client_id` / `client_secret` are deliberately **not** written
there. `Credentials.to_json()` includes them, so they're stripped on save and
merged back in from `.env` on load. `.env` stays the single source of truth for
the app secret, and a leaked `data/tokens.json` is not a complete credential on
its own.

## Layout

```
src/assistant_agent/
├── config.py         # .env settings, scopes, paths, oauthlib flags
├── store.py          # TokenStore: save/load/load_refreshed/list/delete
├── google_oauth.py   # flow construction, code exchange, identity, revoke
├── web.py            # FastAPI app and routes
├── cli.py            # serve / accounts / verify
└── templates/        # base.html, index.html, connected.html
```

| Route | |
|---|---|
| `GET /` | connect page, or the connected-accounts page |
| `GET /auth/google/start` | begins the flow |
| `GET /auth/google/callback` | validates `state`, exchanges the code, stores tokens |
| `POST /disconnect/{email}` | revokes with Google, then deletes locally |
| `GET /healthz` | liveness |

## Follow-up: wider scopes for MCP servers

The app currently requests **read-only** scopes only. That's enough to read mail
and calendars, but not enough for a Gmail/Calendar MCP server that exposes write
tools (send mail, create events).

There are no MCP-specific Google scopes — an MCP server is an ordinary client of
the same REST APIs, so it uses the scopes below. Pick by capability, not by
client.

### Gmail

| Scope | Grants | Tier |
|---|---|---|
| `gmail.readonly` | read messages + settings — **currently used** | Restricted |
| `gmail.metadata` | headers/labels only, no message bodies | Restricted |
| `gmail.send` | send only, cannot read | Sensitive |
| `gmail.labels` | see and edit labels | Sensitive |
| `gmail.compose` | manage drafts and send | Restricted |
| `gmail.modify` | read, compose, send, label — all but permanent delete | Restricted |
| `https://mail.google.com/` | full access including permanent delete | Restricted |

`gmail.modify` covers search/read/draft/send/label in one scope. For read-and-send
without mutation, `gmail.readonly` + `gmail.send` is tighter. Avoid
`https://mail.google.com/` — it adds permanent deletion for no benefit.

### Calendar

| Scope | Grants | Tier |
|---|---|---|
| `calendar.readonly` | read all calendars — **currently used** | Sensitive |
| `calendar.events.readonly` | read events only | Sensitive |
| `calendar.freebusy` | availability only | Sensitive |
| `calendar.events` | view and edit events on all calendars | Sensitive |
| `calendar.events.owned` | create/change/delete events on calendars you own | Sensitive |
| `calendar` | full, including creating and deleting calendars | Sensitive |

`calendar.events` is the right one for scheduling. Full `calendar` is for managing
calendars themselves, not events.

### What changing scopes involves

1. Edit `SCOPES` in `src/assistant_agent/config.py`. (Possible improvement: make
   this `.env`-configurable so read-only and MCP-capable grants can differ per
   environment, without a code change.)
2. Add the same scopes under **Data Access** in the Cloud Console, or the consent
   screen silently won't offer them.
3. Reconnect the account. Scopes are baked into the grant, so an existing token in
   `data/tokens.json` cannot be widened — but the app already sends
   `prompt=consent` and `include_granted_scopes=true`, so clicking **Connect**
   again performs the incremental-auth upgrade.

Note that Gmail write scopes (`gmail.modify`, `gmail.compose`) are *restricted*,
the same tier as `gmail.readonly` — so nothing changes for an unverified personal
app. It only matters if this is ever published for real users, where restricted
scopes require a CASA security assessment. Calendar write stays *sensitive*, a
lower bar.
