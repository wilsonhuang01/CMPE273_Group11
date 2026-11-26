"""Supplementary Google Benchmark kernels and perf evidence; never qualification inputs."""

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from compiler_optimizer.shared.benchmarking import ENVIRONMENT_KEYS
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import Repository
from compiler_optimizer.storage.pipeline import reference
from compiler_optimizer.workers.docker_runner import DockerRunner, InfrastructureError
from compiler_optimizer.workers.package import unbundle
from compiler_optimizer.workers.perf import validate_counters
from compiler_optimizer.workers.verification_plan import VerificationPlan
from compiler_optimizer.workers.verify import _snapshot


@dataclass(frozen=True)
class KernelBenchmark:
    kernel: str = "sum-i64-v1"
    size: int = 4096
    seed: int = 273
    iterations: int = 64
    repetitions: int = 5
    warmups: int = 2

    def validate(self):
        require(self.kernel in ("sum-i64-v1", "dot-i64-v1"), "unsupported benchmark kernel")
        for key, low, high in (
            ("size", 16, 65536),
            ("seed", 0, 2**32 - 1),
            ("iterations", 1, 1024),
            ("repetitions", 3, 11),
            ("warmups", 0, 32),
        ):
            require(
                type(getattr(self, key)) is int and low <= getattr(self, key) <= high,
                "invalid kernel benchmark bound: " + key,
            )
        return asdict(self)

    def driver(self):
        self.validate()
        a = [(i * 17 + self.seed) % 33 - 16 for i in range(self.size)]
        b = [(i * 7 + self.seed) % 17 - 8 for i in range(self.size)]
        dot = self.kernel == "dot-i64-v1"
        expected = sum(x * y for x, y in zip(a, b, strict=True)) if dot else sum(a)
        declaration = (
            "co_dot(const int64_t*, const int64_t*, size_t)"
            if dot
            else "co_sum(const int64_t*, size_t)"
        )
        call = "co_dot(a.data(),b.data(),a.size())" if dot else "co_sum(a.data(),a.size())"
        return f"""
#include <benchmark/benchmark.h>
#include <cstdint>
#include <cstdlib>
#include <vector>
extern "C" int64_t {declaration};
static void Kernel(benchmark::State& state) {{
    std::vector<int64_t> a({self.size}),b({self.size});
    for(size_t i=0;i<a.size();++i) {{
        a[i]=static_cast<int64_t>((i*17+{self.seed}ULL)%33)-16;
        b[i]=static_cast<int64_t>((i*7+{self.seed}ULL)%17)-8;
    }}
    for(int i=0;i<{self.warmups};++i) if({call}!={expected}) std::abort();
    for(auto _ : state) {{
        auto result={call};
        benchmark::DoNotOptimize(result);
        if(result!={expected}) std::abort();
        benchmark::ClobberMemory();
    }}
    state.SetItemsProcessed(state.iterations()*{self.size});
}}
BENCHMARK(Kernel)->Iterations({self.iterations})->Repetitions({self.repetitions})->MeasureProcessCPUTime()->UseRealTime();
BENCHMARK_MAIN();
""".encode()


def parse_samples(data, policy):
    value = json.loads(data)
    rows = [r for r in value.get("benchmarks", []) if r.get("run_type") == "iteration"]
    require(len(rows) == policy.repetitions, "missing Google Benchmark repetitions")
    for row in rows:
        require(
            not row.get("error_occurred")
            and row.get("time_unit") == "ns"
            and row.get("iterations") == policy.iterations,
            "Google Benchmark workload differs from declared policy",
        )
        for key in ("real_time", "cpu_time"):
            require(
                type(row.get(key)) in (int, float)
                and math.isfinite(row[key])
                and (row[key] > 0 if key == "real_time" else row[key] >= 0),
                f"invalid Google Benchmark sample: {key}={str(row.get(key))[:80]}",
            )
    nonfinite = []

    def normalize(item, path=""):
        if isinstance(item, float) and not math.isfinite(item):
            nonfinite.append(path)
            return None
        if isinstance(item, dict):
            return {key: normalize(child, f"{path}.{key}") for key, child in item.items()}
        if isinstance(item, list):
            return [normalize(child, f"{path}[{i}]") for i, child in enumerate(item)]
        return item

    # Derived statistics such as CV(0, 0, 0) may be NaN even for valid samples.
    # Keep strict JSON reports, name unusable fields, and retain raw tool bytes.
    value = normalize(value)
    value["nonfinite_fields"] = nonfinite
    rows = [row for row in value["benchmarks"] if row.get("run_type") == "iteration"]
    return value, rows


