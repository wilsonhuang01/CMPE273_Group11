"""Native OpenMP correctness, fair measurements, Google Benchmark, and perf probing."""

import json
import os
from pathlib import Path

import pytest

from compiler_optimizer.agents.benchmark import BenchmarkAgent
from compiler_optimizer.agents.correctness import CorrectnessAgent
from compiler_optimizer.agents.critic import CriticAgent
from compiler_optimizer.shared.benchmarking import BenchmarkPlan
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.baseline import run_baseline
from compiler_optimizer.workers.benchmark import run_benchmark
from compiler_optimizer.workers.docker_runner import DockerRunner
from compiler_optimizer.workers.measurement_tools import (
    KernelBenchmark,
    run_microbenchmark,
    run_perf,
)
from compiler_optimizer.workers.package import read_package
from compiler_optimizer.workers.verify import register_candidate, run_verification

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("BASELINE_DOCKER_TESTS") != "1", reason="requires native Docker"
    ),
]
EXAMPLE = Path(__file__).resolve().parents[1] / "examples/parallel-sum"


@pytest.mark.parametrize("execution_profile", ["", "parallel-2"])
def test_correctness_same_resource_benchmark_and_supplementary_tools(tmp_path, execution_profile):
    runner = DockerRunner(
        os.environ.get("COMPILER_IMAGE", "cmpe273-action-m:18"),
        target=os.environ.get("COMPILER_TARGET", "linux-aarch64"),
        execution_profile=execution_profile,
        watchdog_directory=tmp_path / "watchdogs",
    )
    repo = Repository(tmp_path / "state")
    original = read_package(EXAMPLE)
    baseline = run_baseline(original, repo, runner)
    assert baseline.HasField("success"), baseline.failure
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    # Require an actual two-worker team, not just acceptance of an ignored pragma.
    code = original.sources["main.cpp"].replace(
        b"    int64_t result = 0;",
        b"""    int team = 0;
#pragma omp parallel num_threads(2) reduction(+:team)
    team += 1;
    if (team != 2) std::abort();
    int64_t result = 0;""",
    )
    if not execution_profile:
        code = original.sources["main.cpp"] + b"\n// single-thread proposal\n"
    (candidate / "main.cpp").write_bytes(code)
    task = baseline.meta.task_id
    proposal_id = register_candidate(repo, task, candidate)
    verified = run_verification(repo, task, proposal_id, runner)
    assert verified.HasField("success"), verified.failure
    proposal = next(p for p in repo.list_proposals(task) if p.proposal_id == proposal_id)
    for agent in (CorrectnessAgent(repo), CriticAgent(repo)):
        agent.review(proposal, verified)
    BenchmarkAgent(repo).organize(
        proposal, verified, repo.artifacts.put(BenchmarkPlan(warmups=0, repetitions=7).encode())
    )
    report = run_benchmark(
        repo, task, verified.job_id, runner, BenchmarkPlan(warmups=0, repetitions=7)
    )
    raw = json.loads(repo.artifacts.get(pb.ArtifactRef(**report["raw"])))
    assert raw["status"] == "completed", raw.get("error")
    assert not report["analysis"]["qualified"]  # The tiny example cannot establish a speedup.
    for pair in raw["cases"]["fixed"]["pairs"]:
        a, b = (pair["samples"][role] for role in ("baseline", "candidate"))
        if execution_profile:
            assert a["cpu_ids"] == b["cpu_ids"] == raw["environment_before"]["cpu_ids"]
            assert a["threads"] == b["threads"] == 2
            assert a["cpu_quota"] == b["cpu_quota"] == 2
            assert a["cpu_seconds"] > 0 and b["cpu_seconds"] > 0
        else:
            assert a["cpu_id"] == b["cpu_id"] == raw["environment_before"]["cpu_id"]
            assert "execution_profile" not in a
    micro, ref = run_microbenchmark(repo, task, proposal_id, runner, KernelBenchmark(repetitions=3))
    assert len(micro["samples"]) == 3
    assert micro["performance_qualification"] is False
    assert repo.artifacts.get(ref)
    perf, ref = run_perf(repo, task, proposal_id, runner)
    assert len(perf["samples"]) == 2
    assert perf["performance_qualification"] is False
    for sample in perf["samples"]:
        for event in sample["events"].values():
            assert event["status"] in ("available", "unavailable")
            if event["status"] == "unavailable":
                assert event["count"] is None
    assert repo.artifacts.get(ref)


