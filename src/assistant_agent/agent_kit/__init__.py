"""Install the agent kit -- instructions and index tooling -- into an export directory.

The kit teaches an assistant agent how to read a Gmail/Calendar export: a CLAUDE.md
per export type plus the stdlib-only scripts that build a queryable index over it.
The tracked copies beside this module are the source of truth; the copies that land
in a data directory are generated and may be overwritten.

Installing copies rather than symlinks is deliberate. Both index scripts locate
themselves with `Path(__file__).resolve().parent`, and `.resolve()` follows symlinks,
so a symlinked script would resolve its root to this package directory and index
nothing -- failing silently with an empty index rather than loudly.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

KIT_VERSION = 1
MANIFEST_NAME = ".agent-kit.json"

# Match the permissions the exporter already applies to everything under data/.
DIR_MODE = 0o700
DOC_MODE = 0o600
SCRIPT_MODE = 0o700

AGENT_KIT_DIR = Path(__file__).resolve().parent

# Listed explicitly rather than globbed: `pip install .` byte-compiles the package,
# so __pycache__/ exists in the image, and lint caches accumulate in these
# directories too. This table is the reviewable contract for what lands in a user's
# export directory. tests/test_agent_kit.py asserts it matches what is on disk.
KIT_FILES: tuple[tuple[str, int], ...] = (
    ("calendar/CLAUDE.md", DOC_MODE),
    ("calendar/build_index.py", SCRIPT_MODE),
    ("emails/CLAUDE.md", DOC_MODE),
    ("emails/build_index.py", SCRIPT_MODE),
    ("emails/decode_email_bodies.py", SCRIPT_MODE),
)

_HEADER_LINES = (
    "Managed copy, installed by `assistant-agent install-kit` from",
    "assistant_agent/agent_kit/{relpath}. Edit the source in the repo and re-run",
    "install-kit -- changes made to this copy are overwritten and are not tracked.",
)


@dataclass(frozen=True)
class KitFileStatus:
    """One kit file's relationship to the packaged source and the recorded manifest."""

    relpath: str
    state: str  # missing | clean | modified | stale | conflict | unmanaged

    @property
    def needs_write(self) -> bool:
        return self.state in ("missing", "stale")

    @property
    def blocks(self) -> bool:
        """States that refuse to install without --force."""
        return self.state in ("modified", "conflict", "unmanaged")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _header(relpath: str, comment: str) -> str:
    """Provenance banner for an installed copy.

    Deterministic by construction: no timestamp, hostname, or absolute path. A
    varying header would make every machine produce a different hash -- turning
    `--check` into a permanent false positive -- and would leak a developer's local
    filesystem layout into every provisioned user directory.
    """
    body = "\n".join(f"{comment} {line.format(relpath=relpath)}" for line in _HEADER_LINES)
    return body + "\n"


def rendered_bytes(relpath: str, *, source: Path = AGENT_KIT_DIR) -> bytes:
    """The exact bytes that get installed: the source with a provenance header."""
    raw = (source / relpath).read_bytes()
    if relpath.endswith(".md"):
        return ("<!--\n" + _header(relpath, " ") + "-->\n\n").encode("utf-8") + raw
    # Keep the shebang on line 0 so the installed script stays directly executable.
    if raw.startswith(b"#!"):
        shebang, _, rest = raw.partition(b"\n")
        return shebang + b"\n" + _header(relpath, "#").encode("utf-8") + rest
    return _header(relpath, "#").encode("utf-8") + raw


def _manifest_path(data_dir: Path) -> Path:
    return data_dir / MANIFEST_NAME


def _read_manifest(data_dir: Path) -> dict:
    try:
        loaded = json.loads(_manifest_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded.get("files", {}) if isinstance(loaded, dict) else {}


def _atomic_write(path: Path, payload: bytes, mode: int) -> None:
    """Write via a temporary file in the same directory, then rename over the target."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def check_agent_kit(data_dir: Path, *, source: Path = AGENT_KIT_DIR) -> list[KitFileStatus]:
    """Classify every kit file without touching the filesystem.

    The manifest records two hashes per file, which separates "someone edited the
    installed copy" from "the packaged source moved on" -- a single hash cannot tell
    those apart, and they want opposite handling.
    """
    recorded = _read_manifest(data_dir)
    statuses: list[KitFileStatus] = []
    for relpath, _mode in KIT_FILES:
        destination = data_dir / relpath
        entry = recorded.get(relpath)
        if not destination.is_file():
            statuses.append(KitFileStatus(relpath, "missing"))
            continue
        if not isinstance(entry, dict):
            statuses.append(KitFileStatus(relpath, "unmanaged"))
            continue
        edited = _sha256(destination.read_bytes()) != entry.get("installed_sha256")
        moved = _sha256((source / relpath).read_bytes()) != entry.get("source_sha256")
        if edited and moved:
            state = "conflict"
        elif edited:
            state = "modified"
        elif moved:
            state = "stale"
        else:
            state = "clean"
        statuses.append(KitFileStatus(relpath, state))
    return statuses


def install_agent_kit(
    data_dir: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
    source: Path = AGENT_KIT_DIR,
) -> dict:
    """Copy the kit into `data_dir`, reporting rather than raising on drift.

    `data_dir` is required and never defaults to config.DATA_DIR: that constant is
    `Path.cwd() / "data"` evaluated at import, and this function will eventually be
    called with absolute per-user paths by the orchestrator.
    """
    modes = dict(KIT_FILES)
    statuses = check_agent_kit(data_dir, source=source)
    recorded = _read_manifest(data_dir)

    written: list[str] = []
    unchanged: list[str] = []
    skipped: list[dict] = []

    for status in statuses:
        if status.blocks and not force:
            skipped.append({"relpath": status.relpath, "state": status.state})
            continue
        if status.state == "clean":
            unchanged.append(status.relpath)
            continue

        payload = rendered_bytes(status.relpath, source=source)
        if not dry_run:
            destination = data_dir / status.relpath
            destination.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
            # mkdir's mode is masked by umask and ignored when the directory already
            # exists, so set it explicitly -- same reasoning as Exporter.run().
            os.chmod(destination.parent, DIR_MODE)
            _atomic_write(destination, payload, modes[status.relpath])
            recorded[status.relpath] = {
                "source_sha256": _sha256((source / status.relpath).read_bytes()),
                "installed_sha256": _sha256(payload),
            }
        written.append(status.relpath)

    # Only rewrite the manifest when something actually changed, so repeated runs
    # leave mtimes alone and `--check` stays quiet.
    if written and not dry_run:
        manifest = {
            "kit_version": KIT_VERSION,
            "installed_at": dt.datetime.now(dt.UTC).isoformat(),
            "files": recorded,
        }
        _atomic_write(
            _manifest_path(data_dir),
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            DOC_MODE,
        )

    return {
        "data_dir": str(data_dir),
        "written": written,
        "unchanged": unchanged,
        "skipped": skipped,
        "dry_run": dry_run,
    }


__all__ = [
    "AGENT_KIT_DIR",
    "DIR_MODE",
    "DOC_MODE",
    "KIT_FILES",
    "KIT_VERSION",
    "MANIFEST_NAME",
    "SCRIPT_MODE",
    "KitFileStatus",
    "check_agent_kit",
    "install_agent_kit",
    "rendered_bytes",
]
