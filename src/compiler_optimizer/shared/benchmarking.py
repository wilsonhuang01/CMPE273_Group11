"""Reproducible benchmark policy and conservative paired-sample analysis."""

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from statistics import median

from compiler_optimizer.shared.manifest import require

MAX_INVOCATIONS = 4096
ENVIRONMENT_KEYS = (
    "engine_id",
    "boot_id",
    "engine_architecture",
    "cpu_id",
    "allowed_cpus",
    "cpu_model",
    "cpu_features_sha256",
    "kernel",
    "governor",
    "clock_resolution_seconds",
    "native_amd64",
)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate benchmark-plan field")
        result[key] = value
    return result


@dataclass(frozen=True)
class BenchmarkPlan:
    schema_version: int = 1
    warmups: int = 2
    repetitions: int = 15
    seed: int = 273
    cpu_id: int | None = None
    minimum_relative_improvement: float = 0.05
    maximum_relative_mad: float = 0.10
    maximum_memory_regression: float = 0.05
    alpha: float = 0.05
    minimum_sample_seconds: float = 0.01
    objective: str = "runtime"
    maximum_runtime_regression: float = 0.05

    @classmethod
    def parse(cls, data):
        if isinstance(data, bytes | str):
            require(len(data) <= 16384, "benchmark plan exceeds 16 KiB")
            data = json.loads(data, object_pairs_hook=_pairs)
        all_fields = set(asdict(cls()))
        legacy_fields = all_fields - {"objective", "maximum_runtime_regression"}
        require(isinstance(data, dict), "benchmark plan must be an object")
        legacy = data.get("schema_version") == 1
        require(
            set(data) in (legacy_fields, all_fields) if legacy else set(data) == all_fields,
            "benchmark plan has missing/unknown fields",
        )
        data = dict(data)
        data.setdefault("objective", "runtime")
        data.setdefault("maximum_runtime_regression", 0.05)
        require(data["objective"] in ("runtime", "memory"), "unsupported benchmark objective")
        require(
            not legacy
            or (data["objective"] == "runtime" and data["maximum_runtime_regression"] == 0.05),
            "v1 plans only support runtime",
        )
        for key, low, high in (
            ("schema_version", 1, 2),
            ("warmups", 0, 10),
            ("repetitions", 7, 101),
            ("seed", 0, 2**32 - 1),
        ):
            require(
                type(data[key]) is int and low <= data[key] <= high,
                "invalid benchmark integer: " + key,
            )
        require(
            data["cpu_id"] is None or (type(data["cpu_id"]) is int and 0 <= data["cpu_id"] <= 8191),
            "invalid benchmark CPU ID",
        )
        for key, low, high in (
            ("minimum_relative_improvement", 0.0001, 0.99),
            ("maximum_relative_mad", 0, 1),
            ("maximum_memory_regression", 0, 1),
            ("alpha", 0.000001, 0.1),
            ("minimum_sample_seconds", 0.000001, 10),
            ("maximum_runtime_regression", 0, 1),
        ):
            value = data[key]
            require(
                type(value) in (int, float) and low <= value <= high and math.isfinite(value),
                "invalid benchmark threshold: " + key,
            )
        return cls(**data)

    def encode(self):
        value = asdict(self)
        self.parse(value)
        if self.schema_version == 1:
            value.pop("objective")
            value.pop("maximum_runtime_regression")
        data = json.dumps(value, sort_keys=True, allow_nan=False).encode()
        self.parse(data)
        return data


def pair_order(plan, test_id, index):
    first = hashlib.sha256(f"{plan.seed}:{test_id}".encode()).digest()[0] & 1
    return ["baseline", "candidate"] if (first + index) % 2 == 0 else ["candidate", "baseline"]


def _number(value, *, positive=False):
    return (
        type(value) in (int, float)
        and math.isfinite(value)
        and (value > 0 if positive else value >= 0)
    )


