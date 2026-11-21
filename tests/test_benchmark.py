"""Paired statistics, immutable benchmark evidence, migration, and final selection gates."""

import copy
import json
import sqlite3
from dataclasses import asdict, replace
from importlib.resources import files

import pytest
from test_verification import scenario as scenario

from compiler_optimizer.shared.benchmarking import BenchmarkPlan, analyze, pair_order
from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import Repository
from compiler_optimizer.storage.errors import StateError
from compiler_optimizer.workers.benchmark import run_benchmark
from compiler_optimizer.workers.docker_runner import InfrastructureError
from compiler_optimizer.workers.verify import run_verification


def environment():
    return dict(
        engine_id="engine",
        boot_id="boot",
        engine_architecture="x86_64",
        cpu_id=0,
        allowed_cpus=[0, 1],
        cpu_model="unit",
        cpu_features_sha256="a" * 64,
        kernel="linux",
        governor="performance",
        clock_resolution_seconds=1e-9,
        native_amd64=True,
    )


def raw_data(plan):
    raw = dict(
        schema_version=1,
        status="completed",
        environment_before=environment(),
        environment_after=environment(),
        cases={},
    )
    sample = dict(
        wall_seconds=1.0,
        peak_memory_bytes=4096,
        returncode=0,
        failure=None,
        cpu_id=0,
        build_profile="normal",
        watchdog_removed=True,
        output_valid=True,
    )
    raw["cases"]["work"] = {}
    for phase, count in (("warmups", plan.warmups), ("pairs", plan.repetitions)):
        offset = 0 if phase == "warmups" else plan.warmups
        raw["cases"]["work"][phase] = [
            {
                "order": pair_order(plan, "work", offset + i),
                "samples": {"baseline": dict(sample), "candidate": {**sample, "wall_seconds": 0.8}},
            }
            for i in range(count)
        ]
    return raw


def test_clear_gain_and_exact_bound():
    plan = BenchmarkPlan()
    report = analyze(raw_data(plan), plan, ["work"])
    assert report["qualified"]
    stats = report["cases"]["work"]
    assert stats["one_sided_sign_p"] == 1 / 2**15
    assert stats["median_gain_lower_bound"] == pytest.approx(0.2)
    assert BenchmarkPlan.parse(plan.encode()) == plan


def test_multiple_workloads_require_family_wise_significance():
    plan = BenchmarkPlan(warmups=0, repetitions=7)
    raw = raw_data(plan)
    assert analyze(raw, plan, ["work"])["qualified"]
    ids = ["work", *[f"work{i}" for i in range(7)]]
    for test_id in ids[1:]:
        raw["cases"][test_id] = copy.deepcopy(raw["cases"]["work"])
        for i, pair in enumerate(raw["cases"][test_id]["pairs"]):
            pair["order"] = pair_order(plan, test_id, i)
    report = analyze(raw, plan, ids)
    assert not report["qualified"]
    assert report["cases"]["work"]["per_case_alpha"] == 0.05 / 8
    assert report["cases"]["work"]["median_gain_lower_bound"] is None


def test_explicit_cpu_policy_is_enforced():
    plan = BenchmarkPlan(cpu_id=1)
    with pytest.raises(ContractError, match="requested CPU"):
        analyze(raw_data(plan), plan, ["work"])


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("emulation", "emulated_worker"),
        ("environment", "environment_changed"),
        ("short", "samples_too_short"),
        ("noise", "unstable_runtime"),
        ("memory", "memory_regression"),
        ("tie", "no_reliable_improvement"),
    ],
)
def test_conservative_retention(kind, reason):
    plan = BenchmarkPlan()
    raw = raw_data(plan)
    if kind == "emulation":
        for key in ("environment_before", "environment_after"):
            raw[key].update(native_amd64=False, engine_architecture="aarch64")
    elif kind == "environment":
        raw["environment_after"]["boot_id"] = "different"
    else:
        for i, pair in enumerate(raw["cases"]["work"]["pairs"]):
            b, c = pair["samples"]["baseline"], pair["samples"]["candidate"]
            if kind == "short":
                b["wall_seconds"], c["wall_seconds"] = 0.001, 0.0008
            elif kind == "noise":
                b["wall_seconds"] = [1.0, 2.0, 4.0][i % 3]
                c["wall_seconds"] = 0.8 * b["wall_seconds"]
            elif kind == "memory":
                c["peak_memory_bytes"] *= 2
            else:
                c["wall_seconds"] = b["wall_seconds"]
    result = analyze(raw, plan, ["work"])
    assert not result["qualified"]
    assert reason in result["reasons"] + result["cases"]["work"]["reasons"]


