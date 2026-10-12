"""Exercise deployment scripts with isolated command stubs, without AWS or host writes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess

import pytest

REPOSITORY = Path(__file__).resolve().parents[1]
PRODUCTION = REPOSITORY / "deploy/production"
RUNTIME = PRODUCTION / "runtime"


@pytest.fixture
def command_stubs(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Provide a command recorder and an isolated PATH for host lifecycle scripts."""
    binary_directory = tmp_path / "bin"
    binary_directory.mkdir()
    command_log = tmp_path / "commands.jsonl"
    environment = os.environ.copy()
    environment.update(PATH=f"{binary_directory}:{os.environ['PATH']}", COMMAND_LOG=str(command_log))
    return binary_directory, environment


def write_stub(binary_directory: Path, name: str, body: str = "") -> None:
    """Install a Python command stub that records argv before executing its response."""
    script = binary_directory / name
    script.write_text(
        "#!/usr/bin/env python3\nimport json, os, pathlib, sys\n"
        "with open(os.environ['COMMAND_LOG'], 'a') as output:\n"
        "    output.write(json.dumps([pathlib.Path(sys.argv[0]).name, *sys.argv[1:]]) + '\\n')\n"
        + body
    )
    script.chmod(0o755)


def read_commands(environment: dict[str, str]) -> list[list[str]]:
    """Read ordered calls made by the script under test."""
    return [json.loads(line) for line in Path(environment["COMMAND_LOG"]).read_text().splitlines()]


