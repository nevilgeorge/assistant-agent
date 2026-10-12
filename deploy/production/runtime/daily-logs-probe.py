#!/usr/bin/env python3
"""Bounded, content-free storage readiness check, also run inside the app image."""

import os
from pathlib import Path
import signal
import sys
import time
import uuid


def probe_storage(mountpoint: Path, expected_source: str) -> None:
    """Verify the mount and exercise the complete write/close/read/delete path."""
    stage = "mount"
    try:
        # Inspect this process's mount namespace, including when run inside the app.
        # A readable plain directory must never qualify as ready S3-backed storage.
        mounts = Path("/proc/self/mountinfo").read_text().splitlines()
        if not any(
            line.split()[4] == str(mountpoint)
            and line.split(" - ", 1)[1].split()[:2] == ["fuse.s3fs", expected_source]
            for line in mounts
        ):
            raise RuntimeError("unexpected filesystem")
        if os.geteuid() != 10001 or os.getegid() != 10001:
            stage = "identity"
            raise RuntimeError("unexpected identity")
        probe_path = mountpoint / f".readiness-{uuid.uuid4().hex}"
        # Exclusive creation and a random name avoid colliding with another probe.
        # Exercise flush/fsync and close before readback; stat alone cannot prove writes.
        payload = os.urandom(32)
        stage = "create-write-flush-close"
        with probe_path.open("xb") as probe_file:
            probe_file.write(payload)
            probe_file.flush()
            os.fsync(probe_file.fileno())
        stage = "reread"
        if probe_path.read_bytes() != payload:
            raise RuntimeError("readback mismatch")
        stage = "delete"
        probe_path.unlink()
    except Exception as error:
        # Report the failed operation and error category without leaking file contents.
        print(f"daily-logs probe failed: {stage} ({type(error).__name__})", file=sys.stderr)
        raise SystemExit(1) from None


def main() -> int:
    """Kill a stalled probe after 30 seconds without waiting for blocked FUSE I/O."""
    child_pid = os.fork()
    if child_pid == 0:
        probe_storage(Path(sys.argv[1]), sys.argv[2])
        os._exit(0)
    deadline = time.monotonic() + 30
    # A separate parent can enforce a deadline when the child stalls in FUSE I/O;
    # a timeout around a thread would leave the filesystem call running in that thread.
    while time.monotonic() < deadline:
        completed_pid, status = os.waitpid(child_pid, os.WNOHANG)
        if completed_pid:
            return 0 if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0 else 1
        time.sleep(0.1)
    os.kill(child_pid, signal.SIGKILL)
    # Do not wait for a kernel-blocked child after requesting termination: return
    # failure promptly so the supervising service can reject storage readiness.
    print("daily-logs probe failed: deadline", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
