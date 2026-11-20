"""Evaluate or demonstrate the optimizer against real local infrastructure."""

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from compiler_optimizer.evaluation.demo import run_demo
from compiler_optimizer.evaluation.experiment import run_evaluation
from compiler_optimizer.evaluation.report import render_report
from compiler_optimizer.monitoring.events import configure_logging


def native_target():
    result = subprocess.run(
        ["docker", "info", "--format", "{{.Architecture}}"],
        capture_output=True,
        check=True,
        timeout=10,
    )
    architecture = result.stdout.decode().strip()
    if architecture in ("aarch64", "arm64"):
        return "linux-aarch64"
    if architecture in ("amd64", "x86_64"):
        return "linux-x86_64"
    raise ValueError("no supported native CPU target for this Docker engine")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("evaluate", "demo"))
    parser.add_argument("--output")
    parser.add_argument("--target", choices=("linux-x86_64", "linux-aarch64"))
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--counts", default="250000,750000")
    parser.add_argument("--repetitions", type=int)
    args = parser.parse_args()
    configure_logging()
    target = args.target or native_target()
    suffix = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    output = Path(args.output or f"var/{args.mode}/{suffix}")
    if args.mode == "evaluate":
        report = run_evaluation(
            output,
            target=target,
            counts=tuple(map(int, args.counts.split(","))),
            trials=args.trials,
            repetitions=args.repetitions or 15,
        )
        render_report(report, output)
    else:
        report = run_demo(output, target=target, repetitions=args.repetitions or 31)
    print(
        json.dumps({"status": report["status"], "target": target, "output": str(output.absolute())})
    )
    return 0 if report["status"] in ("complete", "passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