@pytest.mark.parametrize("setup_s3fs", [False, True])
def test_deploy_forwards_bucket_and_region_safely(
    command_stubs: tuple[Path, dict[str, str]], tmp_path: Path, setup_s3fs: bool,
) -> None:
    """Terraform's bucket output and the region survive the SSM shell boundary."""
    binary_directory, environment = command_stubs
    captured_parameters = tmp_path / "ssm.json"
    environment.update(
        AWS_REGION="us-west-2",
        DOMAIN="example.test; echo never-executed",
        CAPTURE_PARAMETERS=str(captured_parameters),
    )
    for variable in (
        "INSTANCE_ID", "ECR_REPOSITORY_URL", "SANDBOX_ECR_REPOSITORY_URL",
        "DATA_VOLUME_ID", "APPLICATION_DATA_BUCKET_NAME",
    ):
        environment.pop(variable, None)
    write_stub(binary_directory, "terraform", """
values = {'instance_id': 'i-test', 'ecr_repository_url': 'registry/app',
          'sandbox_ecr_repository_url': 'registry/sandbox', 'data_volume_id': 'vol-test',
          'application_data_bucket_name': 'daily-log-test-bucket'}
print(values[sys.argv[-1]])
""")
    write_stub(binary_directory, "docker")
    write_stub(binary_directory, "sleep")
    write_stub(binary_directory, "aws", """
args = sys.argv[1:]
if args[:2] == ['ecr', 'get-login-password']:
    print('fake-token')
elif args[:2] == ['ssm', 'send-command']:
    source = args[args.index('--parameters') + 1].removeprefix('file://')
    pathlib.Path(os.environ['CAPTURE_PARAMETERS']).write_text(pathlib.Path(source).read_text())
    print('command-test')
elif args[:2] == ['ssm', 'get-command-invocation']:
    if '--query' in args and args[args.index('--query') + 1] == 'Status':
        print('Success')
    else:
        print('{"Status":"Success"}')
""")
    result = subprocess.run(
        ["bash", str(REPOSITORY / "deploy/deploy.sh"), *(["--setup-s3fs"] if setup_s3fs else [])],
        env=environment,
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    command = json.loads(captured_parameters.read_text())["commands"][0]
    assignments = dict(token.split("=", 1) for token in shlex.split(command)[:7])
    assert assignments["SETUP_S3FS"] == str(setup_s3fs).lower()
    assert assignments["APPLICATION_DATA_BUCKET_NAME"] == "daily-log-test-bucket"
    assert assignments["AWS_REGION"] == "us-west-2"
    assert assignments["DOMAIN"] == "example.test; echo never-executed"
    calls = read_commands(environment)
    assert any(call[-1] == "application_data_bucket_name" for call in calls)
    send = next(call for call in calls if call[:3] == ["aws", "ssm", "send-command"])
    assert "executionTimeout" in " ".join(send) or "executionTimeout" in captured_parameters.read_text()


def test_compose_storage_paths_and_app_supervision() -> None:
    """Production binds ready storage directly; local storage uses a separate bind."""
    production = (PRODUCTION / "compose.prod.yaml").read_text()
    local = (REPOSITORY / "compose.yaml").read_text()
    assert 'restart: "no"' in production.split("  app:", 1)[1].split("  caddy:", 1)[0]
    assert "SANDBOX_HOST_USER_DATA_ROOT: /srv/assistant-agent/s3/user-data" in production
    assert "SANDBOX_APP_USER_DATA_ROOT: /srv/assistant-agent/s3/user-data" in production
    assert "source: /srv/assistant-agent/s3/user-data" in production
    assert "target: /srv/assistant-agent/s3/user-data" in production
    assert "propagation: rprivate" in production
    assert "rslave" not in production
    assert "create_host_path: false" in production
    assert "SANDBOX_APP_USER_DATA_ROOT: /srv/assistant-agent/user-data" in local
    assert "${SANDBOX_HOST_USER_DATA_ROOT}:/srv/assistant-agent/user-data" in local


def test_units_gate_startup_and_disable_automatic_mount_recovery() -> None:
    """A FUSE failure stops the app and requires deliberate operator recovery."""
    mount_unit = (PRODUCTION / "systemd/assistant-daily-logs.service").read_text()
    app_unit = (PRODUCTION / "systemd/assistant-app.service").read_text()
    assert "Restart=no" in mount_unit
    assert "TimeoutStartSec=180" in mount_unit
    assert "ExecStartPost=" in mount_unit and "ready" in mount_unit
    assert "assistant-daily-logs-prepare.service" not in mount_unit
    assert "RequiresMountsFor=/srv/assistant-agent" in mount_unit
    mount_command = next(line for line in mount_unit.splitlines() if line.startswith("ExecStart="))
    # FUSE 2 suppresses its automatic subtype when fsname is supplied explicitly.
    assert "-o fsname=@BUCKET@:/user-data" in mount_command
    assert "-o subtype=s3fs" in mount_command
    assert mount_unit.index(" cleanup") < mount_unit.index(" prepare") < mount_unit.index("ExecStart=")
    for directive in ("Requires", "After", "BindsTo"):
        matching = [line for line in app_unit.splitlines() if line.startswith(directive + "=")]
        assert any("assistant-daily-logs.service" in line for line in matching)
    assert "Restart=on-failure" in app_unit
    assert "WantedBy=multi-user.target" in app_unit


def host_environment(
    command_stubs: tuple[Path, dict[str, str]], tmp_path: Path
) -> tuple[Path, dict[str, str]]:
    """Supply a real temporary storage config to otherwise stubbed host commands."""
    binary_directory, environment = command_stubs
    data_root = tmp_path / "data"
    data_root.mkdir()
    work_directory = tmp_path / "work"
    work_directory.mkdir()
    configuration = tmp_path / "storage.conf"
    configuration.write_text(
        f"DATA_ROOT={shlex.quote(str(data_root))}\n"
        "DATA_VOLUME_UUID=test-volume-uuid\n"
        "APPLICATION_DATA_BUCKET_NAME=daily-log-test-bucket\n"
        "AWS_REGION=us-west-2\nSANDBOX_DEPLOYMENT_ID=production\n"
    )
    environment.update(
        ASSISTANT_STORAGE_CONFIG=str(configuration),
        ASSISTANT_WORK_DIR=str(work_directory),
        ASSISTANT_HELPER_DIR=str(RUNTIME),
    )
    return binary_directory, environment


def test_host_assets_are_packaged_with_the_app_image() -> None:
    """Explicit flattened image assets include runtime files but exclude local secrets."""
    dockerfile = (REPOSITORY / "Dockerfile").read_text()
    bootstrap = (PRODUCTION / "bootstrap.sh").read_text()
    assets = (
        "systemd/assistant-daily-logs.service", "systemd/assistant-app.service",
        "runtime/daily-logs-host.sh", "runtime/daily-logs-probe.py",
        "runtime/app-host.sh", "runtime/recover-daily-logs.sh",
        "install-s3fs.sh", "apply.sh", "compose.prod.yaml", "Caddyfile",
    )
    logical_lines = dockerfile.replace("\\\n", " ").splitlines()
    deployment_copies = [line.split() for line in logical_lines
                         if line.startswith("COPY ") and "deploy/" in line]
    assert deployment_copies
    copy_sources = [source for tokens in deployment_copies for source in tokens[1:-1]]
    for name in assets:
        assert (PRODUCTION / name).is_file()
        assert f"deploy/production/{name}" in copy_sources
    assert all(tokens[-1] == "./deploy/" for tokens in deployment_copies)
    assert all("*" not in source and (REPOSITORY / source).is_file() for source in copy_sources)
    assert "/app/deploy" in bootstrap
    assert "deploy/local-db-password.txt" in (REPOSITORY / ".dockerignore").read_text().splitlines()


@pytest.mark.parametrize("failure", ["", "cleanup", "ready", "app-start"])
def test_manual_recovery_order_and_abort(
    command_stubs: tuple[Path, dict[str, str]], tmp_path: Path, failure: str
) -> None:
    """Recovery never admits the app after a failed cleanup or storage probe."""
    binary_directory, environment = host_environment(command_stubs, tmp_path)
    helper_directory = tmp_path / "helpers"
    helper_directory.mkdir()
    environment.update(ASSISTANT_HELPER_DIR=str(helper_directory), FAIL_STEP=failure)
    write_stub(binary_directory, "systemctl", """
if os.environ['FAIL_STEP'] == 'app-start' and sys.argv[1:] == ['start', 'assistant-app.service']:
    sys.exit(1)
""")
    write_stub(helper_directory, "app-host.sh")
    write_stub(helper_directory, "daily-logs-host.sh", """
if sys.argv[1] == os.environ['FAIL_STEP']:
    sys.exit(1)
""")
    result = subprocess.run(
        ["bash", str(RUNTIME / "recover-daily-logs.sh")], env=environment,
        capture_output=True, text=True, timeout=10,
    )
    assert (result.returncode != 0) == bool(failure), result.stderr
    calls = read_commands(environment)
    ordered_steps = [
        ["systemctl", "stop", "assistant-app.service"],
        ["app-host.sh", "stop"],
        ["systemctl", "stop", "assistant-daily-logs.service"],
        ["daily-logs-host.sh", "cleanup"],
        ["systemctl", "start", "assistant-daily-logs.service"],
        ["daily-logs-host.sh", "ready"],
        ["systemctl", "start", "assistant-app.service"],
    ]
    if failure in ("cleanup", "ready"):
        assert ordered_steps[-1] not in calls
        ordered_steps = ordered_steps[:4 if failure == "cleanup" else 6]
    indexes = [calls.index(step) for step in ordered_steps]
    assert indexes == sorted(indexes)
    if failure == "app-start":
        assert calls[-1] == ["systemctl", "stop", "assistant-app.service"]


def test_app_stop_retires_only_label_scoped_sandboxes(
    command_stubs: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    """Retirement uses both ownership and deployment and preserves storage."""
    binary_directory, environment = host_environment(command_stubs, tmp_path)
    write_stub(binary_directory, "docker", """
state = pathlib.Path(os.environ['COMMAND_LOG'] + '.removed')
if sys.argv[1:3] == ['ps', '-aq'] and not state.exists():
    print('sandbox-one\\nsandbox-two')
if sys.argv[1:] == ['rm', '-f', 'sandbox-two']:
    state.touch()
""")
    result = subprocess.run(
        ["bash", str(RUNTIME / "app-host.sh"), "stop"], env=environment,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    calls = read_commands(environment)
    listing = next(call for call in calls if call[1:3] == ["ps", "-aq"])
    assert "label=assistant-agent.owner=assistant-agent" in listing
    assert "label=assistant-agent.deployment=production" in listing
    assert [call for call in calls if call[1:3] == ["rm", "-f"]] == [
        ["docker", "rm", "-f", "sandbox-one"], ["docker", "rm", "-f", "sandbox-two"],
    ]
    assert calls[-1][1:3] == ["ps", "-aq"]
    assert (tmp_path / "data").is_dir()


def test_failed_app_image_probe_blocks_start_check(
    command_stubs: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    """A healthy database cannot conceal storage failure through the app's bind."""
    binary_directory, environment = host_environment(command_stubs, tmp_path)
    write_stub(binary_directory, "docker", """
if 'daily-logs-probe.py' in ' '.join(sys.argv):
    sys.exit(23)
""")
    result = subprocess.run(
        ["bash", str(RUNTIME / "app-host.sh"), "check"], env=environment,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    calls = read_commands(environment)
    assert "db" in calls[0] and "--wait" in calls[0]
    assert "10001:10001" in calls[-1] and "--no-deps" in calls[-1]
    assert not any("--exit-code-from" in call for call in calls)


@pytest.mark.parametrize("filesystem,unmount_failure", [("ext4", False), ("fuse.s3fs", True)])
def test_cleanup_refuses_wrong_filesystem_or_failed_unmount(
    command_stubs: tuple[Path, dict[str, str]], tmp_path: Path,
    filesystem: str, unmount_failure: bool,
) -> None:
    """Cleanup cannot silently succeed or unmount unrelated host filesystems."""
    binary_directory, environment = host_environment(command_stubs, tmp_path)
    environment.update(TEST_FILESYSTEM=filesystem)
    write_stub(binary_directory, "docker")
    write_stub(binary_directory, "findmnt", """
if '-o' in sys.argv:
    print(os.environ['TEST_FILESYSTEM'] if sys.argv[-1] == 'FSTYPE'
          else 'daily-log-test-bucket:/user-data')
""")
    write_stub(binary_directory, "timeout", "sys.exit(1)\n")
    result = subprocess.run(
        ["bash", str(RUNTIME / "daily-logs-host.sh"), "cleanup"], env=environment,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    calls = read_commands(environment)
    unmounts = [call for call in calls if call[0] == "timeout"]
    assert bool(unmounts) == unmount_failure
    if unmounts:
        assert unmounts[0][-1] == str(tmp_path / "data/s3/user-data")
        assert "-u" in unmounts[0] and "-z" not in unmounts[0]


def test_s3fs_install_is_idempotent_for_exact_version_and_fuse2(
    command_stubs: tuple[Path, dict[str, str]],
) -> None:
    """A valid existing installation performs no package or network operations."""
    binary_directory, environment = command_stubs
    write_stub(binary_directory, "s3fs", "print('Amazon Simple Storage Service File System V1.95')\n")
    write_stub(binary_directory, "ldd", "print('libfuse.so.2 => /lib64/libfuse.so.2')\n")
    write_stub(binary_directory, "dnf", "sys.exit('unexpected package operation')\n")
    write_stub(binary_directory, "curl", "sys.exit('unexpected download')\n")
    environment["ASSISTANT_S3FS_BINARY"] = str(binary_directory / "s3fs")
    write_stub(binary_directory, "uname", "print('x86_64')\n")
    write_stub(binary_directory, "readelf", "print('Class: ELF64\\nMachine: Advanced Micro Devices X86-64')\n")
    for mode in ("check", "install"):
        result = subprocess.run(
            ["bash", str(PRODUCTION / "install-s3fs.sh"), mode], env=environment,
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
    assert not any(call[0] in ("dnf", "curl") for call in read_commands(environment))


@pytest.mark.parametrize("uuid_value", ["", "wrong-volume"])
def test_preparation_rejects_missing_or_wrong_ebs(
    command_stubs: tuple[Path, dict[str, str]], tmp_path: Path, uuid_value: str
) -> None:
    """Preparation refuses the root disk or a different mounted data volume."""
    binary_directory, environment = host_environment(command_stubs, tmp_path)
    environment["TEST_UUID"] = uuid_value
    write_stub(binary_directory, "findmnt", "print(os.environ['TEST_UUID'])\n")
    result = subprocess.run(
        ["bash", str(RUNTIME / "daily-logs-host.sh"), "prepare"], env=environment,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "missing or wrong EBS mount" in result.stderr
    assert list((tmp_path / "data").iterdir()) == []


@pytest.mark.parametrize("mount_source,identity", [
    ("daily-log-test-bucket:/user-data", 10001),
    ("wrong-bucket:/user-data", 10001),
    ("daily-log-test-bucket:/user-data", 0),
])
def test_storage_probe_checks_identity_mount_and_cleans_successful_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    mount_source: str, identity: int,
) -> None:
    """A probe writes only through the expected mount and application identity."""
    import runpy

    probe_module = runpy.run_path(str(RUNTIME / "daily-logs-probe.py"))
    original_read_text = Path.read_text

    def read_mount_information(path: Path, *args: object, **kwargs: object) -> str:
        """Provide a synthetic mount table while retaining real probe file I/O."""
        if str(path) == "/proc/self/mountinfo":
            return f"1 0 0:1 / {tmp_path} rw - fuse.s3fs {mount_source} rw\n"
        return original_read_text(path, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(Path, "read_text", read_mount_information)
        context.setattr(os, "geteuid", lambda: identity)
        context.setattr(os, "getegid", lambda: identity)
        if mount_source.startswith("wrong") or identity != 10001:
            with pytest.raises(SystemExit, match="1"):
                probe_module["probe_storage"](tmp_path, "daily-log-test-bucket:/user-data")
            assert "daily-logs probe failed:" in capsys.readouterr().err
        else:
            probe_module["probe_storage"](tmp_path, "daily-log-test-bucket:/user-data")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("redeploy_mode", [
    "unchanged", "changed", "failed-probe", "inactive", "legacy", "wrong-parent", "interrupted", "invalid-binary",
])
def test_host_installation_preserves_healthy_mount_and_blocks_failed_storage(
    command_stubs: tuple[Path, dict[str, str]], tmp_path: Path, redeploy_mode: str,
) -> None:
    """Repeated installs preserve mounts; changed config retires before replacement."""
    import shutil

    binary_directory, environment = command_stubs
    assets = tmp_path / "assets"
    assets.mkdir()
    for source in (PRODUCTION / "systemd").glob("*.service"):
        shutil.copyfile(source, assets / source.name)
    for name in ("daily-logs-probe.py", "recover-daily-logs.sh"):
        shutil.copyfile(RUNTIME / name, assets / name)
    # The installer intentionally invokes this with bash, so use a shell stub here.
    (assets / "install-s3fs.sh").write_text("#!/bin/bash\nexit 0\n")
    write_stub(assets, "app-host.sh")
    write_stub(binary_directory, "findmnt", "sys.exit(1)\n")
    write_stub(assets, "daily-logs-host.sh", """
if os.environ.get('FAIL_PROBE') and sys.argv[1] == 'ready':
    sys.exit(1)
""")
    write_stub(binary_directory, "systemctl", """
if os.environ.get('INACTIVE') and sys.argv[1] == 'is-active':
    sys.exit(1)
""")
    environment.update(
        APPLICATION_DATA_BUCKET_NAME="daily-log-test-bucket", AWS_REGION="us-west-2",
        DATA_VOLUME_UUID="1234-abcd", SANDBOX_DEPLOYMENT_ID="production",
        ASSISTANT_STORAGE_CONFIG=str(tmp_path / "etc/storage.conf"),
        ASSISTANT_UNIT_DIR=str(tmp_path / "units"),
        ASSISTANT_HELPER_DIR=str(tmp_path / "helpers"),
        ASSISTANT_WORK_DIR=str(tmp_path / "work"),
    )
    command = ["bash", "-c", '''
set -euo pipefail
source "$1"
initialize_deployment "$2"
candidate=$(mktemp -d)
trap 'rm -rf "$candidate"' EXIT
prepare_candidates
retire_consumers_and_storage
publish_host_services
''', "test-apply", str(PRODUCTION / "apply.sh"), str(assets)]
    first = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=10)
    assert first.returncode == 0, first.stderr
    initial_calls = read_commands(environment)
    assert ["systemctl", "start", "assistant-daily-logs.service"] in initial_calls
    Path(environment["COMMAND_LOG"]).write_text("")
    if redeploy_mode == "changed":
        environment["APPLICATION_DATA_BUCKET_NAME"] = "replacement-daily-log-bucket"
    elif redeploy_mode == "failed-probe":
        environment["FAIL_PROBE"] = "1"
    elif redeploy_mode == "inactive":
        environment["INACTIVE"] = "1"
    elif redeploy_mode == "invalid-binary":
        (assets / "install-s3fs.sh").write_text("#!/bin/bash\nexit 1\n")
    elif redeploy_mode == "interrupted":
        Path(environment["ASSISTANT_STORAGE_CONFIG"] + ".install-pending").touch()
    elif redeploy_mode in ("legacy", "wrong-parent"):
        legacy_unit = Path(environment["ASSISTANT_UNIT_DIR"]) / "assistant-daily-logs-prepare.service"
        legacy_unit.write_text("obsolete preparation unit")
        environment["PARENT_UUID"] = "1234-abcd" if redeploy_mode == "legacy" else "wrong-volume"
        write_stub(binary_directory, "findmnt", """
removed = pathlib.Path(os.environ['COMMAND_LOG'] + '.unmounted')
if sys.argv[sys.argv.index('--mountpoint') + 1] != '/srv/assistant-agent/s3' or removed.exists():
    sys.exit(1)
if '-o' in sys.argv:
    print({'UUID': os.environ['PARENT_UUID'], 'FSTYPE': 'ext4', 'FSROOT': '/s3'}[sys.argv[-1]])
""")
        write_stub(binary_directory, "timeout", """
pathlib.Path(os.environ['COMMAND_LOG'] + '.unmounted').touch()
""")
    second = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=10)
    assert (second.returncode != 0) == (redeploy_mode in ("failed-probe", "inactive", "wrong-parent", "invalid-binary")), second.stderr
    calls = read_commands(environment)
    if redeploy_mode == "invalid-binary":
        assert not calls
        return
    assert calls[0] == ["systemctl", "stop", "assistant-app.service"]
    if redeploy_mode in ("changed", "legacy", "wrong-parent", "interrupted"):
        expected = [["app-host.sh", "stop"],
                    ["systemctl", "stop", "assistant-daily-logs.service"],
                    ["daily-logs-host.sh", "cleanup"],
                    ["systemctl", "start", "assistant-daily-logs.service"],
                    ["daily-logs-host.sh", "ready"]]
        if redeploy_mode == "wrong-parent":
            expected = expected[:3]
            assert not any(call[0] == "timeout" for call in calls)
            assert legacy_unit.is_file()
        elif redeploy_mode == "legacy":
            assert not legacy_unit.exists()
            assert ["systemctl", "disable", "--now", "assistant-daily-logs-prepare.service"] in calls
            assert ["timeout", "--kill-after=2", "15", "umount", "/srv/assistant-agent/s3"] in calls
            Path(environment["COMMAND_LOG"]).write_text("")
            rerun = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=10)
            assert rerun.returncode == 0, rerun.stderr
            assert not any(call[0] == "timeout" for call in read_commands(environment))
        indexes = [calls.index(call) for call in expected]
        assert indexes == sorted(indexes)
    else:
        assert not any(call[:2] == ["systemctl", action]
                       and "assistant-daily-logs.service" in call
                       for call in calls for action in ("stop", "start", "restart"))
        assert ["daily-logs-host.sh", "cleanup"] not in calls
    assert ["systemctl", "start", "assistant-app.service"] not in calls


@pytest.mark.parametrize("backing_state", ["nonempty", "protected"])
def test_preparation_preserves_cache_and_refuses_uncovered_data(
    command_stubs: tuple[Path, dict[str, str]], tmp_path: Path, backing_state: str,
) -> None:
    """A rerun preserves the cache and never rewrites an already immutable inode."""
    binary_directory, environment = host_environment(command_stubs, tmp_path)
    data_root = tmp_path / "data"
    mountpoint = data_root / "s3/user-data"
    mountpoint.mkdir(parents=True)
    cache_directory = data_root / "s3fs-cache"
    cache_directory.mkdir()
    (cache_directory / "cached-content").write_text("keep")
    if backing_state == "nonempty":
        (mountpoint / "unexpected-entry").write_text("keep this too")
    environment["TEST_DATA_ROOT"] = str(data_root)
    write_stub(binary_directory, "findmnt", """
mountpoint = sys.argv[sys.argv.index('--mountpoint') + 1]
if mountpoint == os.environ['TEST_DATA_ROOT']:
    print('test-volume-uuid' if sys.argv[-1] == 'UUID' else 'ext4')
else:
    sys.exit(1)
""")
    write_stub(binary_directory, "install")
    write_stub(binary_directory, "mount")
    write_stub(binary_directory, "lsattr", "print('----i--------- protected-directory')\n")
    write_stub(binary_directory, "stat", "print('0:0:0')\n")
    for name in ("chown", "chmod", "chattr"):
        write_stub(binary_directory, name, "sys.exit('unexpected immutable metadata mutation')\n")
    for _ in range(2):
        result = subprocess.run(
            ["bash", str(RUNTIME / "daily-logs-host.sh"), "prepare"], env=environment,
            capture_output=True, text=True, timeout=10,
        )
        assert (result.returncode != 0) == (backing_state == "nonempty"), result.stderr
        if backing_state == "nonempty":
            assert "nonempty uncovered" in result.stderr
            assert (mountpoint / "unexpected-entry").read_text() == "keep this too"
    assert (cache_directory / "cached-content").read_text() == "keep"
    assert not any(call[0] in ("chown", "chmod", "chattr") for call in read_commands(environment))


@pytest.mark.parametrize("failure", ["remove", "remains", "listing"])
def test_app_retirement_failure_blocks_cleanup(
    command_stubs: tuple[Path, dict[str, str]], tmp_path: Path, failure: str,
) -> None:
    """A failed removal or unverifiable consumer set prevents storage replacement."""
    binary_directory, environment = host_environment(command_stubs, tmp_path)
    environment["RETIREMENT_FAILURE"] = failure
    write_stub(binary_directory, "docker", """
if sys.argv[1:3] == ['ps', '-aq']:
    if os.environ['RETIREMENT_FAILURE'] == 'listing':
        sys.exit(1)
    print('sandbox-one')
if sys.argv[1:3] == ['rm', '-f'] and os.environ['RETIREMENT_FAILURE'] == 'remove':
    sys.exit(1)
""")
    stop_result = subprocess.run(
        ["bash", str(RUNTIME / "app-host.sh"), "stop"], env=environment,
        capture_output=True, text=True, timeout=10,
    )
    assert stop_result.returncode != 0
    write_stub(binary_directory, "findmnt", "sys.exit(1)\n")
    write_stub(binary_directory, "timeout", "sys.exit('unexpected unmount')\n")
    cleanup_result = subprocess.run(
        ["bash", str(RUNTIME / "daily-logs-host.sh"), "cleanup"], env=environment,
        capture_output=True, text=True, timeout=10,
    )
    assert cleanup_result.returncode != 0
    assert not any(call[0] == "timeout" for call in read_commands(environment))


def test_app_start_recreates_only_app(
    command_stubs: tuple[Path, dict[str, str]], tmp_path: Path,
) -> None:
    """App restart acquires a fresh bind without recreating database or Caddy."""
    binary_directory, environment = host_environment(command_stubs, tmp_path)
    write_stub(binary_directory, "docker")
    result = subprocess.run(
        ["bash", str(RUNTIME / "app-host.sh"), "start"], env=environment,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    call, = read_commands(environment)
    assert "--force-recreate" in call and "--no-deps" in call
    assert call[-1] == "app" and "db" not in call and "caddy" not in call


def test_app_retirement_removes_transient_consumers(
    command_stubs: tuple[Path, dict[str, str]], tmp_path: Path,
) -> None:
    """Readiness and migration containers cannot retain binds across replacement."""
    binary_directory, environment = host_environment(command_stubs, tmp_path)
    work_directory = Path(environment["ASSISTANT_WORK_DIR"])
    (work_directory / "compose.yaml").write_text("services: {}\n")
    (work_directory / ".env").write_text("")
    write_stub(binary_directory, "docker", """
removed = pathlib.Path(os.environ['COMMAND_LOG'] + '.removed')
if 'compose' in sys.argv and 'ps' in sys.argv and not removed.exists():
    print('app-container\\nmigration-container\\nreadiness-container')
if sys.argv[1:] == ['rm', '-f', 'readiness-container']:
    removed.touch()
""")
    result = subprocess.run(
        ["bash", str(RUNTIME / "app-host.sh"), "stop"], env=environment,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    calls = read_commands(environment)
    for container in ("app-container", "migration-container", "readiness-container"):
        assert ["docker", "rm", "-f", container] in calls
    app_listings = [call for call in calls if "compose" in call and "ps" in call]
    assert len(app_listings) == 2
    assert all("--all" in call for call in app_listings)


@pytest.mark.parametrize("ready_state", ["healthy", "probe-failure", "absent", "multiple"])
def test_app_ready_checks_actual_container_bind(
    command_stubs: tuple[Path, dict[str, str]], tmp_path: Path, ready_state: str,
) -> None:
    """Activation verifies the recreated app itself and fails on unavailable storage."""
    binary_directory, environment = host_environment(command_stubs, tmp_path)
    environment["READY_STATE"] = ready_state
    write_stub(binary_directory, "sleep")
    write_stub(binary_directory, "timeout", """
import subprocess
sys.exit(subprocess.run(sys.argv[3:]).returncode)
""")
    write_stub(binary_directory, "docker", """
state = os.environ['READY_STATE']
if 'ps' in sys.argv:
    if state != 'absent':
        print('running-app' if state != 'multiple' else 'running-app\\nextra-app')
elif sys.argv[1] == 'exec' and state == 'probe-failure':
    sys.exit(23)
""")
    result = subprocess.run(
        ["bash", str(RUNTIME / "app-host.sh"), "ready"], env=environment,
        capture_output=True, text=True, timeout=10,
    )
    assert (result.returncode == 0) == (ready_state == "healthy"), result.stderr
    probes = [call for call in read_commands(environment) if call[:2] == ["docker", "exec"]]
    if ready_state in ("absent", "multiple"):
        assert not probes
    else:
        assert len(probes) == 1
        assert probes[0][2:5] == ["--user", "10001:10001", "running-app"]
        assert "/app/deploy/daily-logs-probe.py" in probes[0]


@pytest.mark.parametrize("failure", ["", "image-extraction", "tar-extraction", "apply"])
@pytest.mark.parametrize("setup_s3fs", [False, True])
def test_bootstrap_forwards_intent_and_propagates_failures(
    command_stubs: tuple[Path, dict[str, str]], tmp_path: Path,
    failure: str, setup_s3fs: bool,
) -> None:
    """Bootstrap needs only its payload and image; extraction failures never apply."""
    import tarfile

    binary_directory, environment = command_stubs
    environment.update(
        AWS_REGION="us-west-2", IMAGE_URI="registry/app:release",
        SANDBOX_IMAGE_URI="registry/sandbox:release", DOMAIN="example.test",
        DATA_VOLUME_ID="vol-test", APPLICATION_DATA_BUCKET_NAME="test-bucket",
        SETUP_S3FS=str(setup_s3fs).lower(), SANDBOX_DEPLOYMENT_ID="test-deployment",
        BOOTSTRAP_FAILURE=failure, BOOTSTRAP_ARCHIVE=str(tmp_path / "assets.tar"),
    )
    apply_stub = tmp_path / "apply.sh"
    apply_stub.write_text('''#!/bin/bash
python3 - "$1" <<'PYTHON'
import json, os, pathlib, sys
with open(os.environ['COMMAND_LOG'], 'a') as output:
    output.write(json.dumps(['apply', sys.argv[1], {
        name: os.environ[name] for name in (
            'AWS_REGION', 'IMAGE_URI', 'SANDBOX_IMAGE_URI', 'DOMAIN',
            'DATA_VOLUME_ID', 'APPLICATION_DATA_BUCKET_NAME', 'SETUP_S3FS',
            'SANDBOX_DEPLOYMENT_ID',
        )
    }]) + '\\n')
sys.exit(37 if os.environ['BOOTSTRAP_FAILURE'] == 'apply' else 0)
PYTHON
''')
    with tarfile.open(environment["BOOTSTRAP_ARCHIVE"], "w") as archive:
        archive.add(apply_stub, arcname="apply.sh")
    write_stub(binary_directory, "aws", "print('test-token')\n")
    write_stub(binary_directory, "docker", """
if sys.argv[1] == 'login':
    sys.stdin.read()
if sys.argv[1] == 'run':
    if os.environ['BOOTSTRAP_FAILURE'] == 'image-extraction':
        sys.exit(23)
    sys.stdout.buffer.write(pathlib.Path(os.environ['BOOTSTRAP_ARCHIVE']).read_bytes())
""")
    if failure == "tar-extraction":
        write_stub(binary_directory, "tar", "sys.stdin.buffer.read(); sys.exit(31)\n")
    # Running a copy from an unrelated directory catches dependencies on repo siblings.
    payload = tmp_path / "payload.sh"
    payload.write_text((PRODUCTION / "bootstrap.sh").read_text())
    result = subprocess.run(
        ["bash", str(payload)], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=10,
    )
    assert (result.returncode != 0) == bool(failure), result.stderr
    calls = read_commands(environment)
    pulls = [call for call in calls if call[:2] == ["docker", "pull"]]
    assert pulls == [["docker", "pull", environment["IMAGE_URI"]],
                     ["docker", "pull", environment["SANDBOX_IMAGE_URI"]]]
    application_calls = [call for call in calls if call[0] == "apply"]
    if failure in ("image-extraction", "tar-extraction"):
        assert not application_calls
    else:
        application_call, = application_calls
        assert application_call[2] == {
            name: environment[name] for name in application_call[2]
        }
        assert not Path(application_call[1]).exists()
    if failure == "apply":
        assert result.returncode == 37


@pytest.mark.parametrize("failure", [
    "", "prepare_host", "prepare_candidates", "retire_consumers_and_storage",
    "publish_host_services", "deploy_application",
])
def test_apply_orders_phases_and_aborts_before_later_work(tmp_path: Path, failure: str) -> None:
    """Host deployment stops at a failed phase and always removes its staging directory."""
    environment = os.environ.copy()
    environment.update(
        AWS_REGION="us-west-2", IMAGE_URI="registry/app:test",
        SANDBOX_IMAGE_URI="registry/sandbox:test", DOMAIN="example.test",
        DATA_VOLUME_ID="vol-test", APPLICATION_DATA_BUCKET_NAME="test-bucket",
        PHASE_LOG=str(tmp_path / "phases"), FAILED_PHASE=failure,
        CANDIDATE_LOG=str(tmp_path / "candidate"),
    )
    result = subprocess.run(
        ["bash", "-c", '''
source "$1"
record_phase() {
  printf '%s\\n' "$1" >> "$PHASE_LOG"
  printf '%s' "$candidate" > "$CANDIDATE_LOG"
  if [[ "$1" == "$FAILED_PHASE" ]]; then return 27; fi
}
prepare_host() { record_phase prepare_host; }
prepare_candidates() { record_phase prepare_candidates; }
retire_consumers_and_storage() { record_phase retire_consumers_and_storage; }
publish_host_services() { record_phase publish_host_services; }
deploy_application() { record_phase deploy_application; }
main "$2"
''', "test-apply", str(PRODUCTION / "apply.sh"), str(tmp_path)],
        env=environment, capture_output=True, text=True, timeout=10,
    )
    expected_phases = [
        "prepare_host", "prepare_candidates", "retire_consumers_and_storage",
        "publish_host_services", "deploy_application",
    ]
    if failure:
        expected_phases = expected_phases[:expected_phases.index(failure) + 1]
    assert result.returncode == (27 if failure else 0), result.stderr
    assert Path(environment["PHASE_LOG"]).read_text().splitlines() == expected_phases
    assert not Path(Path(environment["CANDIDATE_LOG"]).read_text()).exists()