def test_warmups_excluded_and_no_outliers_discarded():
    plan = BenchmarkPlan()
    raw = raw_data(plan)
    for pair in raw["cases"]["work"]["warmups"]:
        pair["samples"]["candidate"]["wall_seconds"] = 100
    assert analyze(raw, plan, ["work"])["qualified"]
    for pair in raw["cases"]["work"]["pairs"][:5]:
        pair["samples"]["candidate"]["wall_seconds"] = 100
    assert not analyze(raw, plan, ["work"])["qualified"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("repetitions", 6),
        ("warmups", True),
        ("alpha", float("nan")),
        ("cpu_id", -1),
        ("minimum_relative_improvement", 0),
    ],
)
def test_invalid_policy(field, value):
    data = asdict(BenchmarkPlan())
    data[field] = value
    with pytest.raises(ContractError):
        BenchmarkPlan.parse(data)


@pytest.mark.parametrize("mutation", ["count", "pin", "cleanup", "order", "unsuccessful"])
def test_incomplete_or_uncontrolled_measurements_rejected(mutation):
    plan = BenchmarkPlan()
    raw = raw_data(plan)
    pair = raw["cases"]["work"]["pairs"][0]
    if mutation == "count":
        raw["cases"]["work"]["pairs"].pop()
    elif mutation == "order":
        pair["order"].reverse()
    else:
        key, value = {
            "pin": ("cpu_id", 1),
            "cleanup": ("watchdog_removed", False),
            "unsuccessful": ("returncode", 1),
        }[mutation]
        pair["samples"]["candidate"][key] = value
    with pytest.raises(ContractError):
        analyze(raw, plan, ["work"])


@pytest.fixture
def verified(scenario):
    repo, runner, task, proposal, _ = scenario
    result = run_verification(repo, task, proposal, runner)
    execute = runner.execute
    runner.benchmark_environment = lambda cpu: environment()
    runner.benchmark_events = []
    runner.benchmark_mode = "good"

    def measured(binary, case, package, *, build_profile="normal", cpu_id=None):
        role = runner.binaries[binary]
        runner.benchmark_events.append((role, cpu_id))
        if runner.benchmark_mode == "infrastructure":
            raise InfrastructureError("daemon down")
        invocation = execute(binary, case, package, build_profile=build_profile)
        invocation.wall_seconds = 1.0 if role == "baseline" else 0.8
        invocation.sandbox = {"cpu_id": cpu_id, "watchdog": {"state": "removed"}}
        if runner.benchmark_mode == role:
            invocation.stdout = b"wrong\n"
        return invocation

    runner.execute = measured
    return repo, runner, task, result


def test_verified_binaries_reused_then_selection_allowed(verified):
    repo, runner, task, result = verified
    old_events = list(runner.events)
    report = run_benchmark(repo, task, result.job_id, runner)
    assert report["analysis"]["qualified"]
    assert len(runner.benchmark_events) == 34
    assert all(cpu == 0 for _, cpu in runner.benchmark_events)
    assert not any(event[0] == "compile" for event in runner.events[len(old_events) :])
    assert len(repo.list_benchmarks(task)) == 1
    chosen = repo.select_result(task, result.job_id, 1, expected_revision=1, reason="Reliable gain")
    assert chosen["job_id"] == result.job_id


