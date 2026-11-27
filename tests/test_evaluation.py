"""Evaluation fairness, hard-budget, provenance and credential-free release contracts."""

import importlib.util
import json
import tarfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_benchmark import environment
from test_benchmark import verified as verified
from test_verification import EXAMPLES, Runner
from test_verification import scenario as scenario

from compiler_optimizer.evaluation.experiment import (
    benchmark,
    clone_baseline,
    require_comparable,
    run_evaluation,
    snapshot_source,
    source_digest,
)
from compiler_optimizer.evaluation.instrumentation import Budget, BudgetExceeded, ScopedRedis
from compiler_optimizer.evaluation.workloads import CATALOG, Recipe, workload
from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.baseline import run_baseline
from compiler_optimizer.workers.package import read_package


def test_workloads_and_catalog_are_frozen_equal_slots():
    first, second = workload(100, seed=160273), workload(100, seed=160273)
    assert first.package_bundle == second.package_bundle
    assert first.files["tests/workload.in"] != workload(100).files["tests/workload.in"]
    assert all(len(items) == 3 for items in CATALOG.values())
    assert benchmark(15).alpha == 0.05 / 3
    assert benchmark(15).repetitions == 15
    request = SimpleNamespace(
        manifest=SimpleNamespace(language="c17"),
        source=SimpleNamespace(bundle=SimpleNamespace(sha256="a" * 64)),
    )
    sources = {"main.c": first.files["main.c"]}
    for assignments in CATALOG.values():
        for _, recipe in assignments:
            patch = json.loads(Recipe(recipe).generate(request, sources))
            assert patch["parent_source_sha256"] == "a" * 64
            assert patch["edits"] and patch["rationale"]


def test_atomic_shared_budget_accounts_for_failed_attempts(tmp_path):
    budget = Budget(tmp_path / "ledger.json", maximum_invocations=4, maximum_reserved_seconds=8)

    def reserve(_):
        try:
            return budget.reserve({"wall_seconds": 2}, False, False)
        except BudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(reserve, range(20)))
    assert len([r for r in rows if r is not None]) == 4
    assert budget.summary()["reserved_seconds"] == 8
    assert len(json.loads(budget.path.read_text())) == 4
    assert budget.summary()["calls_without_reported_wall_time"] == 4
    assert budget.summary()["monetary_cost_usd"] is None


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf"), True])
def test_invalid_reservations_do_not_consume_budget(tmp_path, seconds):
    budget = Budget(tmp_path / "ledger.json")
    with pytest.raises(ContractError):
        budget.reserve({"wall_seconds": seconds}, False, False)
    assert not budget.calls


@pytest.mark.sqlite_only
def test_checkpoint_isolated_state_and_immutable_artifacts(scenario, tmp_path):
    original = read_package(EXAMPLES / "sum-array")
    repository = Repository(tmp_path / "baseline-only")
    task = run_baseline(original, repository, Runner(original)).meta.task_id
    clone = clone_baseline(repository, tmp_path / "checkpoint")
    assert clone.list_tasks() == repository.list_tasks()
    assert clone.root != repository.root
    assert clone.list_proposals(task) == []
    with pytest.raises(ContractError):
        clone_baseline(repository, clone.root)


def test_environment_identity_excludes_load_but_requires_same_native_cpu():
    first = environment()
    second = dict(first, load_average=[100, 100, 100])
    require_comparable([{"environment": first}, {"environment": second}])
    second["boot_id"] = "different boot"
    with pytest.raises(ContractError, match="environments differ"):
        require_comparable([{"environment": first}, {"environment": second}])


def test_source_snapshot_and_no_resampling(tmp_path):
    assert snapshot_source(tmp_path) == source_digest()
    with zipfile.ZipFile(tmp_path / "implementation.zip") as archive:
        assert "evaluation/experiment.py" in archive.namelist()
        assert not any("__pycache__" in p for p in archive.namelist())
    with pytest.raises(ContractError, match="overwrite"):
        run_evaluation(tmp_path, target="linux-aarch64")


def test_blacklist_namespaces_preserve_record_keys_and_ttl():
    class Cache:
        def set(self, key, value, **kwargs):
            self.value = key, value, kwargs

        def get(self, key):
            return key

    cache = Cache()
    scoped = ScopedRedis(cache, "evaluation:test:")
    scoped.set("original-key", b"record", ex=300)
    assert cache.value == ("evaluation:test:original-key", b"record", {"ex": 300})
    assert scoped.get("original-key") == "evaluation:test:original-key"