@pytest.mark.parametrize("execution_profile,workers", [("", 0), ("parallel-2", 1)])
def test_thread_ceiling_and_no_child_processes(tmp_path, execution_profile, workers):
    from dataclasses import replace

    runner = DockerRunner(
        os.environ.get("COMPILER_IMAGE", "cmpe273-action-m:18"),
        target=os.environ.get("COMPILER_TARGET", "linux-aarch64"),
        execution_profile=execution_profile,
        watchdog_directory=tmp_path / "watchdogs",
    )
    package = read_package(EXAMPLE)
    source = b"""
#include <atomic>
#include <iostream>
#include <pthread.h>
#include <sched.h>
#include <sys/wait.h>
#include <unistd.h>
std::atomic<bool> done(false);
void* work(void*) { while(!done.load()) sched_yield(); return nullptr; }
int main() {
    pid_t pid=fork();
    if(pid==0) _exit(99);
    if(pid>0) waitpid(pid,nullptr,0);
    pthread_t ids[8]; int count=0;
    while(count<8 && pthread_create(&ids[count],nullptr,work,nullptr)==0) ++count;
    done=true;
    for(int i=0;i<count;++i) pthread_join(ids[i],nullptr);
    std::cout << (pid<0) << " " << count << "\\n";
}
"""
    package = replace(package, sources={"main.cpp": source})
    build = runner.compile(package)
    assert not build.failure, build.log()
    run = runner.execute(build.binary, package.manifest.tests[0], package)
    assert not run.failure, run.log()
    assert run.stdout == f"1 {workers}\n".encode()


def test_parallel_loop_hits_external_runtime_bound(tmp_path):
    from dataclasses import replace

    runner = DockerRunner(
        os.environ.get("COMPILER_IMAGE", "cmpe273-action-m:18"),
        target=os.environ.get("COMPILER_TARGET", "linux-aarch64"),
        execution_profile="parallel-2",
        watchdog_directory=tmp_path / "watchdogs",
    )
    package = read_package(EXAMPLE)
    package.manifest.limits.run_wall_seconds = 1
    package = replace(package, sources={"main.cpp": b'int main(){for(;;){asm volatile("");}}'})
    build = runner.compile(package)
    assert not build.failure, build.log()
    run = runner.execute(build.binary, package.manifest.tests[0], package)
    assert run.failure == "timeout", run.log()
    assert run.sandbox["watchdog"]["state"] == "removed"


@pytest.mark.parametrize("available", [False, True])
def test_trusted_perf_reader_and_fd_transfer_with_simulated_kernel_events(tmp_path, available):
    """Exercise the C reader even when this VM denies real perf events.

    This fixture replaces only event-open with pipe-backed test counters; it does
    not claim actual PMU availability or produce repository performance evidence.
    """
    from dataclasses import replace

    from compiler_optimizer.workers.perf import validate_counters

    runner = DockerRunner(
        os.environ.get("COMPILER_IMAGE", "cmpe273-action-m:18"),
        target=os.environ.get("COMPILER_TARGET", "linux-aarch64"),
        watchdog_directory=tmp_path / "watchdogs",
    )
    header = (EXAMPLE.parents[1] / "docker/perf.h").read_text()
    start = header.index("counters[i].fd = syscall(")
    end = header.index(";", start)
    header = header[:start] + "counters[i].fd = fixture_open()" + header[end:]
    source = rb"""
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <sys/socket.h>
#include <unistd.h>
int fixture_open() {
#if AVAILABLE
    int ends[2];
    if(pipe(ends)) return -1;
    uint64_t values[3]={42,1000,1000};
    write(ends[1],values,sizeof(values)); close(ends[1]);
    return ends[0];
#else
    errno=EPERM; return -1;
#endif
}
#include "perf.h"
int main() {
    int ends[2]; if(socketpair(AF_UNIX,SOCK_DGRAM,0,ends)) return 1;
    open_counters(0);
    if(!send_counters(ends[0]) || !receive_counters(ends[1])) return 2;
    dprintf(STDOUT_FILENO,"{\"fixture\":true");
    write_counters(STDOUT_FILENO);
    dprintf(STDOUT_FILENO,"}\n");
}
"""
    source = b"#define AVAILABLE " + (b"1" if available else b"0") + b"\n" + source
    package = read_package(EXAMPLE)
    package = replace(package, sources={"main.cpp": source, "perf.h": header.encode()})
    build = runner.compile(package)
    assert not build.failure, build.log()
    run = runner.execute(build.binary, package.manifest.tests[0], package)
    assert not run.failure, run.log()
    counters = validate_counters(json.loads(run.stdout)["perf"])
    for name, event in counters.items():
        assert event["status"] == (
            "available" if available and name != "energy-pkg" else "unavailable"
        )


