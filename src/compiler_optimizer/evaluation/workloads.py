"""Frozen, deterministic reduction workloads and explicitly rule-based candidate catalogs."""

import json
import random

from compiler_optimizer.agents.memory import BUFFERED_SOURCE, ExampleMemoryBackend
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.workers.package import package_from_files


def workload(count, seed=273):
    require(type(count) is int and 1 <= count <= 1000000, "invalid reduction size")
    randomizer = random.Random(seed)
    values = [randomizer.randrange(-1000, 1001) for _ in range(count)]
    cases = {"empty": ([], 0), "mixed": ([5, -2, 7, -8], 2), "workload": (values, sum(values))}
    files = {"main.c": BUFFERED_SOURCE}
    tests = []
    for name, (items, expected) in cases.items():
        files[f"tests/{name}.in"] = (
            str(len(items)) + "\n" + " ".join(map(str, items)) + "\n"
        ).encode()
        files[f"tests/{name}.out"] = f"{expected}\n".encode()
        tests.append(
            {
                "id": name,
                "stdin": f"tests/{name}.in",
                "expected_stdout": f"tests/{name}.out",
                "args": [],
            }
        )
    manifest = {
        "schema_version": 1,
        "name": f"reduction-{count}",
        "language": "c17",
        "sources": ["main.c"],
        "tests": tests,
        "benchmark_test_ids": ["workload"],
        "limits": {
            "compile_wall_seconds": 30,
            "compile_memory_mib": 1024,
            "run_wall_seconds": 2,
            "run_memory_mib": 256,
            "output_kib": 64,
        },
    }
    files["submission.json"] = json.dumps(manifest, sort_keys=True).encode()
    return package_from_files(files)


class Recipe:
    def __init__(self, name):
        self.name = name

    def generate(self, request, sources):
        require(
            sources == {"main.c": BUFFERED_SOURCE}, "evaluation recipe requires the frozen workload"
        )
        if self.name in ("stream", "stream-buffered"):
            patch = json.loads(ExampleMemoryBackend().generate(request, sources))
            if self.name == "stream-buffered":
                patch["transformation"] = "evaluation-stream-buffered"
                patch["edits"][0]["new"] = patch["edits"][0]["new"].replace(
                    "int main(void) {\n",
                    "int main(void) {\n    static char input_buffer[65536];\n"
                    "    (void)setvbuf(stdin, input_buffer, _IOFBF, sizeof(input_buffer));\n",
                )
            return json.dumps(patch, sort_keys=True).encode()
        if self.name.startswith("stdio-"):
            size = int(self.name.split("-")[1])
            require(size in (4096, 65536, 262144), "unknown buffering recipe")
            old = "int main(void) {\n"
            new = (
                old
                + f"    static char input_buffer[{size}];\n"
                + ("    (void)setvbuf(stdin, input_buffer, _IOFBF, sizeof(input_buffer));\n")
            )
        elif self.name == "unroll":
            old = "    for (int i = 0; i < count; ++i) {\n        sum += values[i];\n    }"
            new = "    int i = 0;\n    for (; i + 3 < count; i += 4) {\n" + (
                "        sum += values[i] + values[i+1] + values[i+2] + values[i+3];\n    }\n"
                "    for (; i < count; ++i) sum += values[i];"
            )
        elif self.name == "wrong-output":
            old, new = 'printf("%lld\\n", sum);', 'printf("%lld\\n", sum+1);'
        elif self.name == "loop":
            old, new = "int main(void) {", "int main(void) { for (;;) {}"
        else:
            raise ValueError("unknown frozen recipe")
        return json.dumps(
            {
                "schema_version": 1,
                "parent_source_sha256": request.source.bundle.sha256,
                "transformation": "evaluation-" + self.name,
                "rationale": "Predeclared deterministic evaluation rule. Preserve declared "
                "valid-input outputs; correctness and measured gains require independent gates."
                if self.name not in ("wrong-output", "loop")
                else "Deliberately invalid failure-demo proposal.",
                "edits": [{"path": "main.c", "old": old, "new": new}],
            },
            sort_keys=True,
        ).encode()


CATALOG = {
    "single": [
        ("performance", "stdio-4096"),
        ("performance", "stdio-65536"),
        ("performance", "stdio-262144"),
    ],
    "multi": [("performance", "stdio-65536"), ("memory", "stream"), ("architecture", "unroll")],
}
