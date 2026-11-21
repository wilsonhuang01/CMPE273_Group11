"""Capacity bounds, real overlap, engine isolation and durable cancellation barriers."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from test_fleet import claim, enqueue, result
from test_fleet import fleet as fleet_fixture
from test_recovery import setup

from compiler_optimizer.scheduler.resources import Capacity, engine_window
from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.storage import Repository
from compiler_optimizer.storage.errors import StateError
from compiler_optimizer.workers.benchmark import run_benchmark

fleet = fleet_fixture


def telemetry(cpus=4, memory_mib=4096):
    return json.dumps({"docker_engine": {"cpus": cpus, "memory_bytes": memory_mib * 1024**2}})


def concurrent_setup(tmp_path):
    values = setup(tmp_path)
    for runner in values[1:3]:
        runner.hardware = telemetry()
    return values


def test_capacity_reserves_headroom_and_worst_case_memory():
    capacity = Capacity.parse(telemetry())
    assert capacity == Capacity(3, 3584)
    assert capacity.workers(1024) == 3
    assert capacity.workers(2048) == 1
    assert capacity.workers(512, maximum=2) == 2
    with pytest.raises(ContractError, match="headroom"):
        Capacity.parse(telemetry(cpus=1))
    with pytest.raises(ContractError, match="capacity"):
        capacity.workers(4096)


def test_parallel_preparation_drains_before_measurement_and_resumes(tmp_path):
    repo, first, second, task, _, _, coordinator, _ = concurrent_setup(tmp_path)
    barrier = threading.Barrier(2)
    state_lock = threading.Lock()
    active = 0
    overlap = []
    for runner in (first, second):
        original = runner.compile
        waited = [False]

        def compile(*args, original=original, waited=waited, **kwargs):
            nonlocal active
            with state_lock:
                active += 1
                overlap.append(active)
            try:
                if not waited[0]:
                    waited[0] = True
                    barrier.wait(timeout=5)
                return original(*args, **kwargs)
            finally:
                with state_lock:
                    active -= 1

        runner.compile = compile
        environment = runner.benchmark_environment

        def probe(cpu=None, environment=environment):
            assert active == 0
            work = repo.cohort_work(task)
            assert work["state"] == "measuring"
            assert all(m["state"] == "ready" for m in work["members"])
            return environment(cpu)

        runner.benchmark_environment = probe
    report = coordinator.run(task, agents=["performance", "architecture"])
    assert report["status"] == "selected", report
    assert max(overlap) == 2
    work = repo.cohort_work(task)
    assert work["workers"] == 2 and work["state"] == "completed"
    assert all(m["state"] == "completed" for m in work["members"])
    jobs = [j for j in repo.list_jobs(task) if j.WhichOneof("subject") == "proposal_id"]
    assert len(jobs) == 2
    before = list(first.events), list(second.events)
    assert coordinator.run(task) == report
    assert before == (first.events, second.events)


def test_generation_overlaps_with_bounded_workers(tmp_path):
    from compiler_optimizer.agents.architecture import ExampleArchitectureBackend
    from compiler_optimizer.agents.performance import ExamplePerformanceBackend

    values = concurrent_setup(tmp_path)
    coordinator, task = values[6], values[3]
    barrier = threading.Barrier(2)

    class Backend:
        def __init__(self, inner):
            self.inner = inner

        def generate(self, request, sources):
            barrier.wait(timeout=5)
            return self.inner.generate(request, sources)

    coordinator.backends = {
        "performance": Backend(ExamplePerformanceBackend()),
        "architecture": Backend(ExampleArchitectureBackend()),
    }
    assert coordinator.run(task, agents=list(coordinator.backends))["status"] == "selected"


def test_cancellation_drains_preparation_and_is_durable(tmp_path):
    repo, first, second, task, _, _, coordinator, _ = concurrent_setup(tmp_path)
    started, release = threading.Event(), threading.Event()
    original = first.compile

    def compile(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    first.compile = compile
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(coordinator.run, task, agents=["performance", "architecture"])
        assert started.wait(5)
        repo.cancel_cohort(task)
        assert not future.done()
        release.set()
        assert future.result(timeout=10)["status"] == "cancelled"
    assert not repo.list_benchmarks(task)
    reopened = Repository(repo.root)
    assert reopened.cohort_work(task)["state"] == "cancelled"
    assert all(m["state"] == "cancelled" for m in reopened.cohort_work(task)["members"])
    before = list(first.events), list(second.events)
    assert coordinator.run(task)["status"] == "cancelled"
    assert before == (first.events, second.events)
    assert repo.get_decision(task) is None


def test_local_measurement_waits_for_work_and_blocks_other_process_handles():
    engine = str(uuid4())
    capacity = Capacity(2, 1024)
    started, release, measuring, finish = (threading.Event() for _ in range(4))

    def work():
        with engine_window(engine, capacity, memory_mib=256):
            started.set()
            assert release.wait(5)

    def measure():
        with engine_window(engine, capacity, exclusive=True):
            measuring.set()
            # Ordinary invocations inside the owning measurement thread are reentrant.
            with engine_window(engine, capacity):
                assert finish.wait(5)

    with ThreadPoolExecutor(max_workers=3) as executor:
        normal = executor.submit(work)
        assert started.wait(5)
        measurement = executor.submit(measure)
        assert not measuring.wait(0.1)
        release.set()
        assert measuring.wait(5)
        competitor = executor.submit(lambda: enter_with_deadline(engine, capacity))
        assert competitor.result(timeout=2) == "blocked"
        finish.set()
        normal.result(timeout=5)
        measurement.result(timeout=5)


def enter_with_deadline(engine, capacity):
    try:
        with engine_window(engine, capacity, timeout=0.1):
            return "entered"
    except TimeoutError:
        return "blocked"


def test_local_memory_reservations_limit_shared_overlap():
    engine, capacity = str(uuid4()), Capacity(4, 512)
    with engine_window(engine, capacity, memory_mib=400):
        with ThreadPoolExecutor(max_workers=1) as executor:

            def work():
                with pytest.raises(TimeoutError):
                    with engine_window(engine, capacity, memory_mib=400, timeout=0.1):
                        pytest.fail("oversubscribed memory")

            executor.submit(work).result(timeout=2)


def test_remote_window_drains_other_workers_and_blocks_new_work(fleet):
    repo, task, (first, second), _ = fleet
    active = claim(repo, enqueue(repo, task, first), first)
    token = repo.request_measurement_window(task, "unit-engine")
    pending = enqueue(repo, task, second)
    assert not repo.enter_measurement_window(token)
    assert repo.schedule_fleet() == []
    repo.commit_fleet_result(first, active.lease, result())
    assert repo.enter_measurement_window(token)
    assert repo.schedule_fleet() == []
    worker = next(w for w in repo.fleet_profiles() if w["worker_id"] == second.worker_id)
    measured = repo.enqueue_fleet_job(
        task,
        worker,
        {
            "operation": "compile",
            "timeout_seconds": 60,
            "build_profile": "normal",
            "output_bytes": 65536,
            "window_token": token,
        },
        [],
    )
    assignment = claim(repo, measured, second)
    repo.commit_fleet_result(second, assignment.lease, result())
    repo.release_measurement_window(token)
    assert any(j["job_id"] == pending for j in repo.schedule_fleet())


def test_remote_window_stays_closed_until_cleanup_confirmed(fleet):
    repo, task, (first, second), _ = fleet
    token = repo.request_measurement_window(task, "unit-engine")
    assert repo.enter_measurement_window(token)
    worker = next(w for w in repo.fleet_profiles() if w["worker_id"] == first.worker_id)
    job = repo.enqueue_fleet_job(
        task,
        worker,
        {
            "operation": "compile",
            "timeout_seconds": 60,
            "window_token": token,
        },
        [],
    )
    assignment = claim(repo, job, first)
    repo.cancel_fleet_job(job)
    repo.release_measurement_window(token)
    queued = enqueue(repo, task, second)
    assert repo.schedule_fleet() == []
    repo.confirm_fleet_cleanup(second, assignment.lease)
    assert any(j["job_id"] == queued for j in repo.schedule_fleet())


def test_noisy_completed_measurement_cannot_be_repeated(tmp_path):
    from test_execution_entrypoints import pending

    from compiler_optimizer.shared.benchmarking import BenchmarkPlan
    from compiler_optimizer.workers.execution import execute_benchmark, execute_verification
    from compiler_optimizer.workers.verification_plan import VerificationPlan

    values = setup(tmp_path)
    repo, runner, _, task, _, recovery, _, _ = values
    proposal = pending(values)
    with repo._transaction() as db:
        db.execute("DELETE FROM pipeline_runs WHERE task_id=?", (task,))
        db.execute("DELETE FROM selection_sessions WHERE task_id=?", (task,))
    verified = execute_verification(
        repo,
        task,
        proposal.proposal_id,
        runner,
        VerificationPlan(),
        BenchmarkPlan(),
        recovery=recovery,
    )
    count = [0]
    original = runner.execute

    def noisy(*args, **kwargs):
        invocation = original(*args, **kwargs)
        count[0] += 1
        invocation.wall_seconds = 0.001  # Below the frozen minimum measurement duration.
        return invocation

    runner.execute = noisy
    report = execute_benchmark(
        repo, task, verified.job_id, runner, BenchmarkPlan(), recovery=recovery
    )
    assert not report["analysis"]["qualified"]
    before = count[0]
    assert (
        execute_benchmark(repo, task, verified.job_id, runner, BenchmarkPlan(), recovery=recovery)
        == report
    )
    assert count[0] == before
    with pytest.raises((ContractError, StateError)):
        run_benchmark(repo, task, verified.job_id, runner, BenchmarkPlan())
    assert count[0] == before


def test_remote_engine_capacity_is_shared_across_worker_identities(fleet):
    from test_fleet import profile

    from compiler_optimizer.shared.fleet import canonical

    repo, task, identities, _ = fleet
    for identity in identities:
        identity.incarnation_id = str(uuid4())
        value = profile()
        value["hardware"] = telemetry(cpus=4, memory_mib=1024)
        repo.register_fleet_worker(identity, canonical(value).decode())
    jobs = []
    for identity in identities:
        worker = next(w for w in repo.fleet_profiles() if w["worker_id"] == identity.worker_id)
        jobs.append(
            repo.enqueue_fleet_job(
                task,
                worker,
                {
                    "operation": "compile",
                    "timeout_seconds": 60,
                    "reservation_mib": 400,
                },
                [],
            )
        )
    assert len(repo.schedule_fleet()) == 1  # 512 MiB remains after reserved headroom.
    assert sorted(repo.fleet_job(j)["state"] for j in jobs) == ["assigned", "pending"]


def test_interrupted_measurement_claim_cannot_be_reopened(tmp_path):
    from test_execution_entrypoints import pending

    from compiler_optimizer.shared.benchmarking import BenchmarkPlan
    from compiler_optimizer.workers.verify import run_verification

    values = setup(tmp_path)
    repo, runner, _, task, _, _, _, _ = values
    proposal = pending(values)
    with repo._transaction() as db:
        db.execute("DELETE FROM pipeline_runs WHERE task_id=?", (task,))
        db.execute("DELETE FROM selection_sessions WHERE task_id=?", (task,))
    verified = run_verification(repo, task, proposal.proposal_id, runner)
    ref = repo.artifacts.put(BenchmarkPlan().encode())
    repo.begin_measurement(task, verified.job_id, 1, ref)
    before = list(runner.events)
    with pytest.raises(StateError, match="interrupted"):
        run_benchmark(Repository(repo.root), task, verified.job_id, runner, BenchmarkPlan())
    assert runner.events == before


def test_measurement_gate_excludes_a_separate_python_process():
    import subprocess
    import sys

    engine = str(uuid4())
    script = """
