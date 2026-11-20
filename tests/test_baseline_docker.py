"""Opt-in real Linux Clang execution; submitted code always stays in Docker."""

import json
import os
import shutil
from pathlib import Path

import pytest

from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.baseline import run_baseline
from compiler_optimizer.workers.docker_runner import DockerRunner
from compiler_optimizer.workers.package import read_package

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("BASELINE_DOCKER_TESTS") != "1",
        reason="set BASELINE_DOCKER_TESTS=1 after building compiler image",
    ),
]


@pytest.fixture(scope="module")
def runner(tmp_path_factory):
    return DockerRunner(
        os.environ.get("COMPILER_IMAGE", "cmpe273-clang:18"),
        watchdog_directory=tmp_path_factory.mktemp("watchdogs"),
    )


def package(tmp_path, source=None, language="c17"):
    directory = tmp_path / "submission"
    shutil.copytree(Path(__file__).resolve().parents[1] / "examples/sum-array", directory)
    manifest = json.loads((directory / "submission.json").read_text())
    manifest["limits"]["run_wall_seconds"] = 1
    manifest["limits"]["run_memory_mib"] = 64
    manifest["limits"]["output_kib"] = 1
    if source is not None:
        filename = "main.c" if language == "c17" else "main.cpp"
        (directory / "main.c").unlink()
        (directory / filename).write_text(source)
        manifest.update(language=language, sources=[filename])
    (directory / "submission.json").write_text(json.dumps(manifest))
    return read_package(directory)


def test_real_c_baseline_and_artifacts(tmp_path, runner):
    repo = Repository(tmp_path / "state")
    result = run_baseline(package(tmp_path), repo, runner)
    assert result.WhichOneof("outcome") == "success", result
    assert result.compiler.version.startswith("18.")
    assert result.compiler.image_digest == runner.image
    assert result.success.benchmarks[0].peak_memory_bytes > 0
    assert repo.get_task(result.meta.task_id)["status"] == "running"


def test_real_cpp17_baseline(tmp_path, runner):
    source = """#include <iostream>
int main() { int n, x; long long total=0; std::cin >> n;
while(n--) { std::cin >> x; total+=x; } std::cout << total << std::endl; }
"""
    result = run_baseline(
        package(tmp_path, source, "cpp17"), Repository(tmp_path / "state"), runner
    )
    assert result.WhichOneof("outcome") == "success", result
    assert result.compiler.executable == "clang++"


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("this is not valid C;", pb.COMPILATION_ERROR),
        ('#include <stdio.h>\nint main(void) { puts("wrong"); }', pb.OUTPUT_MISMATCH),
        ("int main(void) { return 7; }", pb.NONZERO_EXIT),
        ("#include <signal.h>\nint main(void) { raise(SIGSEGV); }", pb.SIGNAL_TERMINATION),
        ("int main(void) { volatile unsigned x=0; for(;;) { x++; } }", pb.TIMEOUT),
        ('#include <stdio.h>\nint main(void) { for(;;) puts("excess output"); }', pb.OUTPUT_LIMIT),
        (
            "#include <stdlib.h>\nint main(void) { volatile char *p=malloc(256UL*1024*1024); "
            "if(!p) return 2; for(unsigned long i=0;i<256UL*1024*1024;i+=4096) p[i]=1; "
            "return p[0]; }",
            pb.MEMORY_LIMIT,
        ),
    ],
)
def test_real_rejection_paths(tmp_path, runner, source, reason):
    repo = Repository(tmp_path / "state")
    result = run_baseline(package(tmp_path, source), repo, runner)
    assert result.WhichOneof("outcome") == "failure", result
    assert result.failure.reason == reason, result
    assert repo.get_task(result.meta.task_id)["status"] == "failed"


def test_real_execution_syscall_identity_and_filesystem_isolation(tmp_path, runner):
    source = r"""#define _GNU_SOURCE
#include <stdio.h>
#include <unistd.h>
#include <errno.h>
#include <fcntl.h>
#include <sched.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/prctl.h>
int main(void) {
    if (geteuid()!=10001 || getegid()!=10001 || getgroups(0,0)!=0) return 10;
    FILE *status=fopen("/proc/self/status","r"); if(!status) return 11;
    char line[256]; int nnp=-1, seccomp=-1; unsigned long long caps=1;
    while(fgets(line,sizeof(line),status)) {
        sscanf(line,"NoNewPrivs: %d",&nnp); sscanf(line,"Seccomp: %d",&seccomp);
        sscanf(line,"CapEff: %llx",&caps);
    }
    fclose(status); if(nnp!=1 || seccomp!=2 || caps!=0) return 11;
    errno=0; if (socket(AF_INET,SOCK_STREAM,0)!=-1 || errno!=EPERM) return 12;
    errno=0; if (fork()!=-1 || (errno!=EPERM && errno!=EAGAIN)) return 13;
    errno=0; if (unshare(CLONE_NEWUSER)!=-1 || errno!=EPERM) return 14;
    errno=0; if (kill(1,SIGSTOP)!=-1 || errno!=EPERM) return 15;
    if (open("/etc/sandbox-escape",O_WRONLY|O_CREAT,0600)!=-1) return 16;
    if (open("/work/run/program",O_WRONLY)!=-1) return 17;
    if (open("/proc/1/fd/1",O_WRONLY)!=-1) return 18;
    FILE *f=fopen("/tmp/local-temp","w"); if(!f) return 19;
    fputs("temporary",f); fclose(f); unlink("/tmp/local-temp");
    int n,x; long long sum=0; if(scanf("%d",&n)!=1) return 20;
    while(n--) { if(scanf("%d",&x)!=1) return 21; sum+=x; }
    printf("%lld\n",sum); return 0;
}
"""
    result = run_baseline(package(tmp_path, source), Repository(tmp_path / "state"), runner)
    assert result.WhichOneof("outcome") == "success", result
