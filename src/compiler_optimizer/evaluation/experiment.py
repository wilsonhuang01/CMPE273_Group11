"""Equal-budget single-generator and multi-generator experiments using real pipeline evidence."""

import hashlib
import json
import os
import random
import shutil
import sqlite3
import time
import zipfile
from dataclasses import asdict, replace
from pathlib import Path
from statistics import median
from uuid import uuid4

from compiler_optimizer.coordinator.round import CohortCoordinator
from compiler_optimizer.evaluation.instrumentation import (
    Budget,
    MeteredRunner,
    ScopedRedis,
    write_json,
)
from compiler_optimizer.evaluation.workloads import CATALOG, Recipe, workload
from compiler_optimizer.runtime.config import Settings
from compiler_optimizer.runtime.proposal_queue import RabbitProposalQueue
from compiler_optimizer.scheduler.blacklist import RedisBlacklist
from compiler_optimizer.scheduler.recovery import RecoveryController, Worker
from compiler_optimizer.shared.benchmarking import ENVIRONMENT_KEYS, BenchmarkPlan
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.recovery import RecoveryPolicy
from compiler_optimizer.shared.selection import SelectionPolicy, rank_key
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.baseline import run_baseline
from compiler_optimizer.workers.targets import DEFAULT_IMAGES
from compiler_optimizer.workers.verification_plan import ArrayGenerator, VerificationPlan


def clone_baseline(source, destination):
    """Fork only a quiescent baseline. SQLite backup includes WAL; CAS bytes stay immutable."""
    destination = Path(destination)
    require(not destination.exists(), "checkpoint destination already exists")
    with source.pipeline_lock():
        tasks = source.list_tasks()
        require(
            len(tasks) == 1 and not source.list_proposals(tasks[0]["task_id"]),
            "only a baseline-only repository can be cloned",
        )
        destination.mkdir(parents=True)
        shutil.copytree(source.root / "artifacts", destination / "artifacts", copy_function=os.link)
        with (
            sqlite3.connect(source.database) as original,
            sqlite3.connect(destination / "state.sqlite3") as copy,
        ):
            original.backup(copy)
    return Repository(destination)


def policy():
    return SelectionPolicy(performance_weight=0.5, memory_weight=0.5)


def benchmark(repetitions):
    return BenchmarkPlan(
        schema_version=2,
        warmups=1,
        repetitions=repetitions,
        seed=273,
        alpha=0.05 / 3,
        minimum_sample_seconds=0.01,
    )


def verification():
    return VerificationPlan(generator=ArrayGenerator(273, 1, 1, 0, 128, -1000, 1000))


def make_controller(repo, runners, namespace):
    settings = Settings.from_env("coordinator")
    cache = RedisBlacklist.from_settings(settings)
    recovery = RecoveryController(
        repo,
        [Worker(runner=r) for r in runners],
        RedisBlacklist(ScopedRedis(cache.client, namespace)),
        policy=replace(
            RecoveryPolicy(),
            backoff_seconds=0.1,
            maximum_backoff_seconds=0.5,
            worker_cooldown_seconds=0.5,
        ),
    )
    return recovery, RabbitProposalQueue(settings)


def evidence(repo, audit):
    results = [
        r for r in repo.list_results(audit["task_id"]) if r.WhichOneof("subject") == "proposal_id"
    ]
    failures = [r for r in results if r.WhichOneof("outcome") == "failure"]
    observations, baseline_samples, benchmark_failures = {}, [], []
    for record in repo.list_benchmarks(audit["task_id"]):
        refs = {a["key"]: a for a in repo.list_artifacts(audit["task_id"])}
        item = refs[record["report_key"]]
        report = json.loads(
            repo.artifacts.get(
                pb.ArtifactRef(**{k: item[k] for k in ("key", "sha256", "size_bytes")})
            )
        )
        raw = json.loads(repo.artifacts.get(pb.ArtifactRef(**report["raw"])))
        if raw["status"] != "completed":
            benchmark_failures.append({"run_id": report["run_id"], **raw.get("error", {})})
            continue
        base, candidate = [], []
        for case in raw["cases"].values():
            for pair in case["pairs"]:
                base.append(pair["samples"]["baseline"])
                candidate.append(pair["samples"]["candidate"])
        baseline_samples.extend(base)
        btime, ctime = (median(s["wall_seconds"] for s in group) for group in (base, candidate))
        bmem, cmem = (median(s["peak_memory_bytes"] for s in group) for group in (base, candidate))
        job = next(
            j
            for j in repo.list_jobs(audit["task_id"])
            if j.job_id == record["job_id"] and j.attempt == record["attempt"]
        )
        observations[job.proposal_id] = {
            "speedup_ratio": btime / ctime,
            "runtime_seconds": ctime,
            "baseline_runtime_seconds": btime,
            "peak_rss_bytes": cmem,
            "memory_change_fraction": cmem / bmem - 1,
            "measured_pairs": len(base),
            "qualified": report["analysis"]["qualified"],
            "qualification_reasons": report["analysis"]["reasons"],
            "case_qualification": report["analysis"]["cases"],
            "environment": raw["environment_before"],
        }
    with repo._transaction(write=False) as db:
        roles = [
            {"role": row[0], "calls": row[1]}
            for row in db.execute("SELECT role,COUNT(*) FROM agent_iterations GROUP BY role")
        ]
    attempts = [
        a
        for proposal in repo.list_proposals(audit["task_id"])
        for a in repo.recovery_attempts(audit["task_id"], proposal.proposal_id)
    ]
    return {
        "correctness_failures": sum(
            r.failure.reason not in (pb.INFRASTRUCTURE_ERROR, pb.UNKNOWN_FAILURE) for r in failures
        )
        + sum(
            item.get("kind") in ("output_mismatch", "program_failure")
            for item in benchmark_failures
        ),
        "benchmark_failures": benchmark_failures,
        "verification_failures_by_reason": {
            pb.FailureReason.Name(reason): sum(r.failure.reason == reason for r in failures)
            for reason in {r.failure.reason for r in failures}
        },
        "observations": observations,
        "baseline_samples": baseline_samples,
        "agent_calls": roles,
        "attempts": attempts,
    }


