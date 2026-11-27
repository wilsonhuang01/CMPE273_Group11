"""Action P: frozen multi-family/trial experiment, real sandbox evidence and final holdout."""

import hashlib
import json
import random
import time
from dataclasses import asdict, replace
from pathlib import Path
from statistics import median
from uuid import uuid4

from compiler_optimizer.coordinator.debate import DebateCoordinator
from compiler_optimizer.coordinator.round import CohortCoordinator
from compiler_optimizer.evaluation.corpus import CATALOGS, FAMILIES, CorpusRecipe, corpus
from compiler_optimizer.evaluation.experiment import (
    benchmark,
    clone_baseline,
    evidence,
    make_controller,
    policy,
    require_comparable,
    snapshot_source,
    source_digest,
)
from compiler_optimizer.evaluation.instrumentation import Budget, MeteredRunner, write_json
from compiler_optimizer.shared.debate import DebatePolicy
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.selection import rank_key
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.baseline import run_baseline
from compiler_optimizer.workers.package import package_from_files, unbundle
from compiler_optimizer.workers.targets import DEFAULT_IMAGES
from compiler_optimizer.workers.verification_plan import VerificationPlan

STRATEGIES = ("original", "single", "multi", "debate")
BUDGET = {
    "candidate_slots": 3,
    "maximum_invocations": 256,
    "maximum_reserved_sandbox_seconds": 1800,
    "workers": 2,
    "external_model_calls": 0,
    "model_tokens": 0,
}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def protocol(*, target, trials=3, count=250000, repetitions=15, families=FAMILIES):
    require(target in DEFAULT_IMAGES and target != "linux-riscv64", "native CPU target required")
    require(target in ("linux-x86_64", "linux-aarch64"), "native CPU target required")
    require(type(trials) is int and 3 <= trials <= 5, "expanded evaluation requires 3–5 trials")
    require(type(count) is int and 1 <= count <= 1000000, "invalid workload size")
    require(tuple(families) == FAMILIES, "expanded evaluation requires all three families")
    plan = benchmark(repetitions)
    schedule = []
    for trial in range(1, trials + 1):
        for family in families:
            seed = 160273 + trial * 100 + families.index(family)
            package, heldout = corpus(family, count, seed)
            order = list(STRATEGIES)
            random.Random(seed + 273).shuffle(order)
            schedule.append(
                {
                    "trial": trial,
                    "family": family,
                    "seed": seed,
                    "count": count,
                    "package_sha256": hashlib.sha256(package.package_bundle).hexdigest(),
                    "heldout_sha256": digest(heldout),
                    "order": order,
                }
            )
    return {
        "schema_version": 2,
        "target": target,
        "trials": trials,
        "schedule": schedule,
        "candidate_catalog": CATALOGS,
        "budgets_per_optimizer_arm": BUDGET,
        "selection_policy": asdict(replace(policy(), maximum_candidates=3)),
        "verification_policy": json.loads(VerificationPlan().encode()),
        "benchmark_policy": json.loads(plan.encode()),
        "holdout": "Two unseen seeded inputs, Python oracle, one final veto; no reselection.",
        "uncertainty": "95% percentile bootstrap over trial blocks; descriptive, not superiority.",
        "backends": "Frozen deterministic rules; no hosted models, calls/tokens exactly zero.",
    }


def final_check(runner, package, heldout):
    """One final evaluation; hidden expected outputs are never passed to generators."""
    compiled = runner.compile(package)
    require(not compiled.failure, "final selected source failed to compile")
    records = []
    for index, item in enumerate(heldout):
        path = "__evaluation_holdout__"
        case = pb.TestCase(id=f"holdout-{index}", stdin=path, args=item["args"])
        result = runner.execute(
            compiled.binary,
            case,
            replace(package, files={**package.files, path: item["stdin"].encode()}),
        )
        records.append(
            {
                "index": index,
                "passed": not result.failure and result.stdout == item["expected_stdout"].encode(),
                **result.log(),
            }
        )
    return {"passed": all(r["passed"] for r in records), "cases": records}


