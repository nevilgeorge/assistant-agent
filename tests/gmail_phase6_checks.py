"""Safe, bounded performance and reachability measurements for Phase 6."""

from __future__ import annotations

import asyncio
import json
import shlex
import statistics
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from urllib.parse import urlsplit

from mcp_types.version import LATEST_HANDSHAKE_VERSION

from assistant_agent.sandbox import Sandbox, SandboxHandle

Assignment = TypeVar("Assignment")


class MeasurementFailure(RuntimeError):
    """A static measurement category, never an upstream response or error text."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def summarize_seconds(samples: list[float]) -> dict[str, float | int | None]:
    """Report small-sample statistics without suggesting a reliable tail estimate."""
    return {
        "samples": len(samples),
        "median_seconds": statistics.median(samples) if samples else None,
        "maximum_seconds": max(samples) if samples else None,
    }


def resource_usage(stats: dict[str, Any]) -> dict[str, float | int]:
    """Convert one Docker stats sample to memory usage and CPU percent."""
    current = stats.get("cpu_stats", {})
    previous = stats.get("precpu_stats", {})
    cpu_delta = (current.get("cpu_usage", {}).get("total_usage", 0)
                 - previous.get("cpu_usage", {}).get("total_usage", 0))
    system_delta = current.get("system_cpu_usage", 0) - previous.get("system_cpu_usage", 0)
    processors = current.get("online_cpus") or len(
        current.get("cpu_usage", {}).get("percpu_usage", [])
    ) or 1
    cpu_percent = max(0.0, cpu_delta / system_delta * processors * 100) if system_delta > 0 else 0.0
    return {"memory_bytes": stats.get("memory_stats", {}).get("usage", 0),
            "cpu_percent": cpu_percent}


async def run_baseline(
    start_one: Callable[[int], Awaitable[Assignment]],
    search_one: Callable[[Assignment], Awaitable[None]],
    close_one: Callable[[Assignment], Awaitable[None]],
    sample_resources: Callable[[], Awaitable[list[dict[str, float | int]]]],
    *, levels: tuple[int, ...] = (1, 2, 4), batches: int = 3, searches: int = 5,
) -> dict[str, Any]:
    """Measure fresh assignments and searches, cleaning partial batches on failure."""
    report: dict[str, Any] = {"data_source": "synthetic_google", "levels": []}
    next_index = 0
    for concurrency in levels:
        startup_samples: list[float] = []
        search_samples: list[float] = []
        peak_memory = 0
        peak_cpu = 0.0
        resource_samples = 0
        sampling_errors = 0
        errors = 0
        failure_categories: dict[str, int] = {}

        def record_failures(outcomes: list[Any]) -> None:
            nonlocal errors
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    errors += 1
                    category = (outcome.category if isinstance(outcome, MeasurementFailure)
                                else type(outcome).__name__)
                    failure_categories[category] = failure_categories.get(category, 0) + 1
        for _ in range(batches):
            assignments: list[Assignment] = []
            stop_sampling = asyncio.Event()

            async def sample() -> None:
                nonlocal peak_memory, peak_cpu, resource_samples, sampling_errors
                while not stop_sampling.is_set():
                    try:
                        async with asyncio.timeout(5):
                            measurements = await sample_resources()
                        resource_samples += len(measurements)
                        peak_memory = max(peak_memory, max(
                            (int(item["memory_bytes"]) for item in measurements), default=0,
                        ))
                        peak_cpu = max(peak_cpu, max(
                            (float(item["cpu_percent"]) for item in measurements), default=0.0,
                        ))
                    except Exception:
                        sampling_errors += 1
                    try:
                        await asyncio.wait_for(stop_sampling.wait(), 0.25)
                    except TimeoutError:
                        pass

            async def start(index: int) -> Assignment:
                began = time.monotonic()
                assignment = await start_one(index)
                assignments.append(assignment)
                startup_samples.append(time.monotonic() - began)
                return assignment

            async def search(assignment: Assignment) -> None:
                for _ in range(searches):
                    began = time.monotonic()
                    await search_one(assignment)
                    search_samples.append(time.monotonic() - began)

            sampler = asyncio.create_task(sample())
            try:
                results = await asyncio.gather(
                    *(start(index) for index in range(next_index, next_index + concurrency)),
                    return_exceptions=True,
                )
                next_index += concurrency
                record_failures(results)
                outcomes = await asyncio.gather(
                    *(search(assignment) for assignment in assignments), return_exceptions=True,
                )
                record_failures(outcomes)
            finally:
                stop_sampling.set()
                await sampler
                outcomes = await asyncio.gather(
                    *(close_one(assignment) for assignment in assignments), return_exceptions=True,
                )
                record_failures(outcomes)
        report["levels"].append({
            "concurrency": concurrency, "batches": batches,
            "startup": summarize_seconds(startup_samples),
            "search": summarize_seconds(search_samples), "errors": errors,
            "failure_categories": failure_categories,
            "peak_observed_container_memory_bytes": peak_memory,
            "peak_observed_container_cpu_percent": peak_cpu,
            "resource_samples": resource_samples, "sampling_errors": sampling_errors,
        })
    return report


async def benchmark_harness(harness: Any) -> dict[str, Any]:
    """Measure the full-app fixture without introducing a production metrics API."""
    async def start_one(index: int) -> Any:
        user = await harness.connect(f"benchmark-{index}")
        response = await user.client.post(
            "/api/message", json={"message": "Reply with only OK."},
            headers={"x-csrf-token": user.csrf},
        )
        assert response.status_code == 202, "Benchmark startup failed"
        return user

    async def search_one(user: Any) -> None:
        grant = harness.grants[user.user_id]
        response = await user.client.post(
            "/mcp/gmail", headers={
                "Authorization": f"Bearer {grant.raw_token}",
                "Accept": "application/json, text/event-stream",
                # Match ClientSession's initialized HTTP protocol, rather than the
                # SDK's newer handshake-free envelope used by LATEST_PROTOCOL_VERSION.
                "MCP-Protocol-Version": LATEST_HANDSHAKE_VERSION,
            }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "search_emails", "arguments": {"query": "fixture", "page_size": 2},
            }},
        )
        if response.status_code != 200:
            raise MeasurementFailure(f"http_status_{response.status_code}")
        payload = response.json()
        if "error" in payload:
            error_code = payload["error"].get("code")
            category = f"rpc_code_{error_code}" if isinstance(error_code, int) else "rpc_error"
            raise MeasurementFailure(category)
        if payload["result"].get("isError"):
            raise MeasurementFailure("tool_error")

    async def close_one(user: Any) -> None:
        async with asyncio.timeout(125):
            while (await user.client.get("/api/conversation")).json().get("active_turn"):
                await asyncio.sleep(0.1)
        await harness.reset(user)

    async def sample_resources() -> list[dict[str, float | int]]:
        handles = list(harness.service._assignments.values())
        async def sample(handle: SandboxHandle) -> dict[str, float | int]:
            container = await harness.docker.containers.get(handle.container_id)
            samples = await container.stats(stream=False)
            return resource_usage(samples[0])
        results = await asyncio.gather(*(sample(handle) for handle in handles), return_exceptions=True)
        return [item for item in results if isinstance(item, dict)]

    return await run_baseline(start_one, search_one, close_one, sample_resources)


# Probe output is deliberately limited to DNS/TCP outcome and HTTP status. It
# never requests IMDS tokens, authenticates to databases, or reads response bodies.
PROBE_PROGRAM = r"""
const dns=require('dns').promises,net=require('net'),http=require('http');
(async()=>{const targets=JSON.parse(process.env.ASSISTANT_PROBE_TARGETS);const results=[];
for(const target of targets){const result={name:target.name,host:target.host,port:target.port};
try{await dns.lookup(target.host);result.dns='resolved'}catch{result.dns='unresolved'}
result.tcp=await new Promise(resolve=>{const socket=net.connect(target.port,target.host);
let done=false;const finish=value=>{if(!done){done=true;socket.destroy();resolve(value)}};
socket.setTimeout(1500,()=>finish('timeout'));socket.on('connect',()=>finish('connected'));
socket.on('error',()=>finish('unreachable'))});
if(target.path&&result.tcp==='connected'){result.http_status=await new Promise(resolve=>{
const request=http.get({hostname:target.host,port:target.port,path:target.path,timeout:1500},
response=>{resolve(response.statusCode);response.destroy()});
request.on('timeout',()=>{request.destroy();resolve(null)});request.on('error',()=>resolve(null))})}
results.push(result)}console.log(JSON.stringify(results))})().catch(()=>process.exit(1));
"""


async def probe_targets(
    service: Sandbox, handle: SandboxHandle, targets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Run secret-free bounded probes from the sandbox, rather than from the host."""
    result = await service.run(
        f"node -e {shlex.quote(PROBE_PROGRAM)}", name=handle.container_id,
        environment={"ASSISTANT_PROBE_TARGETS": json.dumps(targets)},
        timeout=len(targets) * 4 + 5,
    )
    assert result.ok, "Reachability probe failed"
    outcomes = json.loads(result.output)
    assert isinstance(outcomes, list) and len(outcomes) == len(targets)
    return outcomes