def verified_source(repository, task_id, proposal_id, runner):
    task, original, _ = _snapshot(repository, task_id)
    require(
        runner.native_execution
        and runner.compiler(original.manifest.language) == task["submission"].compiler,
        "use the verified native compiler/profile",
    )
    results = [
        r
        for r in repository.list_results(task_id)
        if r.proposal_id == proposal_id
        and r.WhichOneof("outcome") == "success"
        and r.HasField("correctness_report")
    ]
    require(bool(results), "mandatory correctness verification must pass first")
    owner = results[-1]
    proposal = next(p for p in repository.list_proposals(task_id) if p.proposal_id == proposal_id)
    require(not proposal.HasField("ir"), "supplementary adapters require source proposals")
    return original, proposal, owner


def persist(repository, task_id, owner, report):
    ref = repository.artifacts.put(json.dumps(report, sort_keys=True, allow_nan=False).encode())
    repository.attach_artifact(
        task_id, ref, kind="benchmark", job_id=owner.job_id, attempt=owner.attempt
    )
    return report, ref


def run_microbenchmark(repository, task_id, proposal_id, runner, policy=None):
    policy = policy or KernelBenchmark()
    policy.validate()
    original, proposal, owner = verified_source(repository, task_id, proposal_id, runner)
    require(original.manifest.language == "cpp17", "kernel harness requires C++17")
    sources = unbundle(repository.artifacts.get(proposal.source.bundle))
    require(
        "co_benchmark_driver.cpp" not in sources and len(sources) < 100,
        "reserved benchmark adapter source",
    )
    driver = policy.driver()
    sources["co_benchmark_driver.cpp"] = driver
    manifest = type(original.manifest)()
    manifest.CopyFrom(original.manifest)
    manifest.sources.append("co_benchmark_driver.cpp")
    package = replace(
        original, manifest=manifest, sources=sources, files={**original.files, **sources}
    )
    with runner.measurement_window():
        build = runner.compile(package, build_profile="microbenchmark")
        if build.failure or not build.binary:
            raise InfrastructureError(
                "Google Benchmark harness failed to compile: "
                + repository.artifacts.put(json.dumps(build.log()).encode()).sha256
            )
        before = runner.benchmark_environment()
        case = pb.TestCase(
            stdin="benchmark-input", args=["--benchmark_format=json", "--benchmark_color=false"]
        )
        package = replace(package, files={**package.files, "benchmark-input": b""})
        run = runner.execute(
            build.binary,
            case,
            package,
            build_profile="microbenchmark",
            cpu_id=before["cpu_id"],
            harness=True,
            cpu_ids=before.get("cpu_ids"),
        )
        if run.failure:
            raise InfrastructureError(
                "kernel benchmark failed: "
                + repository.artifacts.put(json.dumps(run.log()).encode()).sha256
            )
        result, rows = parse_samples(run.stdout, policy)
        after = runner.benchmark_environment(before["cpu_id"])
        require(
            all(
                before.get(key) == after.get(key)
                for key in (*ENVIRONMENT_KEYS, "execution_profile", "cpu_ids", "cpu_quota")
            ),
            "kernel benchmark environment changed",
        )
    report = {
        "schema_version": 1,
        "tool": "google-benchmark",
        "performance_qualification": False,
        "scope": "declared-kernel-only",
        "policy": policy.validate(),
        "task_id": task_id,
        "proposal_id": proposal_id,
        "verification_job": owner.job_id,
        "source": reference(proposal.source.bundle),
        "compiler_image": runner.image,
        "binary": reference(repository.artifacts.put(build.binary)),
        "harness": reference(repository.artifacts.put(driver)),
        "build": reference(repository.artifacts.put(json.dumps(build.log()).encode())),
        "environment_before": before,
        "environment_after": after,
        "samples": rows,
        "cpu_time_resolution_limited": any(row["cpu_time"] == 0 for row in rows),
        "google_benchmark": result,
        "raw_google_benchmark": reference(repository.artifacts.put(run.stdout)),
        "invocation": run.log(),
    }
    return persist(repository, task_id, owner, report)


