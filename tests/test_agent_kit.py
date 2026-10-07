"""Cover the agent kit installer: what lands, with which modes, and how drift behaves."""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from assistant_agent.agent_kit import (
    AGENT_KIT_DIR,
    DIR_MODE,
    DOC_MODE,
    KIT_FILES,
    MANIFEST_NAME,
    check_agent_kit,
    install_agent_kit,
    rendered_bytes,
)
from assistant_agent.cli import _install_kit

RELPATHS = [relpath for relpath, _mode in KIT_FILES]


def _mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _states(data_dir: Path) -> dict[str, str]:
    return {s.relpath: s.state for s in check_agent_kit(data_dir)}


def test_install_creates_exactly_the_kit(tmp_path: Path) -> None:
    install_agent_kit(tmp_path)
    found = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()}
    assert found == set(RELPATHS) | {MANIFEST_NAME}


def test_installed_modes(tmp_path: Path) -> None:
    install_agent_kit(tmp_path)
    assert _mode_of(tmp_path / "calendar") == DIR_MODE
    assert _mode_of(tmp_path / "CLAUDE.md") == DOC_MODE
    for relpath in RELPATHS:
        if relpath.endswith(".py"):
            assert _mode_of(tmp_path / relpath) & stat.S_IXUSR, relpath


def test_installed_content_is_source_plus_header(tmp_path: Path) -> None:
    install_agent_kit(tmp_path)
    for relpath in RELPATHS:
        installed = (tmp_path / relpath).read_bytes()
        source = (AGENT_KIT_DIR / relpath).read_bytes()
        assert b"Managed copy" in installed[:400], relpath
        if relpath.endswith(".py") and source.startswith(b"#!"):
            # The shebang has to stay on line 0 or the copy is no longer executable.
            assert installed.split(b"\n", 1)[0] == source.split(b"\n", 1)[0], relpath
            assert installed.endswith(source.split(b"\n", 1)[1]), relpath
        else:
            assert installed.endswith(source), relpath


def test_header_is_deterministic() -> None:
    # A header carrying a timestamp or absolute path would make --check a permanent
    # false positive and leak local paths into every provisioned directory.
    for relpath in RELPATHS:
        assert rendered_bytes(relpath) == rendered_bytes(relpath)


def test_install_is_idempotent(tmp_path: Path) -> None:
    install_agent_kit(tmp_path)
    before = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}

    report = install_agent_kit(tmp_path)

    assert report["written"] == []
    assert sorted(report["unchanged"]) == sorted(RELPATHS)
    after = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before  # including the manifest, which must not be rewritten


def test_local_edit_is_reported_and_preserved(tmp_path: Path) -> None:
    install_agent_kit(tmp_path)
    edited = tmp_path / "CLAUDE.md"
    edited.write_bytes(edited.read_bytes() + b"\nlocal note\n")
    keep = edited.read_bytes()

    assert _states(tmp_path)["CLAUDE.md"] == "modified"

    report = install_agent_kit(tmp_path)
    assert {"relpath": "CLAUDE.md", "state": "modified"} in report["skipped"]
    assert edited.read_bytes() == keep  # not clobbered

    install_agent_kit(tmp_path, force=True)
    assert edited.read_bytes() == rendered_bytes("CLAUDE.md")


def test_stale_source_is_reinstalled(tmp_path: Path) -> None:
    install_agent_kit(tmp_path)
    manifest_path = tmp_path / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["calendar/CLAUDE.md"]["source_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))

    assert _states(tmp_path)["calendar/CLAUDE.md"] == "stale"

    report = install_agent_kit(tmp_path)  # no --force needed
    assert "calendar/CLAUDE.md" in report["written"]
    assert _states(tmp_path)["calendar/CLAUDE.md"] == "clean"


def test_preexisting_file_without_manifest_is_unmanaged(tmp_path: Path) -> None:
    # The state the real data/ directory was in before this change existed.
    (tmp_path / "CLAUDE.md").write_text("hand written")

    assert _states(tmp_path)["CLAUDE.md"] == "unmanaged"
    report = install_agent_kit(tmp_path)
    assert {"relpath": "CLAUDE.md", "state": "unmanaged"} in report["skipped"]
    assert (tmp_path / "CLAUDE.md").read_text() == "hand written"


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    report = install_agent_kit(tmp_path, dry_run=True)
    assert sorted(report["written"]) == sorted(RELPATHS)
    assert list(tmp_path.rglob("*")) == []


def test_kit_files_table_matches_disk() -> None:
    """New kit files must be wired in deliberately, not silently omitted from exports."""
    on_disk = {
        str(p.relative_to(AGENT_KIT_DIR))
        for p in AGENT_KIT_DIR.rglob("*")
        if p.is_file()
        and p.name != "__init__.py"
        and not any(part in ("__pycache__", ".ruff_cache") for part in p.parts)
    }
    assert on_disk == set(RELPATHS)


def test_installed_calendar_indexer_runs(tmp_path: Path) -> None:
    """Proves the copy is self-locating and the header did not break the script."""
    install_agent_kit(tmp_path)
    digest = hashlib.sha256(b"primary").hexdigest()
    event_dir = tmp_path / "calendar" / "2026" / digest
    event_dir.mkdir(parents=True)
    (event_dir / f"{digest}.json").write_text(
        json.dumps(
            {
                "calendarId": "primary",
                "event": {
                    "id": "evt1",
                    "summary": "Standup",
                    "organizer": {"email": "primary", "displayName": "Work"},
                    "start": {"dateTime": "2026-03-02T09:00:00-05:00", "timeZone": "America/New_York"},
                    "end": {"dateTime": "2026-03-02T09:15:00-05:00", "timeZone": "America/New_York"},
                },
            }
        )
    )
    out = tmp_path / "events.jsonl"

    subprocess.run(
        [sys.executable, str(tmp_path / "calendar" / "build_index.py"), str(out)],
        check=True,
        capture_output=True,
    )

    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["summary"] == "Standup"
    assert rows[0]["date"] == "2026-03-02"
    assert rows[0]["duration_min"] == 15
    assert rows[0]["calendar"] == "Work"


@pytest.mark.parametrize("drift", [False, True])
def test_check_exit_code(tmp_path: Path, drift: bool) -> None:
    install_agent_kit(tmp_path)
    if drift:
        target = tmp_path / "CLAUDE.md"
        target.write_bytes(target.read_bytes() + b"\nedited\n")

    args = argparse.Namespace(output=str(tmp_path), check=True, force=False)
    assert _install_kit(args) == (1 if drift else 0)