def test_baseline_fallback_and_report_redelivery(verified):
    repo, runner, task, result = verified
    report = run_benchmark(repo, task, result.job_id, runner)
    ref = repo.artifacts.put(json.dumps(report, sort_keys=True, allow_nan=False).encode())
    assert repo.record_benchmark(ref) is False
    baseline = next(
        row for row in repo.list_results(task) if row.WhichOneof("subject") == "baseline"
    )
    repo.select_result(task, baseline.job_id, 1, expected_revision=1, reason="Keep original")
    assert repo.record_benchmark(ref) is False


def test_unverified_candidate_never_reaches_measurement(scenario):
    repo, runner, task, proposal, _ = scenario
    runner.mode = "candidate-ub"
    result = run_verification(repo, task, proposal, runner)
    before = list(runner.events)
    with pytest.raises(ContractError, match="passed correctness"):
        run_benchmark(repo, task, result.job_id, runner)
    assert before == runner.events
    assert repo.list_benchmarks(task) == []


@pytest.mark.parametrize("role", ["baseline", "candidate", "infrastructure"])
def test_failure_persisted_and_invalidity_enforced(verified, role):
    repo, runner, task, result = verified
    runner.benchmark_mode = role
    report = run_benchmark(repo, task, result.job_id, runner)
    assert not report["analysis"]["qualified"]
    assert len(repo.list_benchmarks(task)) == 1
    if role == "baseline":
        assert repo.get_task(task)["status"] == "failed"
    elif role == "candidate":
        assert repo.list_benchmarks(task)[0]["candidate_invalid"]
        with pytest.raises(StateError, match="invalidated"):
            run_benchmark(repo, task, result.job_id, runner)
    else:
        runner.benchmark_mode = "good"
        assert run_benchmark(repo, task, result.job_id, runner)["analysis"]["qualified"]


def test_latest_retained_benchmark_blocks_earlier_gain(verified):
    repo, runner, task, result = verified
    assert run_benchmark(repo, task, result.job_id, runner)["analysis"]["qualified"]
    report = run_benchmark(
        repo,
        task,
        result.job_id,
        runner,
        replace(BenchmarkPlan(), minimum_relative_improvement=0.3),
    )
    assert not report["analysis"]["qualified"]
    with pytest.raises(StateError, match="reliable benchmark"):
        repo.select_result(task, result.job_id, 1, expected_revision=1, reason="Old gain")


def test_forged_summary_and_binary_binding_rejected(verified):
    repo, runner, task, result = verified
    report = run_benchmark(repo, task, result.job_id, runner)
    for field, value in (("analysis", {"qualified": True}), ("binaries", {})):
        forged = copy.deepcopy(report)
        forged["run_id"] += "x"
        forged[field] = value
        with pytest.raises(ContractError):
            repo.record_benchmark(repo.artifacts.put(json.dumps(forged).encode()))
    assert len(repo.list_benchmarks(task)) == 1


def test_raw_artifact_corruption_blocks_selection(verified):
    repo, runner, task, result = verified
    report = run_benchmark(repo, task, result.job_id, runner)
    ref = pb.ArtifactRef(**report["raw"])
    # Corruption is detected again at selection, even after initially valid ingestion.
    (repo.artifacts.root / ref.key).write_bytes(b"corrupted")
    from compiler_optimizer.storage.errors import ArtifactError

    with pytest.raises(ArtifactError):
        repo.select_result(task, result.job_id, 1, expected_revision=1, reason="Gain")


def test_schema_one_migrates_without_losing_data(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    with sqlite3.connect(root / "state.sqlite3") as db:
        db.executescript(files("compiler_optimizer.storage").joinpath("schema.sql").read_text())
        db.execute("PRAGMA user_version=1")
        db.execute("INSERT INTO tasks VALUES ('old', X'00', 'running', 7)")
    Repository(root)
    Repository(root)
    with sqlite3.connect(root / "state.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 8
        assert db.execute("SELECT revision FROM tasks WHERE task_id='old'").fetchone()[0] == 7
        assert db.execute("SELECT COUNT(*) FROM benchmark_runs").fetchone()[0] == 0
