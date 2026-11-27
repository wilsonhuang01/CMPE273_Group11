"""Portable CSV/HTML views of immutable experiment results (no external web dependencies)."""

import csv
import html
from pathlib import Path


def rows(report):
    result = []
    for item in report["workloads"]:
        common = {"trial": item["trial"], "workload": item["name"]}
        result.append(
            {
                **common,
                "strategy": "original",
                "status": "reference",
                **item["original"],
                "correctness_failures": 0,
                "optimization_elapsed_seconds": 0,
                "sandbox_invocations": 0,
                "valid_proposals": 0,
                "generated_proposals": 0,
                "fallback": 0,
                "retry_attempts": 0,
                "recovery_seconds_lower_bound": 0,
                "external_model_calls": 0,
                "model_tokens": 0,
            }
        )
        for name, arm in item["arms"].items():
            selected = arm["selected_measurements"]
            result.append(
                {
                    **common,
                    "strategy": name,
                    "status": arm["status"],
                    "speedup_ratio": selected["speedup_ratio"] if selected else 1.0,
                    "memory_change_fraction": selected["memory_change_fraction"]
                    if selected
                    else 0.0,
                    "runtime_seconds": selected["runtime_seconds"]
                    if selected
                    else item["original"]["runtime_seconds"],
                    "peak_rss_bytes": selected["peak_rss_bytes"]
                    if selected
                    else item["original"]["peak_rss_bytes"],
                    "correctness_failures": arm["correctness_failures"],
                    "optimization_elapsed_seconds": arm["cost"]["optimization_elapsed_seconds"],
                    "sandbox_invocations": arm["cost"]["invocations"],
                    "valid_proposals": arm.get("valid_proposals"),
                    "generated_proposals": arm.get("generated_proposals"),
                    "fallback": int(arm["status"] != "selected"),
                    "retry_attempts": arm.get("retry_attempts"),
                    "recovery_seconds_lower_bound": arm.get("recovery_seconds_lower_bound"),
                    "external_model_calls": arm["cost"]["external_model_calls"],
                    "model_tokens": 0 if report.get("schema_version") == 2 else None,
                }
            )
    return result


