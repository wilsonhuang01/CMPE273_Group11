"""Predeclared C/C++ families, independent Python oracles and unseen final inputs."""

import json
import random

from compiler_optimizer.agents.memory import BUFFERED_SOURCE, STREAMING_SOURCE
from compiler_optimizer.evaluation.workloads import Recipe
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.workers.package import package_from_files

FAMILIES = ("reduction", "histogram", "sorting")
HISTOGRAM = b"""#include <stdio.h>
#include <stdlib.h>
int main(void) {
    int count;
    if (scanf("%d", &count) != 1 || count < 0 || count > 1000000) return 1;
    int *values = malloc(((size_t)count + 1) * sizeof(*values));
    if (!values) return 1;
    for (int i = 0; i < count; ++i) {
        if (scanf("%d", &values[i]) != 1 || values[i] < -1000 || values[i] > 1000) {
            free(values); return 1;
        }
    }
    long long bins[32] = {0};
    for (int i = 0; i < count; ++i) bins[(values[i] + 1000) % 32]++;
    free(values);
    for (int i = 0; i < 32; ++i) printf("%lld%c", bins[i], i == 31 ? '\\n' : ' ');
    return 0;
}
"""
SORTING = b"""#include <algorithm>
#include <iostream>
#include <vector>
int main() {
    int count;
    if (!(std::cin >> count) || count < 0 || count > 1000000) return 1;
    std::vector<long long> values(count);
    for (auto &value : values) {
        if (!(std::cin >> value) || value < -1000 || value > 1000) return 1;
    }
    std::vector<long long> ordered(values);
    std::sort(ordered.begin(), ordered.end());
    long long checksum = 0;
    for (int i = 0; i < count; ++i) checksum += (i + 1LL) * ordered[i];
    std::cout << checksum << '\\n';
    return 0;
}
"""
SOURCES = {"reduction": BUFFERED_SOURCE, "histogram": HISTOGRAM, "sorting": SORTING}
CATALOGS = {
    "single": [("performance", f"buffer-{size}") for size in (4096, 65536, 262144)],
    "multi": [("performance", "buffer-65536"), ("memory", "storage"), ("architecture", "loop")],
    "debate": [("performance", "buffer-4096")],
}


def case(family, count, seed):
    rng = random.Random(seed)
    values = [rng.randint(-1000, 1000) for _ in range(count)]
    stdin = f"{count}\n" + " ".join(map(str, values)) + "\n"
    if family == "reduction":
        expected = f"{sum(values)}\n"
    elif family == "histogram":
        bins = [0] * 32
        for value in values:
            bins[(value + 1000) % 32] += 1
        expected = " ".join(map(str, bins)) + "\n"
    else:
        require(family == "sorting", "unknown corpus family")
        expected = f"{sum(i * value for i, value in enumerate(sorted(values), 1))}\n"
    return {"stdin": stdin, "args": [], "expected_stdout": expected}


def corpus(family, count, seed):
    require(family in FAMILIES, "unknown corpus family")
    require(type(count) is int and 1 <= count <= 1000000, "invalid corpus size")
    source = "main.cpp" if family == "sorting" else "main.c"
    files = {source: SOURCES[family]}
    tests = []
    for name, size, offset in (("empty", 0, 0), ("small", 17, 1), ("workload", count, 2)):
        value = case(family, size, seed + offset)
        files[f"tests/{name}.in"] = value["stdin"].encode()
        files[f"tests/{name}.out"] = value["expected_stdout"].encode()
        tests.append(
            {
                "id": name,
                "stdin": f"tests/{name}.in",
                "expected_stdout": f"tests/{name}.out",
                "args": [],
            }
        )
    files["submission.json"] = json.dumps(
        {
            "schema_version": 1,
            "name": f"{family}-{count}",
            "language": "cpp17" if family == "sorting" else "c17",
            "sources": [source],
            "tests": tests,
            "benchmark_test_ids": ["workload"],
            "limits": {
                "compile_wall_seconds": 30,
                "compile_memory_mib": 1024,
                "run_wall_seconds": 2,
                "run_memory_mib": 256,
                "output_kib": 64,
            },
        },
        sort_keys=True,
    ).encode()
    heldout = [case(family, size, seed + 10000 + i) for i, size in enumerate((31, 257))]
    require(
        not {c["stdin"].encode() for c in heldout}.intersection(files[t["stdin"]] for t in tests),
        "heldout overlaps training",
    )
    return package_from_files(files), heldout


def variant(family, name):
    source = SOURCES[family].decode()
    if name.startswith("buffer-"):
        size = int(name.split("-")[1])
        require(size in (4096, 65536, 262144), "unknown buffer size")
        if family == "sorting":
            setup = "    std::ios::sync_with_stdio(false);\n"
            if size >= 65536:
                setup += "    std::cin.tie(nullptr);\n"
            if size == 262144:
                setup += (
                    "    static char input_buffer[262144];\n"
                    "    std::cin.rdbuf()->pubsetbuf(input_buffer, sizeof(input_buffer));\n"
                )
            return source.replace("int main() {", "int main() {\n" + setup)
        return source.replace(
            "int main(void) {",
            "int main(void) {\n"
            f"    static char buffer[{size}];\n"
            "    setvbuf(stdin, buffer, _IOFBF, sizeof(buffer));",
        )
    if family == "reduction":
        if name == "storage":
            return STREAMING_SOURCE.decode()
        request = type(
            "Request",
            (),
            {
                "source": type(
                    "Source", (), {"bundle": type("Bundle", (), {"sha256": "0" * 64})()}
                )()
            },
        )()
        edit = json.loads(Recipe("unroll").generate(request, {"main.c": BUFFERED_SOURCE}))["edits"][
            0
        ]
        return source.replace(edit["old"], edit["new"])
    if family == "histogram":
        if name == "storage":
            return (
                source.replace(
                    "    int *values = malloc(((size_t)count + 1) * sizeof(*values));\n"
                    "    if (!values) return 1;",
                    "    long long bins[32] = {0};\n    int value;",
                )
                .replace("&values[i]", "&value")
                .replace("values[i] <", "value <")
                .replace("values[i] >", "value >")
                .replace("free(values); return 1;", "return 1;")
                .replace(
                    "        }\n    }\n    long long bins[32] = {0};\n"
                    "    for (int i = 0; i < count; ++i) bins[(values[i] + 1000) % 32]++;\n"
                    "    free(values);",
                    "        }\n        bins[(value + 1000) % 32]++;\n    }",
                )
            )
        return source.replace("(values[i] + 1000) % 32", "(values[i] + 1000) & 31")
    if name == "storage":
        return source.replace("std::vector<long long> ordered(values);", "auto &ordered = values;")
    return source.replace(
        "std::sort(ordered.begin(), ordered.end());",
        "std::sort(ordered.begin(), ordered.end(), "
        "[](long long a, long long b) { return a < b; });",
    )


class CorpusRecipe:
    def __init__(self, family, name):
        self.family, self.name = family, name

    def generate(self, request, sources):
        name = self.name
        if request.HasField("revision_context_json"):
            context = json.loads(request.revision_context_json)
            name = ("buffer-65536", "storage")[min(context["round"] - 2, 1)]
        path = next(iter(sources))
        return json.dumps(
            {
                "schema_version": 1,
                "parent_source_sha256": request.source.bundle.sha256,
                "transformation": f"corpus-{self.family}-{name}",
                "rationale": "Frozen rule; verify outputs and measure independently.",
                "edits": [
                    {"path": path, "old": sources[path].decode(), "new": variant(self.family, name)}
                ],
            },
            sort_keys=True,
        ).encode()
