"""Native measurement v2 integration on either supported Docker CPU architecture."""

import os

import pytest
from test_baseline_docker import package

from compiler_optimizer.evaluation.__main__ import native_target
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.baseline import run_baseline
from compiler_optimizer.workers.docker_runner import DockerRunner
from compiler_optimizer.workers.targets import DEFAULT_IMAGES

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("EVALUATION_DOCKER_TESTS") != "1",
        reason="set EVALUATION_DOCKER_TESTS=1 after building native image",
    ),
]


def test_native_launcher_cannot_forge_rss_or_fork(tmp_path):
    target = native_target()
    runner = DockerRunner(
        DEFAULT_IMAGES[target], target=target, watchdog_directory=tmp_path / "watchdogs"
    )
    assert runner.native_execution and runner.measurement_version == 2
    source = r"""
#include <errno.h>
#include <stdio.h>
#include <sys/types.h>
#include <unistd.h>
int main(void) {
    if (geteuid() != 10001 || getegid() != 10001) return 10;
    if (fopen("/work/run/usage.json", "w") != NULL) return 11;
    errno = 0;
    if (fork() != -1 || (errno != EAGAIN && errno != EPERM)) return 12;
    int count, x; long long sum = 0;
    if (scanf("%d", &count) != 1) return 2;
    while (count--) { if (scanf("%d", &x) != 1) return 2; sum += x; }
    printf("%lld\n", sum);
    return 0;
}
"""
    result = run_baseline(package(tmp_path, source), Repository(tmp_path / "state"), runner)
    assert result.WhichOneof("outcome") == "success", result
    assert result.success.benchmarks[0].peak_memory_bytes > 0