import sys
from compiler_optimizer.scheduler.resources import Capacity, engine_window
try:
    with engine_window(sys.argv[1], Capacity(2, 1024), timeout=0.1):
        raise AssertionError("measurement admitted competing process")
except TimeoutError:
    print("blocked")
"""
    with engine_window(engine, Capacity(2, 1024), exclusive=True):
        child = subprocess.run(
            [sys.executable, "-c", script, engine], capture_output=True, text=True, timeout=5
        )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "blocked"


def test_prepared_cohort_resumes_with_a_smaller_active_pool(tmp_path):
    repo, first, second, task, broker, _, coordinator, _ = concurrent_setup(tmp_path)
    exchange = broker.exchange

    def stopped(*args, **kwargs):
        raise OSError("owner stopped before measurement")

    broker.exchange = stopped
    with pytest.raises(OSError, match="owner stopped"):
        coordinator.run(task, agents=["performance", "architecture"])
    assert all(m["state"] == "ready" for m in repo.cohort_work(task)["members"])
    assert not repo.list_benchmarks(task)
    jobs = len(repo.list_jobs(task))
    coordinator.concurrency = 1
    broker.exchange = exchange
    assert coordinator.run(task)["status"] == "selected"
    assert len(repo.list_jobs(task)) == jobs
    assert repo.cohort_work(task)["state"] == "completed"


def test_sequential_cancellation_during_verification_does_not_start_measurement(tmp_path):
    repo, runner, _, task, _, _, coordinator, _ = setup(tmp_path)
    original = runner.compile

    def compile(*args, **kwargs):
        repo.cancel_cohort(task)
        return original(*args, **kwargs)

    runner.compile = compile
    report = coordinator.run(task, agents=["performance"])
    assert report["status"] == "cancelled"
    assert not repo.list_benchmarks(task)
    assert repo.get_decision(task) is None