def render_report(report, output):
    output = Path(output)
    data = rows(report)
    fields = [
        "trial",
        "workload",
        "strategy",
        "status",
        "speedup_ratio",
        "memory_change_fraction",
        "runtime_seconds",
        "peak_rss_bytes",
        "correctness_failures",
        "optimization_elapsed_seconds",
        "sandbox_invocations",
        "valid_proposals",
        "generated_proposals",
        "fallback",
        "retry_attempts",
        "recovery_seconds_lower_bound",
        "external_model_calls",
        "model_tokens",
    ]
    with (output / "summary.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(data)
    headings = [
        "Trial / workload",
        "Strategy",
        "Result",
        "Speedup (×)",
        "Memory change (%)",
        "Runtime (ms)",
        "Peak RSS (MiB)",
        "Correctness failures",
        "Optimization cost (s)",
        "Sandbox calls",
    ]
    body = []
    for row in data:
        values = [
            f"{row['trial']} / {row['workload']}",
            row["strategy"],
            row["status"],
            f"{row['speedup_ratio']:.3f}",
            f"{100 * row['memory_change_fraction']:+.2f}",
            "unavailable"
            if row["runtime_seconds"] is None
            else f"{1000 * row['runtime_seconds']:.2f}",
            "unavailable"
            if row["peak_rss_bytes"] is None
            else f"{row['peak_rss_bytes'] / 1024**2:.2f}",
            row["correctness_failures"],
            f"{row['optimization_elapsed_seconds']:.2f}",
            row["sandbox_invocations"],
        ]
        body.append(
            "<tr>" + "".join("<td>" + html.escape(str(v)) + "</td>" for v in values) + "</tr>"
        )
    text = (
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Optimizer evaluation</title>'
    )
    text += (
        "<style>body{font:15px system-ui;max-width:1400px;margin:40px auto;padding:0 "
        "24px;color:#19232b}h1{font-size:26px}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:12px"
        " 8px;text-align:left;border-bottom:1px solid "
        "#ddd}p,li{line-height:1.6}small{color:#555}</style>"
    )
    text += (
        "<h1>Original vs single-generator vs multi-generator"
        + (" vs debate" if report.get("schema_version") == 2 else "")
        + "</h1>"
    )
    text += (
        "<p>Native CPU evaluation · identical workloads, three candidate slots per optimizer,"
        " shared safety gates.</p>"
    )
    text += (
        "<p><strong>Scope:</strong> deterministic handwritten rule backends; these results do"
        " not establish LLM or general multi-agent superiority.</p>"
    )
    if "summary" in report:
        text += summary_html(report, output)
    text += "<h2>Individual trials and workloads</h2>"
    text += (
        "<table><thead><tr>"
        + "".join("<th>" + h + "</th>" for h in headings)
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table>"
    )
    text += (
        "<p>Speedup is the ratio of paired-sample medians for the selected verified "
        "candidate; retained originals are 1× / 0% change. Original absolute values pool "
        "matched baseline samples. Lower memory change is better. Costs include actual used "
        "work, not the unused budget.</p>"
    )
    text += (
        "<h2>Declared protocol and limits</h2><ul>"
        + "".join("<li>" + html.escape(v) + "</li>" for v in report["limitations"])
        + "</ul>"
    )
    if "recovery_demo" in report:
        demo = report["recovery_demo"]
        text += "<h2>Automatic recovery demonstration</h2><ul>"
        for scenario in demo["scenarios"]:
            attempts = scenario["evidence"]["attempts"]
            text += (
                "<li>"
                + html.escape(
                    f"{scenario['name']}: {'passed' if scenario['passed'] else 'failed'}; "
                    f"{scenario['elapsed_seconds']:.2f} s; "
                    + " → ".join(a["status"] for a in attempts)
                )
                + "</li>"
            )
        text += (
            "</ul><p>Compiler disconnect is injected; loops and output mismatches execute "
            'in real containers. Full receipts: <a href="demo.json">demo.json</a>.</p>'
        )
    text += (
        "<p>Candidate-level observations, qualification failures, agent calls, retry "
        "histories, source/image fingerprints and complete budgets are in <a "
        'href="report.json">report.json</a>. Raw audit and SQLite/CAS evidence are preserved '
        "alongside it.</p>"
    )
    text += (
        "<small>Run "
        + html.escape(report["run_id"])
        + " · target "
        + html.escape(report["target"])
        + " · "
        + html.escape(report["status"])
        + "</small></html>"
    )
    (output / "report.html").write_text(text)
    return data


def summary_html(report, output):
    """Readable intervals plus a machine-readable table for every reported metric."""
    columns = {
        "speedup_ratio": ("Speedup (×)", 1),
        "runtime_seconds": ("Runtime (ms)", 1000),
        "peak_rss_bytes": ("Peak RSS (MiB)", 1 / 1024**2),
        "fallback_fraction": ("Fallback (%)", 100),
        "correctness_failures": ("Correctness failures", 1),
        "valid_proposals": ("Valid proposals", 1),
        "optimization_elapsed_seconds": ("Optimization (s)", 1),
        "retry_attempts": ("Retry attempts", 1),
        "recovery_seconds_lower_bound": ("Recovery ≥ (s)", 1),
        "model_calls": ("Model calls", 1),
        "model_tokens": ("Model tokens", 1),
    }
    flat = []
    for group in report["summary"]["groups"]:
        for name, values in group["metrics"].items():
            flat.append(
                {"family": group["family"], "strategy": group["strategy"], "metric": name, **values}
            )
    with (Path(output) / "uncertainty.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=["family", "strategy", "metric", "mean", "lower", "upper", "trials"]
        )
        writer.writeheader()
        writer.writerows(flat)
    text = (
        "<h2>Trial uncertainty</h2><p>Mean [95% descriptive bootstrap interval]. "
        "The resampling unit is the independent trial block, preserving all program families. "
        "Three trials provide limited precision. Missing intervals are shown explicitly. "
        "These intervals do not establish strategy superiority. "
        '<a href="protocol.json">Frozen protocol</a> · '
        '<a href="uncertainty.csv">All metric intervals (CSV)</a>.</p>'
    )
    for family in ("all_families", "reduction", "histogram", "sorting"):
        title = "Equal-weight mean across families" if family == "all_families" else family.title()
        if family != "all_families":
            text += "<details><summary>" + title + "</summary>"
        else:
            text += "<h3>" + title + "</h3>"
        text += '<div style="overflow-x:auto"><table><thead><tr><th>Strategy</th>'
        text += "".join("<th>" + label + "</th>" for label, _ in columns.values())
        text += "</tr></thead><tbody>"
        for group in report["summary"]["groups"]:
            if group["family"] != family:
                continue
            text += "<tr><td>" + html.escape(group["strategy"]) + "</td>"
            for name, (_, scale) in columns.items():
                value = group["metrics"][name]
                display = f"{value['mean'] * scale:.3g}"
                display += (
                    " [unavailable]"
                    if value["lower"] is None
                    else f" [{value['lower'] * scale:.3g}, {value['upper'] * scale:.3g}]"
                )
                text += "<td>" + html.escape(display) + "</td>"
            text += "</tr>"
        text += "</tbody></table></div>"
        if family != "all_families":
            text += "</details>"
    text += "<p>Paired speedup differences (trial-block intervals): "
    for comparison in report["summary"]["paired_differences"]:
        text += html.escape(
            f"{comparison['comparison']}: {comparison['mean']:.3g} "
            f"[{comparison['lower']}, {comparison['upper']}]; "
        )
    return text + "</p>"
