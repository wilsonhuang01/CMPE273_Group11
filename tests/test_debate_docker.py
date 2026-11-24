"""Opt-in rejected C candidate -> revised source -> real sandbox revalidation."""

import json
import os
from pathlib import Path

import pytest

from compiler_optimizer.coordinator.debate import DebateCoordinator
from compiler_optimizer.shared.benchmarking import BenchmarkPlan
from compiler_optimizer.shared.debate import DebatePolicy
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.docker_runner import DockerRunner
from compiler_optimizer.workers.package import read_package
from compiler_optimizer.workers.targets import DEFAULT_IMAGES

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("DEBATE_INTEGRATION_TESTS") != "1",
        reason="requires the native Docker compiler image",
    ),
]


class Broker:
    def exchange(self, proposal, consume, published):
        published()
        return consume(proposal)


class Repair:
    def __init__(self):
        self.requests = []

    def generate(self, request, sources):
        self.requests.append(request)
        revision = request.HasField("revision_context_json")
        return json.dumps(
            {
                "schema_version": 1,
                "parent_source_sha256": request.source.bundle.sha256,
                "transformation": "repaired-scalar-sum" if revision else "broken-scalar-sum",
                "rationale": "Repair the reported output mismatch."
                if revision
                else "Test rejection.",
                "edits": [
                    {
                        "path": "main.c",
                        "old": "long long sum = 0;",
                        "new": "long long sum = 0; /* revised */"
                        if revision
                        else "long long sum = 1;",
                    }
                ],
            }
        ).encode()


def test_rejected_candidate_revised_and_reverified_on_native_docker(tmp_path):
    target = os.environ.get("WORKER_TARGET", "linux-x86_64")
    runner = DockerRunner(
        os.environ.get("COMPILER_IMAGE", DEFAULT_IMAGES[target]),
        target=target,
        watchdog_directory=tmp_path / "watchdogs",
    )
    repo = Repository(tmp_path / "state")
    backend = Repair()
    coordinator = DebateCoordinator(
        repo,
        runner,
        Broker(),
        backends={"performance": backend},
        debate_policy=DebatePolicy(heldout_cases=({"stdin": "7\n1 2 3 4 5 6 7\n", "args": []},)),
    )
    report = coordinator.optimize(
        read_package(Path(__file__).parents[1] / "examples/sum-array"),
        agents=["performance"],
        benchmark_plan=BenchmarkPlan(warmups=0, repetitions=7),
    )
    task = coordinator.last_task_id
    assert report["status"] in ("selected", "retained_baseline"), report
    assert len(backend.requests) == 2
    feedback = json.loads(backend.requests[1].revision_context_json)
    assert feedback["feedback"]["failure"]["reason"] == "OUTPUT_MISMATCH"
    results = [r for r in repo.list_results(task) if r.WhichOneof("subject") == "proposal_id"]
    assert [r.WhichOneof("outcome") for r in results] == ["failure", "success"]
    assert len(repo.list_benchmarks(task)) == 1
    jobs = len(repo.list_jobs(task))
    assert coordinator.run(task) == report
    assert len(repo.list_jobs(task)) == jobs and len(repo.list_benchmarks(task)) == 1