def test_dot_kernel_google_benchmark_adapter(tmp_path):
    from compiler_optimizer.workers.package import package_from_files

    runner = DockerRunner(
        os.environ.get("COMPILER_IMAGE", "cmpe273-action-m:18"),
        target=os.environ.get("COMPILER_TARGET", "linux-aarch64"),
        watchdog_directory=tmp_path / "watchdogs",
    )
    files = read_package(EXAMPLE).files
    code = rb"""
#include <cstdint>
#include <iostream>
#include <vector>
extern "C" int64_t co_dot(const int64_t* a,const int64_t* b,size_t n) {
    int64_t out=0; for(size_t i=0;i<n;++i) out+=a[i]*b[i]; return out;
}
#ifndef CO_BENCHMARK
int main() {
    size_t n; std::cin>>n; std::vector<int64_t> a(n);
    for(auto& x:a) std::cin>>x;
    std::cout<<co_dot(a.data(),a.data(),n)<<"\n";
}
#endif
"""
    package = package_from_files({**files, "main.cpp": code, "fixed.out": b"204\n"})
    repo = Repository(tmp_path / "state")
    baseline = run_baseline(package, repo, runner)
    assert baseline.HasField("success"), baseline.failure
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "main.cpp").write_bytes(code + b"\n// proposal\n")
    task = baseline.meta.task_id
    proposal = register_candidate(repo, task, candidate)
    result = run_verification(repo, task, proposal, runner)
    assert result.HasField("success"), result.failure
    report, _ = run_microbenchmark(
        repo, task, proposal, runner, KernelBenchmark(kernel="dot-i64-v1", repetitions=3)
    )
    assert len(report["samples"]) == 3
    assert not report["performance_qualification"]
    assert repo.list_benchmarks(task) == []


@pytest.mark.parametrize("instruction", ["x32", "compat32"])
def test_parallel_child_rejects_alternate_x86_syscall_abis(tmp_path, instruction):
    from dataclasses import replace

    if os.environ.get("COMPILER_TARGET", "linux-aarch64") != "linux-x86_64":
        pytest.skip("x86 syscall ABI hardening requires a native x86-64 worker")
    runner = DockerRunner(
        os.environ.get("COMPILER_IMAGE", "cmpe273-action-m:18"),
        target="linux-x86_64",
        execution_profile="parallel-2",
        watchdog_directory=tmp_path / "watchdogs",
    )
    # Both getpid variants are harmless themselves, but must not reach the kernel
    # through an ABI that could bypass native fork/clone filtering.
    code = (
        b'int main(){long r; asm volatile("syscall":"=a"(r):"a"(0x40000027L):'
        b'"rcx","r11","memory"); return 0;}'
        if instruction == "x32"
        else b'int main(){long r; asm volatile("int $0x80":"=a"(r):"a"(20):"memory"); return 0;}'
    )
    package = replace(read_package(EXAMPLE), sources={"main.cpp": code})
    build = runner.compile(package)
    assert not build.failure, build.log()
    run = runner.execute(build.binary, package.manifest.tests[0], package)
    assert run.failure == "signal" and run.returncode == -31, run.log()
