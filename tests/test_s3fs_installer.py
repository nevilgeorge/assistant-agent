"""Validate explicit installer modes without installing packages or contacting networks."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

INSTALLER = Path(__file__).resolve().parents[1] / "deploy/production/install-s3fs.sh"


def installer_environment(tmp_path: Path) -> dict[str, str]:
    """Provide isolated executable metadata and forbid package/network operations."""
    binary_directory = tmp_path / "bin"
    binary_directory.mkdir()
    responses = {
        "s3fs": "echo 'Amazon Simple Storage Service File System V1.95'",
        "uname": "echo x86_64",
        "readelf": "printf 'Class: ELF64\\nMachine: Advanced Micro Devices X86-64\\n'",
        "ldd": "echo 'libfuse.so.2 => /lib64/libfuse.so.2'",
        "dnf": 'echo forbidden >> "$FORBIDDEN_LOG"; exit 99',
        "curl": 'echo forbidden >> "$FORBIDDEN_LOG"; exit 99',
    }
    for name, body in responses.items():
        command = binary_directory / name
        command.write_text(f"#!/bin/bash\n{body}\n")
        command.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{binary_directory}:{os.environ['PATH']}",
        "ASSISTANT_S3FS_BINARY": str(binary_directory / "s3fs"),
        "FORBIDDEN_LOG": str(tmp_path / "forbidden"),
    }


@pytest.mark.parametrize("mode", ["check", "install"])
def test_compatible_installer_never_uses_packages_or_network(tmp_path: Path, mode: str) -> None:
    """Both explicit modes are no-ops for an already compatible executable."""
    environment = installer_environment(tmp_path)
    candidate = tmp_path / "candidate"
    arguments = [mode, str(candidate)] if mode == "install" else [mode]
    result = subprocess.run(
        ["bash", str(INSTALLER), *arguments], env=environment,
        capture_output=True, text=True, check=False, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert not candidate.exists()
    assert not Path(environment["FORBIDDEN_LOG"]).exists()


@pytest.mark.parametrize(
    "command,response",
    [
        ("s3fs", "echo 'Amazon Simple Storage Service File System V1.950'"),
        ("readelf", "printf 'Class: ELF64\\nMachine: AArch64\\n'"),
        ("readelf", "printf 'Class: ELF32\\nMachine: Advanced Micro Devices X86-64\\n'"),
        ("ldd", "printf 'libfuse.so.2 => /lib64/libfuse.so.2\\nlibssl.so => not found\\n'"),
        ("ldd", "echo 'libfuse3.so.3 => /lib64/libfuse3.so.3'"),
    ],
)
def test_check_rejects_incompatible_binary_without_mutation(
    tmp_path: Path, command: str, response: str,
) -> None:
    """Invalid metadata or dependencies fail with actionable explicit setup guidance."""
    environment = installer_environment(tmp_path)
    (tmp_path / "bin" / command).write_text(f"#!/bin/bash\n{response}\n")
    result = subprocess.run(
        ["bash", str(INSTALLER), "check"], env=environment,
        capture_output=True, text=True, check=False, timeout=10,
    )
    assert result.returncode == 1
    assert "--setup-s3fs" in result.stderr
    assert not Path(environment["FORBIDDEN_LOG"]).exists()


def test_missing_binary_check_is_offline(tmp_path: Path) -> None:
    """Ordinary deployment must fail before package installation when s3fs is absent."""
    environment = installer_environment(tmp_path)
    environment["ASSISTANT_S3FS_BINARY"] = str(tmp_path / "missing")
    result = subprocess.run(
        ["bash", str(INSTALLER), "check"], env=environment,
        capture_output=True, text=True, check=False, timeout=10,
    )
    assert result.returncode == 1
    assert "--setup-s3fs" in result.stderr
    assert not Path(environment["FORBIDDEN_LOG"]).exists()


@pytest.mark.parametrize("checksum_valid", [True, False])
def test_explicit_build_validates_checksum_and_stages_before_replacement(
    tmp_path: Path, checksum_valid: bool,
) -> None:
    """A verified staged build leaves the running binary intact; bad archives never extract."""
    environment = installer_environment(tmp_path)
    binary_directory = tmp_path / "bin"
    build_log = tmp_path / "build.log"
    environment["BUILD_LOG"] = str(build_log)
    responses = {
        "s3fs": "echo 'Amazon Simple Storage Service File System V1.94'",
        "dnf": 'printf "%s\\n" "$*" >> "$BUILD_LOG"',
        "pkg-config": "echo 2.9.9",
        "curl": "exit 0",
        "sha256sum": 'cat >> "$BUILD_LOG"; ' + ("exit 0" if checksum_valid else "exit 1"),
        "tar": '''
echo extracted >> "$BUILD_LOG"
while [[ $1 != -C ]]; do shift; done
mkdir -p "$2/s3fs-fuse-1.95/src"
for command in autogen.sh configure; do
  printf '#!/bin/bash\\nexit 0\\n' > "$2/s3fs-fuse-1.95/$command"
  chmod +x "$2/s3fs-fuse-1.95/$command"
done
''',
        "make": '''
echo "$*" >> "$BUILD_LOG"
printf '#!/bin/bash\\necho "Amazon Simple Storage Service File System V1.95"\\n' > src/s3fs
chmod +x src/s3fs
''',
        # GNU install -D is Linux-specific; emulate only that filesystem operation here.
        "install": '''
[[ $1 == -D && $2 == -m && $3 == 0755 ]] || exit 1
cp "$4" "$5"
chmod 0755 "$5"
''',
    }
    for name, body in responses.items():
        command = binary_directory / name
        command.write_text(f"#!/bin/bash\n{body}\n")
        command.chmod(0o755)
    candidate = tmp_path / "candidate"
    result = subprocess.run(
        ["bash", str(INSTALLER), "install", str(candidate)], env=environment,
        capture_output=True, text=True, check=False, timeout=10,
    )
    assert (result.returncode == 0) == checksum_valid, result.stderr
    assert "V1.94" in (binary_directory / "s3fs").read_text()
    assert candidate.exists() == checksum_valid
    build_operations = build_log.read_text()
    assert "protected_packages=dnf,systemd,systemd-udev,curl-minimal,libcurl-minimal" in build_operations
    assert "0c97b8922f005500d36f72aee29a1345c94191f61d795e2a7b79fb7e3e6f5517" in build_operations
    assert ("extracted" in build_operations) == checksum_valid
    assert ("-j2" in build_operations) == checksum_valid