def reference(root, runner, package, heldout, repetitions):
    compiled = runner.compile(package)
    require(not compiled.failure, "reference compilation failed")
    case = next(c for c in package.manifest.tests if c.id == "workload")
    before = runner.benchmark_environment()
    samples = []
    for i in range(repetitions + 1):
        result = runner.execute(compiled.binary, case, package, cpu_id=before["cpu_id"])
        require(
            not result.failure and result.stdout == package.files[case.expected_stdout],
            "reference measurement failed",
        )
        if i:
            samples.append(result.log())
    after = runner.benchmark_environment(before["cpu_id"])
    require_comparable([{"environment": before}, {"environment": after}])
    final = final_check(runner, package, heldout)
    require(final["passed"], "reference held-out oracle failed")
    write_json(
        root / "reference-samples.json",
        {
            "environment_before": before,
            "environment_after": after,
            "samples": samples,
            "heldout": final,
        },
    )
    return {
        "speedup_ratio": 1.0,
        "memory_change_fraction": 0.0,
        "runtime_seconds": median(s["wall_seconds"] for s in samples),
        "peak_rss_bytes": median(s["peak_memory_bytes"] for s in samples),
        "measured_samples": len(samples),
        "optimization_cost_seconds": 0,
        "environment": before,
    }


def collect(root, checkpoint, package, heldout, *, target, image, strategy, run_id, repetitions):
    started = time.monotonic()
    budget = Budget(
        root / "cost-ledger.json",
        maximum_invocations=BUDGET["maximum_invocations"],
        maximum_reserved_seconds=BUDGET["maximum_reserved_sandbox_seconds"],
    )
    runners = [
        MeteredRunner(image, target=target, budget=budget, watchdog_directory=root / "watchdogs")
        for _ in range(2)
    ]
    assignments = CATALOGS[strategy]
    catalogs = [[item] for item in assignments] if strategy == "single" else [assignments]
    audits, recorded, repositories = [], [], []
    family = package.manifest.name.rsplit("-", 1)[0]
    for index, catalog in enumerate(catalogs):
        repo = clone_baseline(checkpoint, root / f"attempt-{index + 1}" / "state")
        repositories.append(repo)
        recovery, broker = make_controller(repo, runners, f"evaluation:{run_id}:{index}:")
        options = {}
        cls = CohortCoordinator
        if strategy == "debate":
            cls = DebateCoordinator
            options["debate_policy"] = DebatePolicy(
                max_rounds=3,
                max_agent_calls=32,
                heldout_cases=tuple({"stdin": c["stdin"], "args": c["args"]} for c in heldout),
            )
        coordinator = cls(
            repo,
            runners[0],
            broker,
            recovery=recovery,
            backends={role: CorpusRecipe(family, recipe) for role, recipe in catalog},
            **options,
        )
        task = repo.list_tasks()[0]["task_id"]
        selection = replace(
            policy(),
            maximum_candidates=3,
            alpha=policy().alpha / 3 if strategy == "single" else policy().alpha,
        )
        audit = coordinator.run(
            task,
            agents=[role for role, _ in catalog],
            policy=selection,
            verification_plan=VerificationPlan(),
            benchmark_plan=benchmark(repetitions),
        )
        write_json(repo.root.parent / "audit.json", audit)
        audits.append(audit)
        recorded.append(evidence(repo, audit))
    # Only the coordinator's final eligible winner can enter cross-attempt single selection.
    candidates = [
        (c, repo)
        for audit, repo in zip(audits, repositories, strict=True)
        for c in audit["candidates"]
        if c["proposal_id"] == audit["selected_proposal_id"]
    ]
    winner = min(candidates, key=lambda item: rank_key(item[0], policy())) if candidates else None
    observations = {key: value for item in recorded for key, value in item["observations"].items()}
    require_comparable(observations.values())
    selected = None
    final_package = package
    if winner:
        candidate, repo = winner
        selected = candidate["proposal_id"]
        task = repo.list_tasks()[0]["task_id"]
        proposal = next(p for p in repo.list_proposals(task) if p.proposal_id == selected)
        sources = unbundle(repo.artifacts.get(proposal.source.bundle))
        final_package = package_from_files({**package.files, **sources})
    final = final_check(runners[0], final_package, heldout)
    if not final["passed"]:
        selected = None  # Veto, never try the next best candidate on held-out inputs.
    attempts = [a for item in recorded for a in item["attempts"]]
    retries = [a for a in attempts if a["attempt"] > 1]
    actual = sum(len(repo.list_proposals(repo.list_tasks()[0]["task_id"])) for repo in repositories)
    valid = sum(
        r.WhichOneof("outcome") == "success"
        for repo in repositories
        for r in repo.list_results(repo.list_tasks()[0]["task_id"])
        if r.WhichOneof("subject") == "proposal_id"
    )
    require(actual <= 3, "candidate budget exceeded")
    return {
        "strategy": strategy,
        "candidate_slots": 3,
        "generated_proposals": actual,
        "valid_proposals": valid,
        "selected_proposal_id": selected,
        "status": "selected" if selected else "retained_baseline",
        "selected_measurements": observations.get(selected),
        "heldout": final,
        "audits": audits,
        "observations": observations,
        "correctness_failures": sum(item["correctness_failures"] for item in recorded)
        + sum(not c["passed"] for c in final["cases"]),
        "benchmark_failures": [f for item in recorded for f in item["benchmark_failures"]],
        "agent_calls": [r for item in recorded for r in item["agent_calls"]],
        "recovery_attempts": attempts,
        "retry_attempts": len(retries),
        "recovery_seconds_lower_bound": sum(
            max(0, a["finished_ms"] - a["started_ms"]) / 1000
            for a in retries
            if a["finished_ms"] is not None
        ),
        "cost": {
            **budget.summary(),
            "model_tokens": 0,
            "optimization_elapsed_seconds": time.monotonic() - started,
        },
    }


