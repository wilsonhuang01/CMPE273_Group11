"""Create a bounded larger Memory Agent workload without committing megabytes of fixtures."""

import argparse
import json
from pathlib import Path

from compiler_optimizer.agents.memory import BUFFERED_SOURCE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--count", type=int, default=1000000)
    args = parser.parse_args()
    if not 1 <= args.count <= 1000000:
        parser.error("count must be 1–1000000")
    root = Path(args.output)
    if root.exists() or root.is_symlink():
        parser.error("output directory must not exist")
    manifest = {
        "schema_version": 1,
        "name": "sum-array-memory-workload",
        "language": "c17",
        "sources": ["main.c"],
        "tests": [
            {
                "id": "empty",
                "stdin": "tests/empty.in",
                "expected_stdout": "tests/empty.out",
                "args": [],
            },
            {
                "id": "large",
                "stdin": "tests/large.in",
                "expected_stdout": "tests/large.out",
                "args": [],
            },
        ],
        "benchmark_test_ids": ["large"],
        "limits": {
            "compile_wall_seconds": 30,
            "compile_memory_mib": 1024,
            "run_wall_seconds": 10,
            "run_memory_mib": 256,
            "output_kib": 64,
        },
    }
    root.mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "main.c").write_bytes(BUFFERED_SOURCE)
    (root / "submission.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (root / "tests/empty.in").write_bytes(b"0\n")
    (root / "tests/empty.out").write_bytes(b"0\n")
    (root / "tests/large.in").write_text(f"{args.count}\n" + "1 " * args.count + "\n")
    (root / "tests/large.out").write_text(f"{args.count}\n")
    print(root)


if __name__ == "__main__":
    main()
