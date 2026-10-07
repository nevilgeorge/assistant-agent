"""Dedicated lifecycle tests use Docker-shaped fakes, never model calls."""

import asyncio
import io
import stat
import tarfile
import uuid
from importlib import resources
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiodocker
import pytest

from assistant_agent.config import ConfigError, SandboxSettings
from assistant_agent.sandbox import (
    CONVERSATION_LABEL,
    DEPLOYMENT_LABEL,
    OWNER_LABEL,
    Sandbox,
    SandboxError,
)


class Container(dict):
    def __init__(self, name: str, config: dict) -> None:
        super().__init__(State={"Status": "running"}, Labels=config.get("Labels", {}), Config=config)
        self.id = name + "-id"
        self.start = AsyncMock()
        self.delete = AsyncMock()
        self.put_archive = AsyncMock()


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    settings = SandboxSettings(host_input_root=Path("/host/session-inputs"), app_input_root=tmp_path)
    containers = {}
    configs = []

    async def create(config, *, name):
        configs.append(config)
        container = Container(name, config)
        containers[name] = containers[container.id] = container
        return container

    async def get(name):
        if name not in containers:
            raise aiodocker.DockerError(404, {"message": "missing"})
        return containers[name]

    client = SimpleNamespace(
        version=AsyncMock(), images=SimpleNamespace(inspect=AsyncMock()),
        networks=SimpleNamespace(get=AsyncMock()), close=AsyncMock(),
        containers=SimpleNamespace(create=AsyncMock(side_effect=create),
                                   get=AsyncMock(side_effect=get),
                                   list=AsyncMock(return_value=[])),
    )
    monkeypatch.setattr("assistant_agent.sandbox.aiodocker.Docker", lambda **kwargs: client)
    service = Sandbox(settings, max_sessions=2)
    service.readiness = AsyncMock()
    return service, client, containers, configs


async def test_assignments_input_mounts_and_hardening(lifecycle):
    service, client, _, configs = lifecycle
    first = await service.allocate("user1", uuid.uuid4().hex)
    second = await service.allocate("user2", uuid.uuid4().hex)
    assert first.container_id != second.container_id
    assert first.host_input_path != second.host_input_path
    assert first.host_input_path == service.settings.host_input_root / first.conversation_id
    assert first.app_input_path == service.settings.app_input_root / first.conversation_id
    assert not (first.app_input_path / "input").exists()
    assert stat.S_IMODE(first.app_input_path.stat().st_mode) == 0o755
    assert stat.S_IMODE(first.app_input_path.parent.stat().st_mode) == 0o700
    assert service.capacity_used == 2
    with pytest.raises(SandboxError, match="slots"):
        await service.allocate("user3", uuid.uuid4().hex)
    config = configs[0]
    host = config["HostConfig"]
    assert host["Binds"] == [f"{first.host_input_path}:/input:ro"]
    assert host["Memory"] == 2 * 1024**3 and host["NanoCpus"] == 1_000_000_000
    assert host["PidsLimit"] == 2048 and host["Init"]
    assert host["CapDrop"] == ["ALL"]
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    assert host["RestartPolicy"] == {"Name": "no"}
    assert config["User"] == "agent" and "Env" not in config
    assert "PortBindings" not in host and "Volumes" not in config
    client.images.inspect.assert_awaited_with(service.settings.image)
    await service.destroy(first)
    assert service.capacity_used == 1 and not first.app_input_path.exists()
    await service.close()
    assert not second.app_input_path.exists()


@pytest.mark.parametrize("stage", ["directory", "create", "start", "readiness", "context"])
async def test_failed_allocation_cleans_acquired_resources(lifecycle, monkeypatch, stage):
    service, client, _, _ = lifecycle
    conversation_id = uuid.uuid4().hex
    if stage == "directory":
        monkeypatch.setattr(service, "_prepare_input", lambda _: (_ for _ in ()).throw(OSError()))
    elif stage == "create":
        client.containers.create.side_effect = RuntimeError("create failed")
    elif stage == "start":
        original = client.containers.create.side_effect

        async def create(config, *, name):
            container = await original(config, name=name)
            container.start.side_effect = RuntimeError("start failed")
            return container

        client.containers.create.side_effect = create
    elif stage == "readiness":
        service.readiness.side_effect = RuntimeError("readiness failed")
    else:
        original = client.containers.create.side_effect

        async def create(config, *, name):
            container = await original(config, name=name)
            container.put_archive.side_effect = RuntimeError("upload failed")
            return container

        client.containers.create.side_effect = create
    with pytest.raises(SandboxError, match="allocation"):
        await service.allocate("user", conversation_id)
    assert service.capacity_used == 0
    assert not (service.settings.app_input_root / conversation_id).exists()
    await service.close()