def _environment(value):
    require(
        isinstance(value, dict) and all(key in value for key in ENVIRONMENT_KEYS),
        "incomplete benchmark environment",
    )
    require(
        isinstance(value["allowed_cpus"], list)
        and bool(value["allowed_cpus"])
        and all(type(cpu) is int and 0 <= cpu <= 8191 for cpu in value["allowed_cpus"])
        and value["cpu_id"] in value["allowed_cpus"]
        and type(value["cpu_id"]) is int,
        "invalid worker CPU affinity",
    )
    require(_number(value["clock_resolution_seconds"], positive=True), "invalid worker clock")
    require(
        type(value["native_amd64"]) is bool
        and value["native_amd64"] == (value["engine_architecture"] in ("amd64", "x86_64")),
        "invalid native worker claim",
    )
    if "execution_target" in value or "native_execution" in value:
        require(
            value.get("execution_target") == "linux-aarch64"
            and type(value.get("native_execution")) is bool
            and value["native_execution"] == (value["engine_architecture"] in ("aarch64", "arm64")),
            "invalid target-aware native execution claim",
        )
    for key in ("engine_id", "boot_id", "cpu_model", "cpu_features_sha256", "kernel"):
        require(
            isinstance(value[key], str) and 0 < len(value[key]) <= 2048,
            "missing worker identity: " + key,
        )


def _sample(sample, cpu_id, *, complete):
    require(
        isinstance(sample, dict)
        and _number(sample.get("wall_seconds"))
        and type(sample.get("peak_memory_bytes")) is int
        and sample["peak_memory_bytes"] >= 0,
        "invalid benchmark sample",
    )
    require(
        sample.get("cpu_id") == cpu_id
        and sample.get("build_profile") == "normal"
        and sample.get("watchdog_removed") is True,
        "uncontrolled benchmark sample",
    )
    require(type(sample.get("output_valid")) is bool, "missing output validation")
    if complete:
        require(
            sample.get("returncode") == 0
            and sample.get("failure") is None
            and sample["output_valid"]
            and sample["wall_seconds"] > 0
            and sample["peak_memory_bytes"] > 0,
            "unsuccessful sample in completed benchmark",
        )


def validate_raw(raw, plan, test_ids):
    require(
        isinstance(raw, dict)
        and raw.get("schema_version") == 1
        and raw.get("status") in ("completed", "failed"),
        "invalid benchmark raw data",
    )
    require(
        2 * (plan.warmups + plan.repetitions) * len(test_ids) <= MAX_INVOCATIONS,
        "benchmark exceeds invocation budget",
    )
    complete = raw["status"] == "completed"
    if raw.get("environment_before") is not None:
        _environment(raw["environment_before"])
        require(
            plan.cpu_id is None or raw["environment_before"]["cpu_id"] == plan.cpu_id,
            "benchmark affinity differs from requested CPU",
        )
    if complete:
        _environment(raw.get("environment_before"))
        _environment(raw.get("environment_after"))
    cases = raw.get("cases")
    require(isinstance(cases, dict) and set(cases) <= set(test_ids), "unknown benchmark workload")
    if complete:
        require(set(cases) == set(test_ids), "incomplete benchmark workloads")
    for test_id, case in cases.items():
        for phase, count in (("warmups", plan.warmups), ("pairs", plan.repetitions)):
            pairs = case.get(phase)
            require(isinstance(pairs, list) and len(pairs) <= count, "invalid benchmark pair count")
            if complete:
                require(len(pairs) == count, "missing measured/warmup pairs")
            for index, pair in enumerate(pairs):
                offset = 0 if phase == "warmups" else plan.warmups
                require(
                    pair.get("order") == pair_order(plan, test_id, offset + index),
                    "benchmark run ordering differs from policy",
                )
                samples = pair.get("samples")
                require(
                    isinstance(samples, dict) and set(samples) <= {"baseline", "candidate"},
                    "invalid paired samples",
                )
                if complete:
                    require(set(samples) == {"baseline", "candidate"}, "unpaired measurements")
                for sample in samples.values():
                    require(raw.get("environment_before") is not None, "sample without environment")
                    _sample(sample, raw["environment_before"]["cpu_id"], complete=complete)
    if not complete:
        error = raw.get("error")
        require(
            isinstance(error, dict)
            and error.get("kind")
            in ("program_failure", "output_mismatch", "infrastructure", "measurement")
            and error.get("role") in ("baseline", "candidate", None),
            "invalid benchmark error",
        )


def invalidated_role(raw):
    error = raw.get("error") or {}
    return (
        error.get("role") if error.get("kind") in ("program_failure", "output_mismatch") else None
    )


