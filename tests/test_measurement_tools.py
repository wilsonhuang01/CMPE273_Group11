"""Resource identity, fair comparisons, and supplementary tool contracts."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from test_benchmark import raw_data

from compiler_optimizer.scheduler.resources import Capacity, engine_window
from compiler_optimizer.shared.benchmarking import BenchmarkPlan, analyze
from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.workers.measurement_tools import KernelBenchmark, parse_samples
from compiler_optimizer.workers.perf import EVENTS, validate_counters
from compiler_optimizer.workers.policy import POLICY_DIGEST, execution_policy
from compiler_optimizer.workers.targets import compiler_flags


def test_parallel_profiles_keep_default_policy_and_flags():
    assert execution_policy()[1] == POLICY_DIGEST
    for parallel, perf in ((True, False), (False, True), (True, True)):
        policy, digest = execution_policy(parallel=parallel, perf=perf)
        assert digest != POLICY_DIGEST
        allowed = [
            n
            for rule in json.loads(policy)["syscalls"]
            if rule["action"] == "SCMP_ACT_ALLOW"
            for n in rule["names"]
        ]
        assert ("perf_event_open" in allowed) == perf
        assert ("sched_setaffinity" in allowed) == parallel
    normal = compiler_flags("linux-aarch64", "cpp17")
    assert compiler_flags("linux-aarch64", "cpp17", "parallel-2") == [
        *normal,
        "-fopenmp",
        "-DCO_THREADS=2",
    ]
    with pytest.raises(ValueError):
        compiler_flags("linux-riscv64", "cpp17", "parallel-2")


def test_parallel_cpu_reservation_blocks_oversubscription_and_nested_upgrade():
    engine = str(uuid4())
    capacity = Capacity(2, 1024)

    def reserve():
        with engine_window(engine, capacity, timeout=0.1):
            pytest.fail("oversubscribed CPU reservation")

    with engine_window(engine, capacity, cpus=2):
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(TimeoutError):
                pool.submit(reserve).result()
    with engine_window(engine, capacity):
        with pytest.raises(ContractError, match="CPU reservation"):
            with engine_window(engine, capacity, cpus=2):
                pass


def parallel_raw():
    plan = BenchmarkPlan()
    raw = raw_data(plan)
    config = dict(execution_profile="parallel-2", threads=2, cpu_quota=2, cpu_ids=[0, 1])
    for env in ("environment_before", "environment_after"):
        raw[env].update(config)
    for pairs in raw["cases"]["work"].values():
        for pair in pairs:
            for sample in pair["samples"].values():
                sample.update(config, cpu_seconds=1.5)
    return plan, raw


def test_parallel_comparison_qualifies_only_same_resource_samples():
    plan, raw = parallel_raw()
    assert analyze(raw, plan, ["work"])["qualified"]
    for key, value in (("threads", 1), ("cpu_quota", 1), ("cpu_ids", [0]), ("cpu_seconds", None)):
        bad = copy.deepcopy(raw)
        bad["cases"]["work"]["pairs"][0]["samples"]["candidate"][key] = value
        with pytest.raises(ContractError, match="parallel sample"):
            analyze(bad, plan, ["work"])
    raw["environment_after"]["cpu_ids"] = [0, 2]
    raw["environment_after"]["allowed_cpus"].append(2)
    assert "environment_changed" in analyze(raw, plan, ["work"])["reasons"]


@pytest.mark.parametrize("available", [False, True])
def test_perf_available_and_unavailable(available):
    events = {name: {"status": "unavailable", "count": None, "errno": 1} for name in EVENTS}
    if available:
        events["cycles"] = dict(
            status="available", count=42, time_enabled_ns=100, time_running_ns=90
        )
    assert validate_counters(events) == events
    events["energy-pkg"]["count"] = 0
    with pytest.raises(ContractError, match="cannot report zero"):
        validate_counters(events)


def test_perf_rejects_unscheduled_counter():
    events = {name: {"status": "unavailable", "count": None, "errno": 1} for name in EVENTS}
    events["cycles"] = dict(status="available", count=0, time_enabled_ns=100, time_running_ns=0)
    with pytest.raises(ContractError, match="not scheduled"):
        validate_counters(events)


@pytest.mark.parametrize("kernel", ["sum-i64-v1", "dot-i64-v1"])
def test_google_benchmark_declared_workload_and_samples(kernel):
    policy = KernelBenchmark(kernel=kernel, repetitions=3)
    assert policy.driver() == policy.driver()
    rows = [
        dict(run_type="iteration", iterations=64, time_unit="ns", real_time=100, cpu_time=95)
        for _ in range(3)
    ]
    _, samples = parse_samples(json.dumps({"benchmarks": rows}), policy)
    assert len(samples) == 3
    rows[0]["iterations"] = 65
    with pytest.raises(ContractError, match="differs"):
        parse_samples(json.dumps({"benchmarks": rows}), policy)


@pytest.mark.parametrize(
    "field,value",
    [
        ("size", 65537),
        ("seed", True),
        ("iterations", 1025),
        ("repetitions", 12),
        ("warmups", 33),
        ("kernel", "main"),
    ],
)
def test_kernel_policy_rejects_unbounded_work(field, value):
    with pytest.raises(ContractError):
        KernelBenchmark(**{field: value}).validate()


def test_parallel_policy_rejects_weakened_effective_settings():
    from test_sandbox import config

    from compiler_optimizer.workers.policy import validate_host_config

    current = config()
    current.update(
        NanoCpus=2_000_000_000,
        PidsLimit=4,
        CpusetCpus="0,1",
        SecurityOpt=["no-new-privileges", "seccomp=" + execution_policy(parallel=True)[0]],
    )
    validate_host_config(
        current,
        memory=256,
        compile_mode=False,
        mounts={"/tmp": "bounded"},
        parallel=True,
        cpu_id=0,
        cpu_ids=[0, 1],
    )
    for field, bad in (("NanoCpus", 3_000_000_000), ("PidsLimit", 8), ("CpusetCpus", "0")):
        modified = {**current, field: bad}
        with pytest.raises(ValueError):
            validate_host_config(
                modified,
                memory=256,
                compile_mode=False,
                mounts={"/tmp": "bounded"},
                parallel=True,
                cpu_id=0,
                cpu_ids=[0, 1],
            )


def test_execution_profile_is_bound_to_compiler_contract():
    from test_baseline import FakeRunner

    from compiler_optimizer.shared.contracts import _compiler

    compiler = FakeRunner().compiler("c17")
    compiler.execution_profile = "parallel-2"
    with pytest.raises(ContractError, match="compiler flags"):
        _compiler(compiler)
    compiler.flags.extend(["-fopenmp", "-DCO_THREADS=2"])
    _compiler(compiler)
    compiler.execution_profile = ""
    with pytest.raises(ContractError, match="compiler flags"):
        _compiler(compiler)


def test_energy_available_unavailable_and_counter_wrap(tmp_path):
    from compiler_optimizer.workers.energy import measurement, snapshot

    assert snapshot(tmp_path)["status"] == "unavailable"
    domain = tmp_path / "intel-rapl:0"
    domain.mkdir()
    (domain / "name").write_text("package-0")
    (domain / "max_energy_range_uj").write_text("1000")
    (domain / "energy_uj").write_text("900")
    before = snapshot(tmp_path)
    (domain / "energy_uj").write_text("100")
    after = snapshot(tmp_path)
    value = measurement(before, after)
    assert value["status"] == "available"
    assert value["count"] == 200
    assert value["joules"] == 0.0002
    assert value["scope"] == "package-wide"
    events = {name: {"status": "unavailable", "count": None, "errno": 1} for name in EVENTS}
    events["energy-pkg"] = value
    validate_counters(events)
    (domain / "energy_uj").unlink()
    assert measurement(before, snapshot(tmp_path))["status"] == "unavailable"


def test_short_kernel_cpu_time_can_be_below_timer_resolution():
    policy = KernelBenchmark(repetitions=3)
    row = dict(run_type="iteration", iterations=64, time_unit="ns", real_time=50, cpu_time=0)
    _, samples = parse_samples(json.dumps({"benchmarks": [row] * 3}), policy)
    assert all(r["cpu_time"] == 0 for r in samples)
    for key, value in (("real_time", 0), ("cpu_time", -1)):
        with pytest.raises(ContractError, match="invalid Google Benchmark sample"):
            parse_samples(json.dumps({"benchmarks": [{**row, key: value}] * 3}), policy)


def test_nonfinite_derived_google_statistics_are_explicit_nulls():
    policy = KernelBenchmark(repetitions=3)
    rows = [dict(run_type="iteration", iterations=64, time_unit="ns", real_time=50, cpu_time=0)] * 3
    rows.append(dict(run_type="aggregate", aggregate_name="cv", cpu_time=float("nan")))
    result, samples = parse_samples(json.dumps({"benchmarks": rows}), policy)
    assert len(samples) == 3
    assert result["benchmarks"][-1]["cpu_time"] is None
    assert result["nonfinite_fields"] == [".benchmarks[3].cpu_time"]
    json.dumps(result, allow_nan=False)