def launcher():
    spec = importlib.util.spec_from_file_location(
        "release_launcher", Path(__file__).resolve().parents[1] / "tools/project.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_excludes_credentials_preflights_evidence_and_refuses_overwrite(
    tmp_path, monkeypatch
):
    module = launcher()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src/code.py").write_text("pass")
    (tmp_path / "src/.env").write_text("SECRET=value")
    (tmp_path / ".env").write_text("SECRET=value")
    (tmp_path / ".env.example").write_text("SECRET=")
    evidence = tmp_path / "var/run"
    evidence.mkdir(parents=True)
    (evidence / "report.json").write_text('{"status":"failed"}')
    destination = tmp_path / "release.tar.gz"
    with pytest.raises(ValueError, match="incomplete"):
        module.package(destination, [evidence])
    assert not destination.exists()
    (evidence / "report.json").write_text('{"status":"complete"}')
    module.package(destination, [evidence])
    with tarfile.open(destination) as archive:
        assert "project/src/code.py" in archive.getnames()
        assert "project/.env.example" in archive.getnames()
        assert not any(p.endswith("/.env") for p in archive.getnames())
        assert "evidence/run/report.json" in archive.getnames()
    assert destination.with_name("release.tar.gz.sha256").exists()
    with pytest.raises(ValueError, match="overwrite"):
        module.package(destination, [])


def test_env_is_literal_not_shell(tmp_path, monkeypatch):
    module = launcher()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.delenv("TEST_LITERAL", raising=False)
    (tmp_path / ".env").write_text(
        "TEST_LITERAL='$(touch should-never-exist)'\nRABBITMQ_AMQP_PORT=5679\n"
    )
    monkeypatch.delenv("RABBITMQ_PORT", raising=False)
    monkeypatch.delenv("RABBITMQ_AMQP_PORT", raising=False)
    assert module.environment()["TEST_LITERAL"] == "$(touch should-never-exist)"
    assert module.environment()["RABBITMQ_PORT"] == "5679"
    assert not (tmp_path / "should-never-exist").exists()


def test_arm_native_claim_is_required_and_bound_to_engine_architecture():
    from test_benchmark import raw_data

    from compiler_optimizer.shared.benchmarking import analyze

    plan = benchmark(15)
    raw = raw_data(plan)
    for key in ("environment_before", "environment_after"):
        raw[key].update(
            engine_architecture="aarch64",
            native_amd64=False,
            execution_target="linux-aarch64",
            native_execution=True,
        )
    assert analyze(raw, plan, ["work"])["qualified"]
    raw["environment_after"]["native_execution"] = False
    with pytest.raises(ContractError, match="native execution claim"):
        analyze(raw, plan, ["work"])
    raw["environment_after"]["native_execution"] = True
    raw["environment_after"]["engine_architecture"] = "x86_64"
    raw["environment_after"]["native_amd64"] = True
    with pytest.raises(ContractError, match="native execution claim"):
        analyze(raw, plan, ["work"])


def test_arm_compiler_profile_rejects_foreign_flags():
    from test_contracts import compiler

    from compiler_optimizer.shared.contracts import _compiler
    from compiler_optimizer.workers.targets import compiler_flags

    value = compiler()
    value.target = "linux-aarch64"
    with pytest.raises(ContractError):
        _compiler(value)
    value.flags[:] = compiler_flags(value.target, value.language)
    _compiler(value)


@pytest.mark.parametrize(
    "mode,failures", [("candidate", 1), ("baseline", 1), ("infrastructure", 0)]
)
def test_measurement_failures_remain_visible_in_evaluation(verified, mode, failures):
    from compiler_optimizer.evaluation.experiment import evidence
    from compiler_optimizer.workers.benchmark import run_benchmark

    repo, runner, task, result = verified
    runner.benchmark_mode = mode
    run_benchmark(repo, task, result.job_id, runner)
    recorded = evidence(repo, {"task_id": task})
    assert recorded["correctness_failures"] == failures
    assert len(recorded["benchmark_failures"]) == 1
    assert not recorded["observations"]
