"""Compose agents with private volumes and TLS, real Docker sandboxes and frozen selection."""

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from compiler_optimizer.agents.performance import FilePatchBackend
from compiler_optimizer.agents.rpc_client import AgentClient
from compiler_optimizer.coordinator.round import CohortCoordinator
from compiler_optimizer.runtime.config import Settings
from compiler_optimizer.shared.agents_rpc import AGENT_PORTS
from compiler_optimizer.shared.benchmarking import BenchmarkPlan
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.baseline import run_baseline
from compiler_optimizer.workers.docker_runner import DockerRunner
from compiler_optimizer.workers.package import read_package
from compiler_optimizer.workers.targets import DEFAULT_IMAGES

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("AGENT_INTEGRATION_TESTS") != "1",
        reason="requires healthy Compose agent services",
    ),
]
ROOT = Path(__file__).resolve().parents[1]


class InlineBroker:
    def exchange(self, proposal, consume, published):
        published()
        return consume(proposal)


def test_compose_agents_use_tls_and_unshared_artifacts_for_real_cohort(tmp_path):
    certificates = tmp_path / "certificates"
    certificates.mkdir()
    subprocess.run(
        ["docker", "compose", "cp", "agent-performance:/agent-ca/.", str(certificates)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        timeout=15,
    )
    repo = Repository(tmp_path / "coordinator")
    settings = replace(
        Settings("coordinator"),
        agent_backend="rpc",
        agent_tls=True,
        agent_ca_directory=certificates,
        agent_timeout_seconds=30,
        agent_token=os.environ.get("AGENT_API_TOKEN", "optimizer-agent-dev-token-change-me-0001"),
        agent_endpoints=json.dumps(
            {
                name: f"127.0.0.1:{os.environ.get('AGENT_' + name.upper() + '_PORT', port)}"
                for name, port in AGENT_PORTS.items()
            }
        ),
    )
    client = AgentClient(repo, settings)
    client.check_health()
    target = os.environ.get("WORKER_TARGET", "linux-x86_64")
    runner = DockerRunner(
        os.environ.get("COMPILER_IMAGE", DEFAULT_IMAGES[target]),
        target=target,
        watchdog_directory=tmp_path / "watchdogs",
    )
    package = read_package(ROOT / "examples/sum-array")
    baseline = run_baseline(package, repo, runner)
    assert baseline.WhichOneof("outcome") == "success", baseline.failure
    patch = tmp_path / "portable.json"
    patch.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "parent_source_sha256": baseline.source.bundle.sha256,
                "transformation": "rpc-portable-scalar",
                "rationale": "A portable source change for RPC validation.",
                "edits": [
                    {
                        "path": "main.c",
                        "old": "long long sum = 0;",
                        "new": "long long sum = 0; /* portable scalar reduction */",
                    }
                ],
            }
        )
    )
    coordinator = CohortCoordinator(
        repo,
        runner,
        InlineBroker(),
        agent_client=client,
        backends={"architecture": FilePatchBackend(patch)},
    )
    task = baseline.meta.task_id
    audit = coordinator.run(
        task,
        agents=["performance", "architecture"],
        benchmark_plan=BenchmarkPlan(warmups=0, repetitions=7, minimum_sample_seconds=1.0),
    )
    assert audit["status"] == "retained_baseline", audit
    assert len(repo.list_benchmarks(task)) == 2
    calls = repo.list_agent_rpc_calls(task)
    assert len(calls) == 8 and all(c["status"] == "completed" for c in calls)
    assert all(p.HasField("agent_provenance") for p in repo.list_proposals(task))
    assert coordinator.run(task) == audit
    assert len(repo.list_agent_rpc_calls(task)) == 8