def run_expanded(output, *, target, trials=3, count=250000, repetitions=15):
    output = Path(output).absolute()
    require(not output.exists(), "evaluation output exists; no overwrite or resampling")
    declaration = protocol(target=target, trials=trials, count=count, repetitions=repetitions)
    output.mkdir(parents=True)
    write_json(
        output / "protocol.json", declaration
    )  # Before probing Docker or seeing measurements.
    run_id = str(uuid4())
    report = {
        "schema_version": 2,
        "run_id": run_id,
        "status": "running",
        "target": target,
        "trials": trials,
        "protocol_sha256": digest(declaration),
        "source_tree_sha256": snapshot_source(output),
        "workloads": [],
        "limitations": [
            "Three fixed families with deterministic rules; no LLM or general superiority claim.",
            "Native single-host Docker measurements; no physical-host, GPU or cloud claims.",
            "Trial-block bootstrap intervals are descriptive with only 3–5 independent seeds.",
            "Equal three-slot sandbox/model caps; unused slots remain unused.",
            "Single uses isolated proposals, multi a cohort; debate revises actionable failures.",
            "Held-out inputs veto final outputs; they never choose replacement candidates.",
            "Original measurements and setup cost are separate from optimization cost.",
            "No hosted model calls: zero calls/tokens; monetary cost is unavailable.",
        ],
    }
    write_json(output / "report.json", report)
    try:
        for declared in declaration["schedule"]:
            package, heldout = corpus(declared["family"], count, declared["seed"])
            require(
                hashlib.sha256(package.package_bundle).hexdigest() == declared["package_sha256"]
                and digest(heldout) == declared["heldout_sha256"],
                "corpus changed after declaration",
            )
            root = output / f"trial-{declared['trial']}" / declared["family"]
            write_json(root / "heldout.json", heldout)
            setup = Budget(root / "baseline-cost.json")
            runner = MeteredRunner(
                DEFAULT_IMAGES[target],
                target=target,
                budget=setup,
                watchdog_directory=root / "baseline-watchdogs",
            )
            require(
                runner.native_execution and runner.measurement_version == 2,
                "expanded evaluation requires native CPU measurement v2",
            )
            checkpoint = Repository(root / "baseline")
            result = run_baseline(package, checkpoint, runner)
            require(result.WhichOneof("outcome") == "success", "reference workload failed")
            entry = {
                **declared,
                "name": package.manifest.name,
                "arms": {},
                "compiler": {
                    "image_digest": runner.image,
                    "hardware": json.loads(runner.hardware),
                    "measurement_version": runner.measurement_version,
                },
            }
            report["workloads"].append(entry)
            for strategy in declared["order"]:
                if strategy == "original":
                    entry["original"] = reference(root, runner, package, heldout, repetitions)
                else:
                    entry["arms"][strategy] = collect(
                        root / strategy,
                        checkpoint,
                        package,
                        heldout,
                        target=target,
                        image=runner.image,
                        strategy=strategy,
                        run_id=f"{run_id}:{declared['trial']}:{declared['family']}:{strategy}",
                        repetitions=repetitions,
                    )
                write_json(output / "report.json", report)
            entry["baseline_setup_cost"] = setup.summary()
            require_comparable(
                [
                    {"environment": entry["original"]["environment"]},
                    *[o for a in entry["arms"].values() for o in a["observations"].values()],
                ]
            )
        from compiler_optimizer.evaluation.uncertainty import summarize

        report["summary"] = summarize(report)
        report["status"] = "complete"
    except BaseException as error:
        report.update(
            status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
            error_class=type(error).__name__,
        )
        write_json(output / "report.json", report)
        raise
    require(source_digest() == report["source_tree_sha256"], "implementation changed during run")
    require(
        digest(json.loads((output / "protocol.json").read_text())) == report["protocol_sha256"],
        "protocol changed during evaluation",
    )
    write_json(output / "report.json", report)
    return report