def run_perf(repository, task_id, proposal_id, runner):
    original, _, owner = verified_source(repository, task_id, proposal_id, runner)
    correctness = json.loads(repository.artifacts.get(owner.correctness_report))
    job = next(
        j
        for j in repository.list_jobs(task_id)
        if j.job_id == owner.job_id and j.attempt == owner.attempt
    )
    comparator = VerificationPlan.parse(repository.artifacts.get(job.verification_plan))
    report = {
        "schema_version": 1,
        "tool": "linux-perf-event-open",
        "performance_qualification": False,
        "scope": "user-space-per-process-including-threads; energy is package-wide",
        "task_id": task_id,
        "proposal_id": proposal_id,
        "samples": [],
    }
    with runner.measurement_window():
        env = runner.benchmark_environment()
        report["environment"] = env
        for role in ("baseline", "candidate"):
            digest = next(
                c["binary_sha256"] for c in correctness["checks"] if c["phase"] == role + "_compile"
            )
            ref = next(
                a
                for a in repository.list_artifacts(task_id)
                if a["sha256"] == digest
                and a["kind"] == "binary"
                and a["owner_id"] == f"{owner.job_id}/{owner.attempt}"
            )
            binary = repository.artifacts.get(
                pb.ArtifactRef(**{k: ref[k] for k in ("key", "sha256", "size_bytes")})
            )
            for case in original.manifest.tests:
                if case.id not in original.manifest.benchmark_test_ids:
                    continue
                invocation = runner.execute(
                    binary,
                    case,
                    original,
                    cpu_id=env["cpu_id"],
                    cpu_ids=env.get("cpu_ids"),
                    perf=True,
                )
                require(
                    not invocation.failure
                    and comparator.matches(invocation.stdout, original.files[case.expected_stdout]),
                    "perf workload failed",
                )
                counters = validate_counters(invocation.perf)
                report["samples"].append(
                    {
                        "role": role,
                        "test_id": case.id,
                        "binary_sha256": hashlib.sha256(binary).hexdigest(),
                        "events": counters,
                        "cpu_seconds": invocation.cpu_seconds,
                        "invocation": invocation.log(),
                    }
                )
    return persist(repository, task_id, owner, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=("kernel", "perf"))
    parser.add_argument("task_id")
    parser.add_argument("proposal_id")
    parser.add_argument("--state", default="var")
    parser.add_argument("--image", required=True)
    parser.add_argument("--target", choices=("linux-x86_64", "linux-aarch64"), required=True)
    parser.add_argument("--execution-profile", choices=("", "parallel-2"), default="")
    parser.add_argument("--kernel", choices=("sum-i64-v1", "dot-i64-v1"), default="sum-i64-v1")
    args = parser.parse_args()
    repo = Repository(args.state)
    runner = DockerRunner(
        args.image,
        target=args.target,
        execution_profile=args.execution_profile,
        watchdog_directory=Path(args.state) / "watchdogs",
    )
    if args.tool == "kernel":
        _, ref = run_microbenchmark(
            repo, args.task_id, args.proposal_id, runner, KernelBenchmark(kernel=args.kernel)
        )
    else:
        _, ref = run_perf(repo, args.task_id, args.proposal_id, runner)
    print(json.dumps({"report": reference(ref), "performance_qualification": False}))


if __name__ == "__main__":
    main()
