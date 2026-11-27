"""Predeclaration, corpus/oracle isolation, candidate semantics and trial uncertainty."""

import json
import os
from types import SimpleNamespace

import pytest

from compiler_optimizer.evaluation.corpus import CATALOGS, FAMILIES, corpus, variant
from compiler_optimizer.evaluation.expanded import digest, protocol, run_expanded
from compiler_optimizer.evaluation.uncertainty import interval
from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.workers.package import package_from_files


def test_protocol_is_deterministic_balanced_and_holds_out_independent_inputs():
    plan = protocol(target="linux-aarch64")
    assert plan == protocol(target="linux-aarch64")
    assert len(plan["schedule"]) == 9
    assert {entry["trial"] for entry in plan["schedule"]} == {1, 2, 3}
    assert all(
        set(entry["order"]) == {"original", "single", "multi", "debate"}
        for entry in plan["schedule"]
    )
    for entry in plan["schedule"]:
        package, hidden = corpus(entry["family"], entry["count"], entry["seed"])
        assert digest(hidden) == entry["heldout_sha256"]
        assert not {case["stdin"].encode() for case in hidden}.intersection(
            package.files[test.stdin] for test in package.manifest.tests
        )
    assert len(CATALOGS["single"]) == len(CATALOGS["multi"]) == 3
    assert len(CATALOGS["debate"]) * 3 == 3
    assert plan["budgets_per_optimizer_arm"]["external_model_calls"] == 0


@pytest.mark.parametrize(
    "kwargs", [{"trials": 1}, {"trials": True}, {"count": 0}, {"target": "linux-riscv64"}]
)
def test_invalid_designs_rejected_before_measurement(tmp_path, kwargs):
    with pytest.raises(ContractError):
        run_expanded(tmp_path / "run", **{"target": "linux-aarch64", **kwargs})
    assert not (tmp_path / "run").exists()


def test_protocol_and_partial_report_survive_failed_first_probe(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("Docker unavailable")

    monkeypatch.setattr("compiler_optimizer.evaluation.expanded.MeteredRunner", unavailable)
    root = tmp_path / "run"
    with pytest.raises(RuntimeError):
        run_expanded(root, target="linux-aarch64", count=10)
    report = json.loads((root / "report.json").read_text())
    assert report["status"] == "failed"
    assert report["protocol_sha256"] == digest(json.loads((root / "protocol.json").read_text()))
    with pytest.raises(ContractError, match="overwrite"):
        run_expanded(root, target="linux-aarch64")


def test_trial_bootstrap_does_not_mistake_repeated_samples_for_independent_trials():
    result = interval({1: [1.0] * 1000, 2: [2.0], 3: [3.0]})
    assert result["mean"] == 2.0 and result["trials"] == 3
    assert result["lower"] == 1.0 and result["upper"] == 3.0
    assert interval({1: [1.0] * 1000})["lower"] is None
    assert result == interval({1: [1.0], 2: [2.0], 3: [3.0]})


@pytest.mark.docker
@pytest.mark.skipif(
    os.environ.get("EVALUATION_DOCKER_TESTS") != "1", reason="native Docker required"
)
@pytest.mark.parametrize("family", FAMILIES)
def test_frozen_candidates_match_python_heldout_oracle_on_native_docker(tmp_path, family):
    from compiler_optimizer.evaluation.__main__ import native_target
    from compiler_optimizer.evaluation.expanded import final_check
    from compiler_optimizer.workers.docker_runner import DockerRunner
    from compiler_optimizer.workers.targets import DEFAULT_IMAGES

    package, hidden = corpus(family, 100, 273)
    target = native_target()
    runner = DockerRunner(
        DEFAULT_IMAGES[target], target=target, watchdog_directory=tmp_path / "watchdogs"
    )
    assert runner.native_execution
    for name in ("buffer-4096", "buffer-65536", "buffer-262144", "storage", "loop"):
        candidate = package_from_files(
            {**package.files, package.manifest.sources[0]: variant(family, name).encode()}
        )
        assert final_check(runner, candidate, hidden)["passed"], name


def test_holdout_mismatch_veto_is_reported_with_raw_evidence():
    from compiler_optimizer.evaluation.expanded import final_check

    class Runner:
        def compile(self, package):
            return SimpleNamespace(failure=None, binary=b"binary")

        def execute(self, binary, test, package):
            return SimpleNamespace(failure=None, stdout=b"wrong", log=lambda: {"wall_seconds": 0.1})

    package, hidden = corpus("reduction", 10, 1)
    report = final_check(Runner(), package, hidden)
    assert not report["passed"] and len(report["cases"]) == 2
    assert all(not case["passed"] for case in report["cases"])


def test_saved_summary_preserves_trial_pairing_and_all_reported_costs(tmp_path):
    from compiler_optimizer.evaluation.report import render_report
    from compiler_optimizer.evaluation.uncertainty import summarize

    report = {
        "schema_version": 2,
        "run_id": "test-fixture",
        "target": "linux-aarch64",
        "status": "complete",
        "limitations": ["Synthetic unit-test fixture."],
        "workloads": [],
    }
    for trial in (1, 2, 3):
        entry = {
            "trial": trial,
            "family": "reduction",
            "name": "fixture",
            "arms": {},
            "original": {
                "runtime_seconds": 0.1,
                "peak_rss_bytes": 2000000,
                "speedup_ratio": 1.0,
                "memory_change_fraction": 0.0,
            },
        }
        for name in ("single", "multi", "debate"):
            entry["arms"][name] = {
                "status": "retained_baseline",
                "selected_measurements": None,
                "valid_proposals": 2,
                "generated_proposals": 3,
                "retry_attempts": 1,
                "recovery_seconds_lower_bound": 0.5,
                "correctness_failures": 1,
                "cost": {
                    "invocations": 100,
                    "optimization_elapsed_seconds": 20,
                    "external_model_calls": 0,
                },
            }
        report["workloads"].append(entry)
    report["summary"] = summarize(report)
    render_report(report, tmp_path)
    group = next(
        g
        for g in report["summary"]["groups"]
        if g["family"] == "all_families" and g["strategy"] == "single"
    )
    assert group["metrics"]["fallback_fraction"] == {"mean": 1, "lower": 1, "upper": 1, "trials": 3}
    assert group["metrics"]["valid_proposals"]["mean"] == 2
    assert all(c["mean"] == 0 for c in report["summary"]["paired_differences"])
    assert "recovery_seconds_lower_bound" in (tmp_path / "uncertainty.csv").read_text()
    assert "model_tokens" in (tmp_path / "summary.csv").read_text()
    assert "Trial uncertainty" in (tmp_path / "report.html").read_text()
