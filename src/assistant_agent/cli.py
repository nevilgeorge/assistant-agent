"""Command line entry points: serve the connect page, inspect and verify accounts."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from assistant_agent.config import ConfigError, get_settings
from assistant_agent.store import TokenStore


def _serve(_: argparse.Namespace) -> int:
    import uvicorn

    settings = get_settings()
    print(f"Open {settings.base_url} to connect a Google account.")
    # Import string, not the app object -- otherwise --reload cannot work.
    uvicorn.run(
        "assistant_agent.web:app",
        host=settings.host,
        port=settings.port,
        reload=not settings.is_production,
    )
    return 0


def _accounts(_: argparse.Namespace) -> int:
    accounts = TokenStore().list_accounts()
    if not accounts:
        print("No connected accounts. Run `assistant-agent serve` and connect one.")
        return 0
    for account in accounts:
        print(f"{account.email}  connected {account.connected_at}")
        for scope in account.scopes:
            print(f"    {scope}")
    return 0


def _verify(args: argparse.Namespace) -> int:
    """Prove the stored credentials really work against both APIs."""
    from googleapiclient.discovery import build

    store = TokenStore()
    accounts = store.list_accounts()
    if not accounts:
        print("No connected accounts. Run `assistant-agent serve` and connect one.")
        return 1

    email = args.email or accounts[0].email
    creds = store.load_refreshed(email)
    if creds is None:
        print(f"Credentials for {email} are not usable. Reconnect via the web page.")
        return 1

    # cache_discovery=False silences the spurious "file_cache is only supported
    # with oauth2client<4.0.0" warning that surfaces under google-auth.
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    profile = gmail.users().getProfile(userId="me").execute()
    print(f"Gmail   {profile['emailAddress']}")
    print(f"        {profile['messagesTotal']} messages, {profile['threadsTotal']} threads")

    calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)
    events = (
        calendar.events()
        .list(
            calendarId="primary",
            timeMin=datetime.now(timezone.utc).isoformat(),
            maxResults=3,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
        .get("items", [])
    )
    print(f"\nCalendar next {len(events)} event(s)")
    for event in events:
        start = event["start"].get("dateTime") or event["start"].get("date")
        print(f"        {start}  {event.get('summary', '(no title)')}")
    if not events:
        print("        (nothing upcoming)")

    print("\nStored credentials are valid for both Gmail and Calendar.")
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(prog="assistant-agent")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("serve", help="run the web app (default)").set_defaults(func=_serve)
    subparsers.add_parser("accounts", help="list connected accounts").set_defaults(func=_accounts)

    verify = subparsers.add_parser("verify", help="make a real Gmail and Calendar call")
    verify.add_argument("--email", help="account to verify (default: the first one)")
    verify.set_defaults(func=_verify)

    args = parser.parse_args()
    handler = getattr(args, "func", _serve)

    try:
        sys.exit(handler(args))
    except ConfigError as exc:
        parser.exit(2, f"error: {exc}\n")
