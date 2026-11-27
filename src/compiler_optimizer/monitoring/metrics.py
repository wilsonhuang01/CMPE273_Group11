"""Prometheus views of durable coordinator evidence, without per-task metric labels."""

import json
from collections import Counter, defaultdict

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, HistogramMetricFamily

from compiler_optimizer.monitoring.events import emit
from compiler_optimizer.shared.recovery import RecoveryPolicy
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.workers.recovery_evidence import CRASH_REASONS


def family(kind, name, help_text, labels, values):
    result = kind("optimizer_" + name, help_text, labels=labels)
    for keys, value in sorted(values.items()):
        result.add_metric(list(keys), value)
    return result


def histogram(name, help_text, labels, groups, bounds, *, signed=False):
    result = HistogramMetricFamily("optimizer_" + name, help_text, labels=labels)
    for keys, values in sorted(groups.items()):
        buckets = [(str(b), sum(v <= b for v in values)) for b in bounds]
        buckets.append(("+Inf", len(values)))
        result.add_metric(list(keys), buckets, None if signed else sum(values))
    return result


class RepositoryCollector:
    """One consistent read transaction; counts survive exporter restarts and redelivery."""

    def __init__(self, repository):
        self.repository = repository

    def collect(self):
        repo, now = self.repository, self.repository.recovery_now()
        with repo._transaction(write=False) as db:
            tasks = [
                dict(r)
                for r in db.execute(
                    "SELECT t.task_id,t.status,MIN(h.recorded_at_unix_ms) AS started,"
                    "MAX(CASE WHEN h.status IN ('completed','failed') "
                    "THEN h.recorded_at_unix_ms END) AS finished "
                    "FROM tasks t JOIN task_history h ON h.task_id=t.task_id GROUP BY t.task_id"
                )
            ]
            attempts = [dict(r) for r in db.execute("SELECT * FROM recovery_attempts")]
            jobs = [dict(r) for r in db.execute("SELECT * FROM recovery_jobs")]
            workers = [dict(r) for r in db.execute("SELECT * FROM recovery_workers")]
            outbox = [dict(r) for r in db.execute("SELECT * FROM recovery_outbox")]
            results = [bytes(r[0]) for r in db.execute("SELECT payload FROM results")]
            decisions = [dict(r) for r in db.execute("SELECT * FROM decisions")]
            # Raw failure reports can include measurement-only crashes. Completed batches
            # need no large raw artifact read. Read artifacts only after releasing SQLite.
            benchmarks = [
                dict(r)
                for r in db.execute(
                    "SELECT a.* FROM artifacts a JOIN benchmark_runs b ON b.report_key=a.key"
                )
            ]
            policies = {
                r["key"]: pb.ArtifactRef(**dict(r))
                for r in db.execute(
                    "SELECT DISTINCT a.* FROM artifacts a JOIN recovery_jobs j "
                    "ON j.policy_key=a.key"
                )
            }
        from compiler_optimizer.monitoring.stages import coordinator_metrics

        yield from coordinator_metrics(repo)
        yield histogram(
            "execution_attempt_duration_seconds",
            "Finished leased execution attempts including retries.",
            ["status"],
            {
                (status,): [
                    max(0, a["finished_ms"] - a["started_ms"]) / 1000
                    for a in attempts
                    if a["status"] == status and a["finished_ms"] is not None
                ]
                for status in {a["status"] for a in attempts}
            },
            [0.1, 1, 5, 30, 120, 600],
        )
        task_counts = Counter(
            {(state,): 0 for state in ("accepted", "running", "completed", "failed")}
        )
        task_counts.update((t["status"],) for t in tasks)
        yield family(
            GaugeMetricFamily,
            "tasks",
            "Tasks by current durable state.",
            ["status"],
            task_counts,
        )
        durations, ages = defaultdict(list), []
        for task in tasks:
            if task["finished"] is not None:
                durations[(task["status"],)].append(
                    max(0, task["finished"] - task["started"]) / 1000
                )
            else:
                ages.append(max(0, now - task["started"]) / 1000)
        yield histogram(
            "task_duration_seconds",
            "Accepted to terminal task duration.",
            ["status"],
            durations,
            [1, 5, 15, 30, 60, 120, 300, 900, 3600, 10800],
        )
        yield GaugeMetricFamily(
            "optimizer_oldest_active_task_seconds",
            "Age of oldest accepted or running task.",
            value=max(ages, default=0),
        )
        yield family(
            GaugeMetricFamily,
            "recovery_jobs",
            "Recovery jobs by current state.",
            ["state"],
            Counter((j["state"],) for j in jobs),
        )
        yield family(
            GaugeMetricFamily,
            "recovery_attempts",
            "Attempts by mutable current status.",
            ["status"],
            Counter((a["status"],) for a in attempts),
        )
        retry = Counter(
            {
                (reason,): 0
                for reason in (
                    "infrastructure_failure",
                    "program_crash",
                    "lease_expired",
                    "no_healthy_worker",
                    "other",
                )
            }
        )
        terminal = Counter(
            {
                (reason,): 0
                for reason in (
                    "infrastructure_attempts_exhausted",
                    "confirmed_program_crash",
                    "blacklisted_execution_context",
                    "sandbox_cleanup_unconfirmed",
                    "unstable_program_failure",
                    "unconfirmed_program_failure",
                    "invalid_worker_evidence",
                    "other",
                )
            }
        )
        for event in outbox:
            if event["kind"] in ("retry", "deadletter"):
                target = retry if event["kind"] == "retry" else terminal
                reason = json.loads(event["payload_json"])["reason"]
                target[(reason if (reason,) in target else "other",)] += 1
        yield family(
            CounterMetricFamily,
            "retries",
            "Durably scheduled retries, not deliveries.",
            ["reason"],
            retry,
        )
        yield family(
            CounterMetricFamily,
            "recovery_terminal",
            "Terminal recovery receipts.",
            ["reason"],
            terminal,
        )
        yield GaugeMetricFamily(
            "optimizer_recovery_outbox_pending",
            "Unconfirmed RabbitMQ or Redis publications.",
            value=sum(e["sent_ms"] is None for e in outbox),
        )
        job_freshness = {}
        for job in jobs:
            job_freshness[job["job_id"]] = RecoveryPolicy.parse(
                repo.artifacts.get(policies[job["policy_key"]])
            ).worker_health_seconds
        last_job = {
            a["worker_id"]: a["job_id"]
            for a in sorted(attempts, key=lambda a: (a["started_ms"], a["attempt"]))
            if a["worker_id"] is not None
        }
        health = Counter({(state,): 0 for state in ("idle", "busy", "quarantined", "stale")})
        heartbeat_ages = []
        stale_assigned = 0
        for worker in workers:
            age = max(0, now - worker["last_seen_ms"]) / 1000
            heartbeat_ages.append(age)
            state = worker["state"]
            assigned = worker["current_job_id"] or last_job.get(worker["worker_id"])
            if state != "quarantined" and age > job_freshness.get(assigned, 10):
                state = "stale"
                stale_assigned += int(worker["current_job_id"] is not None)
            health[(state,)] += 1
        yield family(
            GaugeMetricFamily,
            "workers",
            "Registered execution workers by effective health.",
            ["state"],
            health,
        )
        yield GaugeMetricFamily(
            "optimizer_stale_assigned_workers",
            "Workers with an active assignment whose heartbeat has expired.",
            value=stale_assigned,
        )
        yield GaugeMetricFamily(
            "optimizer_worker_heartbeat_max_age_seconds",
            "Maximum registered worker heartbeat age.",
            value=max(heartbeat_ages, default=0),
        )
        checks = Counter()
        crashes = Counter(
            {("verification", pb.FailureReason.Name(reason)): 0 for reason in CRASH_REASONS}
        )
        crashes[("measurement", "PROGRAM_FAILURE")] = 0
        for payload in results:
            result = pb.VerificationResult.FromString(payload)
            outcome = result.WhichOneof("outcome")
            reason = (
                pb.FailureReason.Name(result.failure.reason) if outcome == "failure" else "NONE"
            )
            stage = pb.FailureStage.Name(result.failure.stage) if outcome == "failure" else "NONE"
            checks[(outcome, reason, stage)] += 1
            if (
                outcome == "failure"
                and result.failure.reason in CRASH_REASONS
                and (result.failure.stage in (pb.TEST, pb.MEASURE))
            ):
                crashes[("verification", reason)] += 1
        for reference in benchmarks:
            report = json.loads(repo.artifacts.get(pb.ArtifactRef(**reference)))
            if "execution_failed" not in report["analysis"]["reasons"]:
                continue
            raw = json.loads(repo.artifacts.get(pb.ArtifactRef(**report["raw"])))
            if raw.get("error", {}).get("kind") == "program_failure":
                crashes[("measurement", "PROGRAM_FAILURE")] += 1
        yield family(
            CounterMetricFamily,
            "verification_results",
            "Committed verification outcomes.",
            ["outcome", "reason", "stage"],
            checks,
        )
        yield family(
            CounterMetricFamily,
            "crashes",
            "Committed runtime failure observations.",
            ["phase", "reason"],
            crashes,
        )
        gains = defaultdict(list)
        for decision in decisions:
            scores = json.loads(decision["scores_json"])
            for metric in ("performance_gain", "memory_gain", "weighted_score"):
                if metric in scores:
                    gains[(metric,)].append(scores[metric])
        yield histogram(
            "optimization_gain_ratio",
            "Final selected gains; retained baseline is zero.",
            ["metric"],
            gains,
            [-10, -1, -0.5, 0, 0.01, 0.05, 0.1, 0.25, 0.5, 1],
            signed=True,
        )
        yield family(
            GaugeMetricFamily,
            "optimization_gain_mean_ratio",
            "Mean committed selected gain including retained-baseline zeros.",
            ["metric"],
            {key: sum(values) / len(values) for key, values in gains.items() if values},
        )
        yield GaugeMetricFamily(
            "optimizer_metrics_snapshot_timestamp_seconds",
            "Time of successful evidence snapshot.",
            value=now / 1000,
        )


