"""Explicit weighted selection, paired confidence bounds and deterministic tie breaking."""

import json
import math
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from statistics import mean, median

from compiler_optimizer.shared.benchmarking import validate_raw
from compiler_optimizer.shared.manifest import require

TIE_BREAKERS = [
    "weighted_lower_bound",
    "weighted_point_score",
    "worst_case_lower_bound",
    "lower_peak_memory_ratio",
    "lower_runtime_ratio",
    "source_sha256",
    "proposal_id",
]


def _unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate selection policy field")
        result[key] = value
    return result


@dataclass(frozen=True)
class SelectionPolicy:
    schema_version: int = 1
    performance_weight: float = 0.7
    memory_weight: float = 0.3
    minimum_weighted_gain: float = 0.01
    alpha: float = 0.05
    tie_digits: int = 6
    maximum_candidates: int = 6

    @classmethod
    def parse(cls, data):
        if isinstance(data, bytes | str):
            require(len(data) <= 16384, "selection policy exceeds 16 KiB")
            data = json.loads(data, object_pairs_hook=_unique)
        require(
            isinstance(data, dict) and set(data) == set(asdict(cls())),
            "selection policy has missing/unknown fields",
        )
        for key, low, high in (
            ("schema_version", 1, 1),
            ("tie_digits", 0, 12),
            ("maximum_candidates", 1, 6),
        ):
            require(
                type(data[key]) is int and low <= data[key] <= high,
                "invalid selection integer: " + key,
            )
        for key, low, high in (
            ("performance_weight", 0, 1),
            ("memory_weight", 0, 1),
            ("minimum_weighted_gain", 0, 0.99),
            ("alpha", 0.000001, 0.1),
        ):
            value = data[key]
            require(
                type(value) in (int, float) and math.isfinite(value) and low <= value <= high,
                "invalid selection number: " + key,
            )
        require(
            math.isclose(
                data["performance_weight"] + data["memory_weight"], 1, rel_tol=0, abs_tol=1e-12
            ),
            "selection weights must sum to one",
        )
        return cls(**data)

    def encode(self):
        data = json.dumps(asdict(self), sort_keys=True, allow_nan=False).encode()
        self.parse(data)
        return data


def weighted_statistics(raw, plan, test_ids, policy, cohort_size):
    validate_raw(raw, plan, test_ids)
    require(raw["status"] == "completed", "selection needs completed paired measurements")
    require(1 <= cohort_size <= policy.maximum_candidates, "invalid selection cohort size")
    alpha = policy.alpha / (cohort_size * len(test_ids))
    n = plan.repetitions
    rank = max(
        (k for k in range(1, n + 1) if sum(math.comb(n, j) for j in range(k)) / 2**n <= alpha),
        default=0,
    )
    cases = {}
    for test_id in test_ids:
        pairs = raw["cases"][test_id]["pairs"]
        times, memory, gains = [], [], []
        for pair in pairs:
            baseline, candidate = (pair["samples"][role] for role in ("baseline", "candidate"))
            t = 1 - candidate["wall_seconds"] / baseline["wall_seconds"]
            m = 1 - candidate["peak_memory_bytes"] / baseline["peak_memory_bytes"]
            times.append(t)
            memory.append(m)
            gains.append(policy.performance_weight * t + policy.memory_weight * m)
        require(all(math.isfinite(value) for value in gains), "nonfinite weighted gain")
        wins = sum(gain > policy.minimum_weighted_gain for gain in gains)
        p_value = sum(math.comb(n, k) for k in range(wins, n + 1)) / 2**n
        lower = sorted(gains)[rank - 1] if rank else None
        cases[test_id] = {
            "performance_gain": median(times),
            "memory_gain": median(memory),
            "weighted_gain": median(gains),
            "weighted_lower_bound": lower,
            "sign_test_p": p_value,
            "per_case_alpha": alpha,
            "reliable": lower is not None
            and lower > policy.minimum_weighted_gain
            and p_value <= alpha,
        }
    reliable = all(case["reliable"] for case in cases.values())
    return {
        "reliable": reliable,
        "performance_gain": mean(case["performance_gain"] for case in cases.values()),
        "memory_gain": mean(case["memory_gain"] for case in cases.values()),
        "weighted_point_score": mean(case["weighted_gain"] for case in cases.values()),
        "weighted_lower_bound": mean(case["weighted_lower_bound"] for case in cases.values())
        if rank
        else None,
        "worst_case_lower_bound": min(case["weighted_lower_bound"] for case in cases.values())
        if rank
        else None,
        "peak_memory_ratio": max(
            max(p["samples"]["candidate"]["peak_memory_bytes"] for p in raw["cases"][tid]["pairs"])
            / max(p["samples"]["baseline"]["peak_memory_bytes"] for p in raw["cases"][tid]["pairs"])
            for tid in test_ids
        ),
        "runtime_ratio": max(
            median(p["samples"]["candidate"]["wall_seconds"] for p in raw["cases"][tid]["pairs"])
            / median(p["samples"]["baseline"]["wall_seconds"] for p in raw["cases"][tid]["pairs"])
            for tid in test_ids
        ),
        "cases": cases,
    }


def rank_key(candidate, policy):
    def quantize(value):
        return int(
            (Decimal(str(value)) * 10**policy.tie_digits).to_integral_value(
                rounding=ROUND_HALF_EVEN
            )
        )

    stats = candidate["statistics"]
    return (
        -quantize(stats["weighted_lower_bound"]),
        -quantize(stats["weighted_point_score"]),
        -quantize(stats["worst_case_lower_bound"]),
        quantize(stats["peak_memory_ratio"]),
        quantize(stats["runtime_ratio"]),
        candidate["source_sha256"],
        candidate["proposal_id"],
    )
