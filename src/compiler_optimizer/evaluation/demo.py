"""Real accepted optimization and automatic recovery, with explicit failure assertions."""

import time
from pathlib import Path
from uuid import uuid4

from compiler_optimizer.coordinator.round import CohortCoordinator
from compiler_optimizer.evaluation.experiment import (
    benchmark,
    clone_baseline,
    evidence,
    make_controller,
    policy,
    snapshot_source,
    verification,
)
from compiler_optimizer.evaluation.instrumentation import Budget, MeteredRunner, write_json
from compiler_optimizer.evaluation.workloads import Recipe, workload
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.baseline import run_baseline
from compiler_optimizer.workers.targets import DEFAULT_IMAGES


def run_demo(output, *, target, repetitions=31, count=750000):
    output = Path(output).absolute()
    require(not output.exists(), "demo output already exists; refusing to resample results")
    require(target in ("linux-x86_64", "linux-aarch64"), "demo requires a native CPU target")
    benchmark(repetitions).encode()
    output.mkdir(parents=True)
    source_hash = snapshot_source(output)
    run_id = str(uuid4())
    setup = Budget(output / "baseline-cost.json")
    runner = MeteredRunner(
        DEFAULT_IMAGES[target],
        target=target,
        budget=setup,
        watchdog_directory=output / "baseline-watchdogs",
    )
    require(runner.native_execution, "the success demo requires a native CPU profile")
    require(runner.measurement_version == 2, "demo requires native RSS measurement v2")
    repo = Repository(output / "baseline")
    baseline = run_baseline(workload(count), repo, runner)
    require(baseline.WhichOneof("outcome") == "success", "demo reference failed")
    report = {
        "schema_version": 1,
        "kind": "optimization-and-recovery-demo",
        "run_id": run_id,
        "source_tree_sha256": source_hash,
        "target": target,
        "status": "running",
        "scenarios": [],
    }
    write_json(output / "demo.json", report)
    for name, recipe, inject in (
        ("successful-optimization", "stream-buffered", False),
        ("infrastructure-recovery", "stream-buffered", True),
        ("deterministic-crash-quarantine", "loop", False),
        ("correctness-rejection", "wrong-output", False),
    ):
        started = time.monotonic()
        root = output / name
        store = clone_baseline(repo, root / "state")
        budget = Budget(root / "cost-ledger.json")
        workers = [
            MeteredRunner(
                runner.image,
                target=target,
                budget=budget,
                inject_compiler_disconnect=inject and i == 0,
                watchdog_directory=root / "watchdogs",
            )
            for i in range(2)
        ]
        recovery, broker = make_controller(store, workers, f"evaluation:{run_id}:{name}:")
        role = "memory" if recipe.startswith("stream") else "performance"
        coordinator = CohortCoordinator(
            store, workers[0], broker, recovery=recovery, backends={role: Recipe(recipe)}
        )
        audit = coordinator.run(
            baseline.meta.task_id,
            agents=[role],
            policy=policy(),
            verification_plan=verification(),
            benchmark_plan=benchmark(repetitions),
        )
        item = {
            "name": name,
            "injected_infrastructure_failure": inject,
            "audit": audit,
            "evidence": evidence(store, audit),
            "cost": budget.summary(),
            "elapsed_seconds": time.monotonic() - started,
        }
        item["evidence"].pop("baseline_samples")
        attempts = item["evidence"]["attempts"]
        if name in ("successful-optimization", "infrastructure-recovery"):
            item["passed"] = audit["status"] == "selected"
            if inject:
                item["passed"] = (
                    item["passed"]
                    and len(attempts) == 2
                    and (
                        attempts[0]["worker_id"] != attempts[1]["worker_id"]
                        and attempts[0]["status"] == "infrastructure_failure"
                        and attempts[1]["status"] == "completed"
                    )
                )
        elif name == "deterministic-crash-quarantine":
            proposal = store.list_proposals(audit["task_id"])[0]
            item["recovery_receipt"] = store.recovery_outcome(
                audit["task_id"], proposal.proposal_id
            )
            item["passed"] = (
                audit["status"] == "retained_baseline"
                and item["recovery_receipt"]["reason"] == "confirmed_program_crash"
                and len(attempts) == 2
            )
        else:
            item["passed"] = (
                audit["status"] == "retained_baseline"
                and item["evidence"]["correctness_failures"] == 1
            )
        write_json(root / "scenario.json", item)
        report["scenarios"].append(item)
        write_json(output / "demo.json", report)
    report["status"] = "passed" if all(s["passed"] for s in report["scenarios"]) else "failed"
    write_json(output / "demo.json", report)
    return report
