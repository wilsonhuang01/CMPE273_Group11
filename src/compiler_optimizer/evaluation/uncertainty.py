"""Descriptive uncertainty with trial blocks, preserving paired families and strategies."""

import random
from collections import defaultdict
from statistics import mean


def interval(blocks, *, seed=273, draws=4000):
    """Bootstrap the mean of independent trial means; never count samples as trials."""
    if not blocks:
        return {"mean": None, "lower": None, "upper": None, "trials": 0}
    values = [mean(items) for _, items in sorted(blocks.items())]
    point = mean(values)
    if len(values) < 3:
        return {"mean": point, "lower": None, "upper": None, "trials": len(values)}
    rng = random.Random(seed)
    distribution = sorted(mean(rng.choices(values, k=len(values))) for _ in range(draws))
    return {
        "mean": point,
        "lower": distribution[int(draws * 0.025)],
        "upper": distribution[int(draws * 0.975) - 1],
        "trials": len(values),
    }


def summarize(report):
    groups = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for entry in report["workloads"]:
        trial = entry["trial"]
        for strategy in ("original", *entry["arms"]):
            arm = entry["arms"].get(strategy)
            chosen = arm["selected_measurements"] if arm else None
            measured = chosen or entry["original"]
            metrics = {
                "speedup_ratio": chosen["speedup_ratio"] if chosen else 1.0,
                "memory_change_fraction": chosen["memory_change_fraction"] if chosen else 0.0,
                "runtime_seconds": measured["runtime_seconds"],
                "peak_rss_bytes": measured["peak_rss_bytes"],
                "fallback_fraction": int(arm["status"] != "selected") if arm else 0,
                "correctness_failures": arm["correctness_failures"] if arm else 0,
                "valid_proposals": arm["valid_proposals"] if arm else 0,
                "generated_proposals": arm["generated_proposals"] if arm else 0,
                "optimization_elapsed_seconds": arm["cost"]["optimization_elapsed_seconds"]
                if arm
                else 0,
                "retry_attempts": arm["retry_attempts"] if arm else 0,
                "recovery_seconds_lower_bound": arm["recovery_seconds_lower_bound"] if arm else 0,
                "sandbox_invocations": arm["cost"]["invocations"] if arm else 0,
                "model_calls": 0,
                "model_tokens": 0,
            }
            for scope in ("all_families", entry["family"]):
                for name, value in metrics.items():
                    if value is not None:
                        groups[(scope, strategy)][name][trial].append(value)
    result = [
        {
            "family": family,
            "strategy": strategy,
            "metrics": {name: interval(blocks) for name, blocks in metrics.items()},
        }
        for (family, strategy), metrics in sorted(groups.items())
    ]
    # Paired differences retain each trial's workload and baseline alignment.
    differences = []
    for left, right in (("multi", "single"), ("debate", "single")):
        blocks = defaultdict(list)
        for entry in report["workloads"]:

            def speed(strategy, entry=entry):
                selected = entry["arms"][strategy]["selected_measurements"]
                return selected["speedup_ratio"] if selected else 1.0

            blocks[entry["trial"]].append(speed(left) - speed(right))
        differences.append(
            {"comparison": f"{left} minus {right}", "metric": "speedup_ratio", **interval(blocks)}
        )
    return {
        "confidence_level": 0.95,
        "resampling_unit": "trial block",
        "bootstrap_seed": 273,
        "bootstrap_draws": 4000,
        "groups": result,
        "paired_differences": differences,
    }