async def test_context_archive_is_exact_and_uploaded_after_readiness(lifecycle):
    service, _, containers, _ = lifecycle

    async def readiness(container_id: str) -> None:
        container = containers[container_id]
        container.start.assert_awaited_once()
        container.put_archive.assert_not_awaited()

    service.readiness.side_effect = readiness
    handle = await service.allocate("user", uuid.uuid4().hex)
    container = containers[handle.container_id]
    container.put_archive.assert_awaited_once()
    destination, archive_bytes = container.put_archive.call_args.args
    assert destination == "/workspace"
    with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as archive:
        assert archive.getnames() == ["CLAUDE.md"]
        member = archive.getmember("CLAUDE.md")
        assert member.isfile() and member.mode == 0o644
        assert member.uid == member.gid == 0
        assert archive.extractfile(member).read() == resources.files(
            "assistant_agent.agent_kit"
        ).joinpath("CLAUDE.md").read_bytes()
    assert not (handle.app_input_path / "CLAUDE.md").exists()
    await service.close()


async def test_context_upload_obeys_allocation_deadline(lifecycle, monkeypatch):
    service, client, containers, _ = lifecycle
    monkeypatch.setattr("assistant_agent.sandbox.ALLOCATION_TIMEOUT_SECONDS", 0.05)
    original = client.containers.create.side_effect

    async def create(config: dict, *, name: str) -> Container:
        container = await original(config, name=name)
        container.put_archive.side_effect = stalled_upload
        return container

    async def stalled_upload(*args) -> None:
        await asyncio.Event().wait()

    client.containers.create.side_effect = create
    conversation_id = uuid.uuid4().hex
    with pytest.raises((SandboxError, TimeoutError)):
        await service.allocate("user", conversation_id)
    containers[service._name(conversation_id)].delete.assert_awaited_once_with(force=True)
    assert service.capacity_used == 0
    assert not (service.settings.app_input_root / conversation_id).exists()
    await service.close()


async def test_context_read_failure_cleans_up_and_logs_static_error(lifecycle, monkeypatch, caplog):
    service, _, containers, _ = lifecycle
    monkeypatch.setattr(
        "assistant_agent.sandbox.resources.files",
        lambda _: (_ for _ in ()).throw(OSError("sensitive read details")),
    )
    conversation_id = uuid.uuid4().hex
    with pytest.raises(SandboxError, match="allocation"):
        await service.allocate("user", conversation_id)
    container = containers[service._name(conversation_id)]
    container.put_archive.assert_not_awaited()
    container.delete.assert_awaited_once_with(force=True)
    assert service.capacity_used == 0
    assert not (service.settings.app_input_root / conversation_id).exists()
    assert "Sandbox context provisioning failed" in caplog.text
    assert "sensitive read details" not in caplog.text
    await service.close()


async def test_replacement_container_reads_current_packaged_context(lifecycle, monkeypatch, tmp_path):
    service, _, containers, _ = lifecycle
    package_path = tmp_path / "package"
    package_path.mkdir()
    context_path = package_path / "CLAUDE.md"
    monkeypatch.setattr("assistant_agent.sandbox.resources.files", lambda _: package_path)
    conversation_id = uuid.uuid4().hex
    for context_bytes in (b"first version\n", b"updated version\n"):
        context_path.write_bytes(context_bytes)
        handle = await service.allocate("user", conversation_id)
        container = containers[handle.container_id]
        container.put_archive.assert_awaited_once()
        with tarfile.open(fileobj=io.BytesIO(container.put_archive.call_args.args[1])) as archive:
            assert archive.extractfile("CLAUDE.md").read() == context_bytes
        await service.destroy(handle)
    await service.close()


async def test_cleanup_failure_retains_capacity_and_input_until_retry(lifecycle):
    service, _, containers, _ = lifecycle
    handle = await service.allocate("user", uuid.uuid4().hex)
    container = containers[handle.container_id]
    container.delete.side_effect = RuntimeError("daemon down")
    with pytest.raises(SandboxError, match="retained"):
        await service.destroy(handle)
    assert service.capacity_used == 1 and handle.app_input_path.exists()
    with pytest.raises(SandboxError, match="cleanup"):
        await service.allocate("other", uuid.uuid4().hex)
    container.delete.side_effect = None
    await service.retry_cleanup()
    assert service.capacity_used == 0 and not handle.app_input_path.exists()
    await service.close()


