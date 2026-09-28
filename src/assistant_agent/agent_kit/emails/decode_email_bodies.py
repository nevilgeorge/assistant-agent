#!/usr/bin/env python3
"""Decode Gmail body.data fields, overwriting existing JSON files.

Run once on the original encoded dump. No encoding marker is added, so this
script is not intended to be run again on files that have already been decoded.
All input files are validated before any file is overwritten.
"""

import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile


def decode_parts(part, location="payload"):
    """Change only body.data in the payload and its recursive MIME parts."""
    count = 0
    body = part.get("body", {})
    if "data" in body:
        data = body["data"]
        if not isinstance(data, str):
            raise ValueError(f"{location}.body.data is not a string")
        if data:
            if not re.fullmatch(r"[A-Za-z0-9_-]*={0,2}", data):
                raise ValueError(f"{location}.body.data is not base64url")
            try:
                raw = base64.b64decode(
                    data + "=" * (-len(data) % 4),
                    altchars=b"-_",
                    validate=True,
                )
                # Reject malformed padding and noncanonical encodings too.
                if base64.urlsafe_b64encode(raw).decode().rstrip("=") != data.rstrip("="):
                    raise ValueError("noncanonical base64url")
                body["data"] = raw.decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
                raise ValueError(f"{location}.body.data cannot be decoded: {exc}") from exc
            count += 1
    for index, child in enumerate(part.get("parts", [])):
        count += decode_parts(child, f"{location}.parts[{index}]")
    return count


def load_and_decode(path):
    original = path.read_text(encoding="utf-8")
    message = json.loads(original)
    count = decode_parts(message.get("payload", {}))
    return message, count, original.endswith("\n")


def overwrite(path, message, trailing_newline):
    """Replace the original atomically; the temporary file has a .tmp suffix."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(message, stream, ensure_ascii=False, indent=2)
            if trailing_newline:
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory", nargs="?", type=Path,
        default=Path(__file__).resolve().parent / "2026",
        help="directory to search recursively (default: 2026 beside this script)",
    )
    parser.add_argument("--dry-run", action="store_true", help="validate without changing files")
    args = parser.parse_args()
    if not args.directory.is_dir():
        parser.error(f"directory does not exist: {args.directory}")
    files = sorted(args.directory.rglob("*.json"))
    if not files:
        parser.error(f"no JSON files found in {args.directory}")

    print(f"Validating {len(files):,} JSON files before making changes...", flush=True)
    total = 0
    for path in files:
        try:
            _, count, _ = load_and_decode(path)
            total += count
        except (ValueError, OSError, TypeError, AttributeError) as exc:
            print(f"Validation failed: {path}: {exc}\nNo files were changed.", file=sys.stderr)
            return 1
    if args.dry_run:
        print(f"Validated {total:,} nonempty body.data fields. No files changed.")
        return 0

    changed = 0
    for index, path in enumerate(files, 1):
        try:
            message, count, trailing_newline = load_and_decode(path)
            if count:
                overwrite(path, message, trailing_newline)
                changed += 1
        except (ValueError, OSError, TypeError, AttributeError) as exc:
            print(
                f"Stopped at {path}: {exc}\n{changed:,} files were already overwritten.",
                file=sys.stderr,
            )
            return 1
        if index % 500 == 0:
            print(f"Processed {index:,}/{len(files):,} files...", flush=True)
    print(f"Decoded {total:,} fields; overwrote {changed:,} existing JSON files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