def collect_arm(root, checkpoint, target, image, strategy, run_id, repetitions):
    started = time.monotonic()
    budget = Budget(root / "cost-ledger.json")
    runners = [
        MeteredRunner(image, target=target, budget=budget, watchdog_directory=root / "watchdogs")
        for _ in range(2)
    ]
    catalogs = (
        [[item] for item in CATALOG[strategy]] if strategy == "single" else [CATALOG[strategy]]
    )
    audits, all_evidence = [], []
    for index, assignments in enumerate(catalogs):
        repo = clone_baseline(checkpoint, root / f"attempt-{index + 1}" / "state")
        recovery, broker = make_controller(
            repo, runners, f"evaluation:{run_id}:{root.name}:{index}:"
        )
        coordinator = CohortCoordinator(
            repo,
            runners[0],
            broker,
            recovery=recovery,
            backends={role: Recipe(recipe) for role, recipe in assignments},
        )
        task = repo.list_tasks()[0]["task_id"]
        frozen_policy = (
            replace(policy(), alpha=policy().alpha / 3) if strategy == "single" else policy()
        )
        audit = coordinator.run(
            task,
            agents=[role for role, _ in assignments],
            policy=frozen_policy,
            verification_plan=verification(),
            benchmark_plan=benchmark(repetitions),
        )
        write_json(repo.root.parent / "audit.json", audit)
        audits.append(audit)
        all_evidence.append(evidence(repo, audit))
    eligible = [c for audit in audits for c in audit["candidates"] if c["state"] == "eligible"]
    winner = min(eligible, key=lambda c: rank_key(c, policy())) if eligible else None
    observations = {
        key: value for item in all_evidence for key, value in item["observations"].items()
    }
    require_comparable(observations.values())
    return {
        "strategy": strategy,
        "candidate_slots": 3,
        "assignments": CATALOG[strategy],
        "selected_proposal_id": winner["proposal_id"] if winner else None,
        "status": "selected" if winner else "retained_baseline",
        "selected_measurements": observations.get(winner["proposal_id"]) if winner else None,
        "audits": audits,
        "observations": observations,
        "correctness_failures": sum(item["correctness_failures"] for item in all_evidence),
        "benchmark_failures": [
            failure for item in all_evidence for failure in item["benchmark_failures"]
        ],
        "verification_failures_by_reason": [
            item["verification_failures_by_reason"] for item in all_evidence
        ],
        "agent_calls": [row for item in all_evidence for row in item["agent_calls"]],
        "recovery_attempts": [a for item in all_evidence for a in item["attempts"]],
        "cost": {**budget.summary(), "optimization_elapsed_seconds": time.monotonic() - started},
        "baseline_samples": [
            sample for item in all_evidence for sample in item["baseline_samples"]
        ],
    }