async def audit_harness(harness: Any) -> dict[str, Any]:
    """Audit assigned containers with a controlled sibling listener and no secrets."""
    user = await harness.connect("reachability")
    await harness.submit(user, "Reply with only OK.")
    handle = harness.app.state.conversation_manager.get(user.user_id).sandbox_handle
    peer = await harness.docker.containers.create({
        "Image": harness.image_id, "User": "agent",
        "Cmd": ["node", "-e", "require('http').createServer((q,r)=>r.end()).listen(8765,'0.0.0.0')"],
        "HostConfig": {"NetworkMode": harness.network_name, "CapDrop": ["ALL"],
                       "SecurityOpt": ["no-new-privileges:true"]},
        "NetworkingConfig": {"EndpointsConfig": {
            harness.network_name: {"Aliases": ["phase6-peer"]},
        }},
    })
    try:
        await peer.start()
        targets = [
            {"name": "app_health", "host": "app", "port": 8000, "path": "/healthz"},
            {"name": "app_browser_without_cookie", "host": "app", "port": 8000,
             "path": "/api/conversation"},
            {"name": "mcp_without_bearer", "host": "app", "port": 8000, "path": "/mcp/gmail"},
            {"name": "backend_database", "host": "db", "port": 5432},
            {"name": "controlled_sibling", "host": "phase6-peer", "port": 8765, "path": "/"},
            {"name": "host_app", "host": "host.docker.internal",
             "port": urlsplit(harness.base_url).port, "path": "/healthz"},
            {"name": "host_database", "host": "host.docker.internal", "port": 5432},
            {"name": "metadata_address_tcp_only", "host": "169.254.169.254", "port": 80},
        ]
        probes = await probe_targets(harness.service, handle, targets)
        by_name = {probe["name"]: probe for probe in probes}
        assert by_name["app_health"].get("http_status") == 200
        assert by_name["app_browser_without_cookie"].get("http_status") == 401
        assert by_name["mcp_without_bearer"].get("http_status") == 401
        container = await harness.docker.containers.get(handle.container_id)
        mounts = container["HostConfig"].get("Binds", [])
        assert mounts == [f"{handle.host_input_path}:/input:ro"]
        environment_names = [entry.split("=", 1)[0] for entry in container["Config"].get("Env", [])]
        assert not any(name.startswith("GOOGLE") or name in {
            "DATABASE_URL", "CREDENTIAL_ENCRYPTION_KEY", "ASSISTANT_MCP_TOKEN",
            "ANTHROPIC_API_KEY",
        } for name in environment_names)
        boundary = await harness.service.run(
            "test ! -e /var/run/docker.sock && ! touch /input/phase6-write-must-fail",
            name=handle.container_id,
        )
        assert boundary.ok and not (handle.app_input_path / "phase6-write-must-fail").exists()
        report = {"environment": "isolated_local_full_app_harness", "probes": probes,
                  "input_read_only": True, "google_credentials_absent": True,
                  "docker_socket_absent": True, "ec2_metadata_policy": "unverified_locally"}
        return report
    finally:
        try:
            await peer.delete(force=True)
        finally:
            await harness.reset(user)
