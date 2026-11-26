"""Bounded, durable stage and capacity metrics. Never export model text or identities."""

import json
from collections import Counter, defaultdict

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from compiler_optimizer.monitoring.metrics import family, histogram
from compiler_optimizer.shared.fleet import FRESH_MS


def coordinator_metrics(repo):
    with repo._transaction(write=False) as db:
        workers = list(db.execute("SELECT last_seen_ms,active_job FROM fleet_workers"))
        jobs = Counter((r[0],) for r in db.execute("SELECT state FROM fleet_jobs"))
        windows = Counter((r[0],) for r in db.execute("SELECT state FROM measurement_windows"))
        rounds = Counter(
            ("initial" if r[0] == 1 else "revision",)
            for r in db.execute(
                "SELECT round_number FROM debate_slots WHERE context_key IS NOT NULL "
                "OR proposal_id IN (SELECT proposal_id FROM proposals)"
            )
        )
        outcomes = [
            repo._ref(db, r[0])
            for r in db.execute(
                "SELECT outcome_key FROM selection_sessions WHERE outcome_key IS NOT NULL"
            )
        ]
        progress = Counter((r[0],) for r in db.execute("SELECT status FROM selection_progress"))
    reasons = Counter()
    allowed = {
        "verification_failed",
        "review_veto_or_abstention",
        "benchmark_not_qualified",
        "candidate_failed_during_measurement",
        "no_reliable_weighted_gain",
        "invalid_or_missing_evidence",
        "duplicate_source",
        "generation_failed",
        "assessment_failed",
        "incomparable_environment_or_baseline",
        "emulated_target_correctness_only",
        "evaluation_not_committed",
    }
    for ref in outcomes:
        for candidate in json.loads(repo.artifacts.get(ref)).get("candidates", []):
            for reason in set(candidate.get("reasons", [])):
                reasons[(reason if reason in allowed else "other",)] += 1
    yield family(
        CounterMetricFamily,
        "candidate_rejections",
        "Final candidate rejection reasons.",
        ["reason"],
        reasons,
    )
    now = repo.fleet_now()
    capacity = Counter({(s,): 0 for s in ("idle", "busy", "stale")})
    for worker in workers:
        state = "stale" if now - worker[0] > FRESH_MS else "busy" if worker[1] else "idle"
        capacity[(state,)] += 1
    yield family(
        GaugeMetricFamily,
        "fleet_workers",
        "Independent fleet worker availability.",
        ["state"],
        capacity,
    )
    yield family(
        GaugeMetricFamily, "fleet_jobs", "Primitive jobs by current state.", ["state"], jobs
    )
    yield family(
        GaugeMetricFamily,
        "measurement_windows",
        "Engine measurement reservations.",
        ["state"],
        windows,
    )
    yield family(
        CounterMetricFamily, "generation_rounds", "Started generation slots.", ["kind"], rounds
    )
    yield family(
        CounterMetricFamily,
        "candidate_evaluations",
        "Committed evaluation outcomes.",
        ["status"],
        progress,
    )


class AgentCollector:
    def __init__(self, store):
        self.store = store

    def collect(self):
        # Select only redacted scalar fields; prompt/response bodies and error text stay private.
        with self.store.transaction() as db:
            calls = list(
                db.execute("SELECT state,input_tokens,output_tokens,latency_ms FROM model_calls")
            )
            operations = list(
                db.execute("SELECT operation,state,started_ms,finished_ms FROM operations")
            )
        counts, tokens, latency, stages = Counter(), Counter(), defaultdict(list), defaultdict(list)
        for state, inputs, outputs, milliseconds in calls:
            counts[(state,)] += 1
            tokens[("input",)] += inputs
            tokens[("output",)] += outputs
            if milliseconds is not None:
                latency[(state,)].append(milliseconds / 1000)
        for operation, state, started, finished in operations:
            method = operation.split(":", 1)[0]
            method = method if method in {"generate", "review", "plan"} else "other"
            if started is not None and finished is not None:
                stages[(method, state)].append(max(0, finished - started) / 1000)
        yield family(
            CounterMetricFamily,
            "model_calls",
            "Reserved provider calls including retries.",
            [],
            {(): sum(counts.values())},
        )
        yield family(
            CounterMetricFamily,
            "model_tokens",
            "Provider-reported usage; unknown is zero.",
            ["direction"],
            tokens,
        )
        yield histogram(
            "model_call_duration_seconds",
            "Finished provider call latency.",
            ["state"],
            latency,
            [0.1, 1, 5, 30, 120, 600],
        )
        yield histogram(
            "agent_operation_duration_seconds",
            "Latest completed operation attempt.",
            ["method", "state"],
            stages,
            [0.1, 1, 5, 30, 120, 600],
        )
