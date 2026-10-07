"""Check measurement accuracy and partial-batch cleanup without Docker/model calls."""

import asyncio

import pytest

from gmail_phase6_checks import resource_usage, run_baseline, summarize_seconds


def test_resource_cpu_delta_and_missing_sample_fields():
    assert resource_usage({}) == {"memory_bytes": 0, "cpu_percent": 0.0}
    stats = {
        "cpu_stats": {"cpu_usage": {"total_usage": 110}, "system_cpu_usage": 2000,
                      "online_cpus": 4},
        "precpu_stats": {"cpu_usage": {"total_usage": 10}, "system_cpu_usage": 1000},
        "memory_stats": {"usage": 1234},
    }
    assert resource_usage(stats) == {"memory_bytes": 1234, "cpu_percent": 40.0}
    stats["cpu_stats"]["system_cpu_usage"] = 0
    assert resource_usage(stats)["cpu_percent"] == 0


def test_small_sample_statistics_report_no_fabricated_tail():
    assert summarize_seconds([]) == {"samples": 0, "median_seconds": None, "maximum_seconds": None}
    assert summarize_seconds([1, 3, 2]) == {
        "samples": 3, "median_seconds": 2, "maximum_seconds": 3,
    }


async def test_baseline_counts_samples_cleans_batches_and_omits_identifiers():
    closed = []
    async def start(index):
        await asyncio.sleep(0)
        return f"private-user-{index}"
    async def search(assignment):
        await asyncio.sleep(0)
    async def close(assignment):
        closed.append(assignment)
    async def resources():
        return [{"memory_bytes": 100, "cpu_percent": 25}]
    report = await run_baseline(start, search, close, resources, levels=(1, 2), batches=2, searches=3)
    assert len(closed) == 6
    for level in report["levels"]:
        assert level["startup"]["samples"] == level["concurrency"] * 2
        assert level["search"]["samples"] == level["concurrency"] * 2 * 3
        assert level["errors"] == level["sampling_errors"] == 0
        assert level["peak_observed_container_memory_bytes"] == 100
        assert level["peak_observed_container_cpu_percent"] == 25
    assert "private-user" not in str(report)


@pytest.mark.parametrize("stage", ["startup", "search", "cleanup", "sampling"])
async def test_baseline_partial_failures_are_safe_and_cleanup_still_runs(stage):
    closed = []
    async def start(index):
        if stage == "startup" and index == 1:
            raise RuntimeError("private-credential")
        return index
    async def search(assignment):
        if stage == "search":
            raise RuntimeError("private-message")
    async def close(assignment):
        closed.append(assignment)
        if stage == "cleanup":
            raise RuntimeError("private-path")
    async def resources():
        if stage == "sampling":
            raise RuntimeError("private-host")
        return []
    report = await run_baseline(start, search, close, resources, levels=(2,), batches=1, searches=1)
    assert len(closed) == (1 if stage == "startup" else 2)
    level = report["levels"][0]
    assert (level["sampling_errors"] > 0) if stage == "sampling" else level["errors"] > 0
    assert "private" not in str(report)