def source_digest():
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def snapshot_source(output):
    """Preserve the implementation used by this run before any future source edits."""
    root = Path(__file__).resolve().parents[1]
    digest = source_digest()
    with zipfile.ZipFile(Path(output) / "implementation.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                archive.write(path, str(path.relative_to(root)))
    require(source_digest() == digest, "implementation changed during snapshot")
    return digest


def require_comparable(observations):
    identities = {
        json.dumps(
            {
                key: item["environment"].get(key)
                for key in (*ENVIRONMENT_KEYS, "execution_target", "native_execution")
            },
            sort_keys=True,
        )
        for item in observations
    }
    require(len(identities) <= 1, "evaluation environments differ across candidate measurements")


def run_evaluation(output, *, target, counts=(250000, 750000), trials=1, repetitions=15):
    output = Path(output).absolute()
    require(
        not output.exists(),
        "evaluation output already exists; fresh runs cannot overwrite evidence",
    )
    require(target in ("linux-x86_64", "linux-aarch64"), "evaluation needs a native CPU target")
    require(type(trials) is int and 1 <= trials <= 5, "trials must be 1–5")
    require(1 <= len(counts) <= 3 and len(set(counts)) == len(counts), "use 1–3 distinct workloads")
    benchmark(repetitions).encode()
    run_id = str(uuid4())
    output.mkdir(parents=True)
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "running",
        "source_tree_sha256": snapshot_source(output),
        "method": "deterministic single-generator versus multi-generator; shared safety reviewers",
        "target": target,
        "trials": trials,
        "repetitions": repetitions,
        "budget": {
            "candidate_slots": 3,
            "maximum_invocations": 256,
            "maximum_reserved_sandbox_seconds": 1800,
            "workers": 2,
            "cpu_quota": 1,
            "shared_family_alpha": 0.05,
        },
        "selection_policy": asdict(policy()),
        "benchmark_policy": json.loads(benchmark(repetitions).encode()),
        "candidate_catalog": CATALOG,
        "workloads": [],
        "limitations": [
            "Handwritten example rules, not an LLM comparison or evidence of general superiority.",
            "Single uses three isolated attempts; multi uses one three-generator cohort.",
            "Single proposal alpha .05/3 matches multi cohort Bonferroni correction.",
            "Role-specific gates remain enabled; sampling and resource caps are identical.",
            "One trial is an engineering demonstration, not a between-strategy significance test.",
            "Tokens and monetary cost are unavailable; no external models are called.",
        ],
    }
    write_json(output / "report.json", report)
    try:
        for trial in range(trials):
            for count in counts:
                root = output / f"trial-{trial + 1}" / f"reduction-{count}"
                package = workload(count, seed=160273)
                budget = Budget(root / "baseline-cost.json")
                runner = MeteredRunner(
                    DEFAULT_IMAGES[target],
                    target=target,
                    budget=budget,
                    watchdog_directory=root / "baseline-watchdogs",
                )
                require(
                    runner.native_execution,
                    "emulated comparisons cannot qualify; use a native Docker CPU profile",
                )
                require(
                    runner.measurement_version == 2, "evaluation requires native RSS measurement v2"
                )
                original = Repository(root / "baseline")
                verified = run_baseline(package, original, runner)
                require(verified.WhichOneof("outcome") == "success", "reference workload failed")
                order = ["single", "multi"]
                random.Random(273 + trial + count).shuffle(order)
                entry = {
                    "trial": trial + 1,
                    "name": package.manifest.name,
                    "input_seed": 160273,
                    "package_sha256": hashlib.sha256(package.package_bundle).hexdigest(),
                    "limits": json.loads(package.files["submission.json"])["limits"],
                    "baseline_setup_cost": budget.summary(),
                    "compiler": {
                        "image_digest": runner.image,
                        "hardware": json.loads(runner.hardware),
                        "measurement_version": runner.measurement_version,
                    },
                    "order": order,
                    "arms": {},
                }
                for strategy in order:
                    entry["arms"][strategy] = collect_arm(
                        root / strategy,
                        original,
                        target,
                        runner.image,
                        strategy,
                        run_id + f":{trial}:{count}",
                        repetitions,
                    )
                require_comparable(
                    item for arm in entry["arms"].values() for item in arm["observations"].values()
                )
                baselines = [
                    s for arm in entry["arms"].values() for s in arm.pop("baseline_samples")
                ]
                entry["original"] = {
                    "speedup_ratio": 1.0,
                    "memory_change_fraction": 0.0,
                    "runtime_seconds": median(s["wall_seconds"] for s in baselines)
                    if baselines
                    else None,
                    "peak_rss_bytes": median(s["peak_memory_bytes"] for s in baselines)
                    if baselines
                    else None,
                    "measured_samples": len(baselines),
                    "optimization_cost_seconds": 0,
                }
                report["workloads"].append(entry)
                write_json(output / "report.json", report)
        report["status"] = "complete"
    except BaseException as error:
        report.update(
            status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
            error_class=type(error).__name__,
        )
        write_json(output / "report.json", report)
        raise
    write_json(output / "report.json", report)
    return report