def analyze(raw, plan, test_ids):
    """Exact one-sided paired sign test + median lower bound, with Bonferroni correction.

    No samples are trimmed. Bounds assume independent pairs from a stable worker.
    Additional duration/MAD/native/memory gates can only make acceptance stricter.
    """
    validate_raw(raw, plan, test_ids)
    summary = {"qualified": False, "decision": "retain_baseline", "reasons": [], "cases": {}}
    if plan.schema_version >= 2:
        summary["objective"] = plan.objective
    if raw["status"] != "completed":
        summary["reasons"].append("execution_failed")
        return summary
    before, after = raw["environment_before"], raw["environment_after"]
    if not before.get("native_execution", before["native_amd64"]):
        summary["reasons"].append("emulated_worker")
    if any(
        before.get(key) != after.get(key)
        for key in (*ENVIRONMENT_KEYS, "execution_target", "native_execution")
    ):
        summary["reasons"].append("environment_changed")
    alpha = plan.alpha / (len(test_ids) * (2 if plan.objective == "memory" else 1))
    n = plan.repetitions
    rank = max(
        (k for k in range(1, n + 1) if sum(math.comb(n, j) for j in range(k)) / 2**n <= alpha),
        default=0,
    )
    for test_id in test_ids:
        pairs = raw["cases"][test_id]["pairs"]
        baseline = [pair["samples"]["baseline"]["wall_seconds"] for pair in pairs]
        candidate = [pair["samples"]["candidate"]["wall_seconds"] for pair in pairs]
        gains = [1 - c / b for b, c in zip(baseline, candidate, strict=True)]
        bmed, cmed = median(baseline), median(candidate)
        bmad = median(abs(value - bmed) for value in baseline) / bmed
        cmad = median(abs(value - cmed) for value in candidate) / cmed
        bmemory = [pair["samples"]["baseline"]["peak_memory_bytes"] for pair in pairs]
        cmemory = [pair["samples"]["candidate"]["peak_memory_bytes"] for pair in pairs]
        runtime_gains = gains
        if plan.objective == "memory":
            gains = [1 - c / b for b, c in zip(bmemory, cmemory, strict=True)]
        wins = sum(gain > plan.minimum_relative_improvement for gain in gains)
        p_value = sum(math.comb(n, k) for k in range(wins, n + 1)) / 2**n
        lower = sorted(gains)[rank - 1] if rank else None
        reasons = []
        if min(bmed, cmed) < max(
            plan.minimum_sample_seconds, 100 * before["clock_resolution_seconds"]
        ):
            reasons.append("samples_too_short")
        if max(bmad, cmad) > plan.maximum_relative_mad:
            reasons.append("unstable_runtime")
        if median(cmemory) > median(bmemory) * (1 + plan.maximum_memory_regression) or max(
            cmemory
        ) > max(bmemory) * (1 + plan.maximum_memory_regression):
            reasons.append("memory_regression")
        if plan.objective == "memory":
            memory_mad = max(
                median(abs(v - median(values)) for v in values) / median(values)
                for values in (bmemory, cmemory)
            )
            if memory_mad > plan.maximum_relative_mad:
                reasons.append("unstable_memory")
            # One-sided upper bound on paired runtime regression; ties at the budget are safe.
            regression_upper = sorted([-gain for gain in runtime_gains])[n - rank] if rank else None
            if (
                regression_upper is None
                or regression_upper > plan.maximum_runtime_regression
                or cmed > bmed * (1 + plan.maximum_runtime_regression)
            ):
                reasons.append("runtime_regression")
        if lower is None or lower <= plan.minimum_relative_improvement or p_value > alpha:
            reasons.append("no_reliable_improvement")
        summary["cases"][test_id] = {
            "baseline_median_seconds": bmed,
            "candidate_median_seconds": cmed,
            "baseline_relative_mad": bmad,
            "candidate_relative_mad": cmad,
            "median_relative_gain": median(gains),
            "median_gain_lower_bound": lower,
            "wins_above_minimum_gain": wins,
            "one_sided_sign_p": p_value,
            "per_case_alpha": alpha,
            "baseline_peak_memory_bytes": max(bmemory),
            "candidate_peak_memory_bytes": max(cmemory),
            "baseline_median_memory_bytes": median(bmemory),
            "candidate_median_memory_bytes": median(cmemory),
            "reasons": reasons,
        }
        if plan.objective == "memory":
            summary["cases"][test_id].update(
                objective="memory",
                memory_relative_mad=memory_mad,
                runtime_regression_upper_bound=regression_upper,
                median_runtime_relative_gain=median(runtime_gains),
            )
    if any(case["reasons"] for case in summary["cases"].values()):
        summary["reasons"].append("workload_gate_failed")
    summary["qualified"] = not summary["reasons"]
    if summary["qualified"]:
        summary["decision"] = "eligible_candidate"
    return summary