async def test_cancelled_allocation_and_destruction_finish_independently(lifecycle):
    service, _, containers, _ = lifecycle
    ready = asyncio.Event()
    service.readiness.side_effect = lambda _: ready.wait()
    # AsyncMock does not await a coroutine returned by a synchronous side effect.
    async def readiness(_):
        await ready.wait()
    service.readiness.side_effect = readiness
    conversation_id = uuid.uuid4().hex
    allocation = asyncio.create_task(service.allocate("user", conversation_id))
    while not containers:
        await asyncio.sleep(0)
    allocation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await allocation
    ready.set()
    while service._tasks:
        await asyncio.sleep(0)
    handle = service._assignments[conversation_id]
    finish = asyncio.Event()
    async def delete(**kwargs):
        await finish.wait()
    containers[handle.container_id].delete.side_effect = delete
    destruction = asyncio.create_task(service.destroy(handle))
    while not containers[handle.container_id].delete.await_count:
        await asyncio.sleep(0)
    destruction.cancel()
    with pytest.raises(asyncio.CancelledError):
        await destruction
    assert service.capacity_used == 1
    finish.set()
    while service._tasks:
        await asyncio.sleep(0)
    assert service.capacity_used == 0
    await service.close()


async def test_reconcile_only_owned_deployment_and_generated_directories(lifecycle):
    service, client, containers, _ = lifecycle
    old_id = uuid.uuid4().hex
    other_id = uuid.uuid4().hex
    old = Container("old", {"Labels": {OWNER_LABEL: "assistant-agent",
                     DEPLOYMENT_LABEL: "local", CONVERSATION_LABEL: old_id}})
    other = Container("other", {"Labels": {OWNER_LABEL: "assistant-agent",
                     DEPLOYMENT_LABEL: "elsewhere", CONVERSATION_LABEL: other_id}})
    containers[old.id] = old
    client.containers.list.return_value = [old, other]
    service._prepare_input(old_id)
    service._prepare_input(other_id)
    unrelated = service.settings.app_input_root / "notes"
    unrelated.write_text("keep")
    await service.reconcile()
    old.delete.assert_awaited_once_with(force=True)
    other.delete.assert_not_awaited()
    assert unrelated.read_text() == "keep"
    assert not (service.settings.app_input_root / old_id).exists()
    assert (service.settings.app_input_root / other_id).exists()
    await service.close()


async def test_daemon_outage_blocks_allocation_until_reconciled(lifecycle):
    service, client, _, _ = lifecycle
    client.containers.list.side_effect = RuntimeError("offline")
    with pytest.raises(SandboxError, match="reconciliation"):
        await service.allocate("user", uuid.uuid4().hex)
    client.containers.create.assert_not_awaited()
    client.containers.list.side_effect = None
    await service.allocate("user", uuid.uuid4().hex)
    await service.close()


async def test_generated_symlink_rejected(lifecycle, tmp_path):
    service, _, _, _ = lifecycle
    conversation_id = uuid.uuid4().hex
    (tmp_path / conversation_id).symlink_to(tmp_path / "elsewhere")
    with pytest.raises(SandboxError, match="reconciliation"):
        await service.reconcile()
    assert (tmp_path / conversation_id).is_symlink()
    await service.close()


@pytest.mark.parametrize("kwargs", [
    {"host_input_root": Path("relative")}, {"app_input_root": Path("/")},
    {"deployment_id": "bad/name"}, {"image": ""}, {"network": "two names"},
])
def test_settings_validation(kwargs):
    with pytest.raises(ConfigError):
        SandboxSettings(**kwargs)


async def test_ambiguous_create_response_recovers_by_deterministic_name(lifecycle):
    service, client, containers, _ = lifecycle
    original = client.containers.create.side_effect

    async def create(config, *, name):
        await original(config, name=name)
        raise RuntimeError("lost creation response")

    client.containers.create.side_effect = create
    conversation_id = uuid.uuid4().hex
    with pytest.raises(SandboxError, match="allocation"):
        await service.allocate("user", conversation_id)
    containers[service._name(conversation_id)].delete.assert_awaited_once_with(force=True)
    assert service.capacity_used == 0
    await service.close()
