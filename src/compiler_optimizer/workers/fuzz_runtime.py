"""Trusted libFuzzer orchestration inside one disposable Docker container."""

import base64
import json
from pathlib import Path

from fuzz_policy import FuzzPolicy


def fuzz_main(request, execute):
    policy = FuzzPolicy(**request["policy"])
    policy.validate()
    binary = base64.b64decode(request["binary"], validate=True)
    if not 0 < len(binary) <= 16 * 1024**2:
        raise ValueError("invalid fuzz executable")
    program = Path("/work/run/program")
    program.write_bytes(binary)
    program.chmod(0o555)
    corpus = Path("/tmp/corpus")
    corpus.mkdir()
    corpus.chmod(0o777)
    seeds = request["corpus"]
    if not 0 < len(seeds) <= 64:
        raise ValueError("invalid seed count")
    for i, value in enumerate(seeds):
        data = base64.b64decode(value, validate=True)
        if not 0 <= len(data) <= policy.max_length:
            raise ValueError("invalid seed length")
        path = corpus / str(i)
        path.write_bytes(data)
        path.chmod(0o444)
    common = [
        str(program),
        f"-seed={policy.seed}",
        "-timeout=1",
        "-rss_limit_mb=384",
        "-malloc_limit_mb=128",
        f"-max_len={policy.max_length}",
        "-artifact_prefix=/tmp/",
        "-print_final_stats=1",
    ]
    args = [*common, f"-runs={policy.runs}", f"-max_total_time={policy.seconds}", str(corpus)]
    if request["replay"]:
        args = [*common, "-runs=1", str(corpus / "0")]
    report = execute(
        args, b"", policy.seconds + 3, 262144, compile_mode=False, build_profile="fuzzer", fuzz=True
    )
    failures = sorted(
        p
        for p in Path("/tmp").iterdir()
        if p.name.startswith(("crash-", "timeout-", "oom-")) and p.is_file()
    )
    minimization = None
    if failures and not request["replay"]:
        minimized = "/tmp/minimized"
        mini = execute(
            [
                *common,
                "-minimize_crash=1",
                f"-max_total_time={policy.minimize_seconds}",
                "-exact_artifact_path=" + minimized,
                str(failures[0]),
            ],
            b"",
            policy.minimize_seconds + 2,
            65536,
            compile_mode=False,
            build_profile="fuzzer",
            fuzz=True,
        )
        minimization = mini
    # Never follow a submission-created symlink or read unbounded files.
    artifacts = {}
    paths = [
        *failures,
        Path("/tmp/minimized"),
        Path("/tmp/coverage.profraw"),
        *sorted(corpus.iterdir()),
    ]
    total = 0
    for path in paths[:130]:
        if path.is_symlink() or not path.is_file():
            continue
        size = path.stat().st_size
        if size > 1048576 or total + size > 1048576:
            continue
        total += size
        artifacts[str(path.relative_to("/tmp"))] = base64.b64encode(path.read_bytes()).decode()
    summary = {
        "schema_version": 1,
        "policy": request["policy"],
        "artifacts": artifacts,
        "run": dict(report),
        "minimization": minimization,
        "failure_inputs": [p.name for p in failures],
        "replay": request["replay"],
    }
    report["stdout"] = base64.b64encode(json.dumps(summary, sort_keys=True).encode()).decode()
    print(json.dumps(report))