class Metrics:
    def __init__(self, *, repository=None, readiness=None, agent_store=None):
        self.repository, self.readiness = repository, readiness
        self.agent_store = agent_store
        self.collector = RepositoryCollector(repository) if repository is not None else None

    def render(self):
        registry = CollectorRegistry()
        if self.collector is not None:
            registry.register(self.collector)
        if self.agent_store is not None:
            from compiler_optimizer.monitoring.stages import AgentCollector

            registry.register(AgentCollector(self.agent_store))
        if self.readiness is not None:
            snapshot = self.readiness.snapshot()
            gauges = GaugeMetricFamily(
                "optimizer_service_ready", "Cached service readiness.", labels=["service"]
            )
            gauges.add_metric([snapshot["service"]], int(snapshot["infrastructure_ready"]))
            registry.register(_Fixed(gauges))
            checks = GaugeMetricFamily(
                "optimizer_dependency_up",
                "Cached dependency probe status.",
                labels=["service", "dependency"],
            )
            for name, value in snapshot["checks"].items():
                checks.add_metric([snapshot["service"], name], int(value == "ok"))
            registry.register(_Fixed(checks))
        measurements = {}
        # Avoid a second repository scan, and log only scalar aggregate samples.
        families = list(registry.collect())
        for metric in families:
            for sample in metric.samples:
                if sample.name.endswith(("_bucket", "_sum", "_count")):
                    continue
                label = ",".join(f"{k}={v}" for k, v in sorted(sample.labels.items()))
                measurements[sample.name + "{" + label + "}"] = sample.value
        emit("metrics_snapshot", measurements=measurements)
        snapshot = CollectorRegistry()
        snapshot.register(_FixedMany(families))
        return generate_latest(snapshot)


class _Fixed:
    def __init__(self, metric):
        self.metric = metric

    def collect(self):
        yield self.metric


class _FixedMany:
    def __init__(self, metrics):
        self.metrics = metrics

    def collect(self):
        yield from self.metrics
