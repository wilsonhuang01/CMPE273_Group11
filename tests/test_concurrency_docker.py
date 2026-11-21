"""Opt-in real Docker cohort overlap, drained benchmark windows and immutable measurements."""

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from compiler_optimizer.coordinator.round import CohortCoordinator
from compiler_optimizer.scheduler.resources import reservation_mib, runner_capacity
from compiler_optimizer.shared.benchmarking import BenchmarkPlan
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.baseline import run_baseline
from compiler_optimizer.workers.docker_runner import DockerRunner
from compiler_optimizer.workers.package import read_package
from compiler_optimizer.workers.targets import DEFAULT_IMAGES

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("CONCURRENCY_INTEGRATION_TESTS") != "1",
        reason="requires two Docker CPU slots plus resource headroom",
    ),
]


class InlineBroker:
    def exchange(self, proposal, consume, published):
        published()
        return consume(proposal)


class PortablePatch:
    def generate(self, request, sources):
        return json.dumps(
            {
                "schema_version": 1,
                "parent_source_sha256": request.source.bundle.sha256,
                "transformation": "portable-scalar-reduction",
                "rationale": "Exercise native verification and measurement with a portable edit.",
                "edits": [
                    {
                        "path": "main.c",
                        "old": "long long sum = 0;",
                        "new": "long long sum = 0; /* portable scalar reduction */",
                    }
                ],
            }
        ).encode()


class ObservedRunner(DockerRunner):
    def start_observation(self):
        self.observation = threading.Lock()
        self.barrier = threading.Barrier(2)
        self.seen = set()
        self.active_compilers = 0
        self.maximum_compilers = 0
        self.measured_windows = 0
        self.measuring = False

    def _invoke_reserved(self, payload, *, compile_mode, **kwargs):
        observing = hasattr(self, "observation") and compile_mode
        if not observing:
            return super()._invoke_reserved(payload, compile_mode=compile_mode, **kwargs)
        with self.observation:
            assert not self.measuring
            self.active_compilers += 1
            self.maximum_compilers = max(self.maximum_compilers, self.active_compilers)
            first = threading.get_ident() not in self.seen
            self.seen.add(threading.get_ident())
        try:
            if first:
                self.barrier.wait(timeout=15)
            return super()._invoke_reserved(payload, compile_mode=compile_mode, **kwargs)
        finally:
            with self.observation:
                self.active_compilers -= 1

    @contextmanager
    def measurement_window(self):
        with super().measurement_window():
            with self.observation:
                assert self.active_compilers == 0
                self.measuring = True
                self.measured_windows += 1
            try:
                yield
            finally:
                with self.observation:
                    self.measuring = False


def test_real_parallel_cohort_uses_isolated_windows_and_reuses_completed_evidence(tmp_path):
    target = os.environ.get("WORKER_TARGET", "linux-x86_64")
    runner = ObservedRunner(
        os.environ.get("COMPILER_IMAGE", DEFAULT_IMAGES[target]),
        target=target,
        watchdog_directory=tmp_path / "watchdogs",
    )
    repo = Repository(tmp_path / "state")
    package = read_package(Path(__file__).resolve().parents[1] / "examples/sum-array")
    if runner_capacity(runner).workers(reservation_mib(package.manifest), maximum=2) < 2:
        pytest.skip("engine cannot fit two reserved workers plus headroom")
    baseline = run_baseline(package, repo, runner)
    assert baseline.WhichOneof("outcome") == "success", baseline.failure
    runner.start_observation()
    coordinator = CohortCoordinator(
        repo, runner, InlineBroker(), concurrency=2, backends={"architecture": PortablePatch()}
    )
    task = baseline.meta.task_id
    audit = coordinator.run(
        task,
        agents=["performance", "architecture"],
        benchmark_plan=BenchmarkPlan(warmups=0, repetitions=7, minimum_sample_seconds=1.0),
    )
    assert audit["status"] == "retained_baseline", audit
    assert runner.maximum_compilers == 2
    assert runner.measured_windows == 2
    assert len(repo.list_benchmarks(task)) == 2
    assert repo.cohort_work(task)["state"] == "completed"
    assert coordinator.run(task) == audit
    assert runner.measured_windows == 2
