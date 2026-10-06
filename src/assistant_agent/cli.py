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


def _export_2026(args: argparse.Namespace) -> int:
    import json
    from pathlib import Path

    from assistant_agent.export_2026 import export_account

    report = export_account(args.email, Path(args.output), calendar_only=args.calendar_only)
    print(json.dumps(report, indent=2))
    return 0 if report["complete"] else 1


def _install_kit(args: argparse.Namespace) -> int:
    """Copy the agent instructions and index tooling into an export directory."""
    from pathlib import Path

    from assistant_agent.agent_kit import check_agent_kit, install_agent_kit

    data_dir = Path(args.output)

    if args.check:
        statuses = check_agent_kit(data_dir)
        drifted = [s for s in statuses if s.state != "clean"]
        for status in statuses:
            print(f"{status.state:>10}  {status.relpath}")
        if drifted:
            print(f"\n{len(drifted)} file(s) need attention. Re-run without --check to install,")
            print("or with --force to overwrite copies that were edited in place.")
        return 1 if drifted else 0

    if not data_dir.is_dir():
        print(f"{data_dir} does not exist. Run `assistant-agent export-2026` first.")
        return 1

    report = install_agent_kit(data_dir, force=args.force)
    for relpath in report["written"]:
        print(f"   written  {relpath}")
    for relpath in report["unchanged"]:
        print(f" unchanged  {relpath}")
    for entry in report["skipped"]:
        print(f"   skipped  {entry['relpath']}  ({entry['state']})")
    if report["skipped"]:
        print("\nSkipped files differ from the packaged source. Re-run with --force to")
        print("overwrite them, after copying anything worth keeping back into")
        print("src/assistant_agent/agent_kit/.")
        return 1
    return 0


def _sandbox(args: argparse.Namespace) -> int:
    import asyncio

    return asyncio.run(_sandbox_async(args))


async def _sandbox_async(args: argparse.Namespace) -> int:
    """Run a command in the sandbox container, or report its status."""
    import json

    from assistant_agent.sandbox import SandboxError, health, run

    try:
        if not args.command:
            status = await health(name=args.container)
            print(json.dumps(status, indent=2))
            return 0 if status["ok"] else 1
        if not args.container:
            raise SandboxError("Execution requires sandbox --container <id>.")
        result = await run(" ".join(args.command), name=args.container)
    except SandboxError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(result.output, end="" if result.output.endswith("\n") else "\n")
    return result.exit_code


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(prog="assistant-agent")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("serve", help="run the web app (default)").set_defaults(func=_serve)
    subparsers.add_parser("accounts", help="list connected accounts").set_defaults(func=_accounts)

    verify = subparsers.add_parser("verify", help="make a real Gmail and Calendar call")
    verify.add_argument("--email", help="account to verify (default: the first one)")
    verify.set_defaults(func=_verify)

    export = subparsers.add_parser("export-2026", help="export 2026 Gmail and Calendar JSON")
    export.add_argument("--email", help="connected account (required when multiple accounts exist)")
    export.add_argument("--output", default="data", help="output directory (default: data)")
    export.add_argument("--calendar-only", action="store_true", help="export Calendar without listing or fetching Gmail")
    export.set_defaults(func=_export_2026)

    kit = subparsers.add_parser(
        "install-kit", help="install agent instructions and index tooling into an export directory"
    )
    kit.add_argument("--output", default="data", help="export directory (default: data)")
    kit.add_argument("--check", action="store_true", help="report drift without writing")
    kit.add_argument("--force", action="store_true", help="overwrite locally modified copies")
    kit.set_defaults(func=_install_kit)

    sandbox = subparsers.add_parser(
        "sandbox", help="run a command in the agent sandbox container (no command: report status)"
    )
    # REMAINDER so flags meant for the inner command are not parsed as ours.
    sandbox.add_argument("--container", help="explicit sandbox container ID")
    sandbox.add_argument("command", nargs=argparse.REMAINDER, help="command to run inside the sandbox")
    sandbox.set_defaults(func=_sandbox)

    args = parser.parse_args()
    handler = getattr(args, "func", _serve)

    try:
        sys.exit(handler(args))
    except ConfigError as exc:
        parser.exit(2, f"error: {exc}\n")
