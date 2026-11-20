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
    text += "<h1>Original vs single-generator vs multi-generator</h1>"
    text += (
        "<p>Native CPU evaluation · identical workloads, three candidate slots per optimizer,"
        " shared safety gates.</p>"
    )
    text += (
        "<p><strong>Scope:</strong> deterministic handwritten rule backends; these results do"
        " not establish LLM or general multi-agent superiority.</p>"
    )
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
