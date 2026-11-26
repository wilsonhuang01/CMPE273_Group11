"""Opt-in sort-library fuzz campaign, supplementary to mandatory verification."""

import argparse
import base64
import json
from dataclasses import replace
from pathlib import Path

from compiler_optimizer.shared.manifest import require
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.docker_runner import DockerRunner, InfrastructureError
from compiler_optimizer.workers.fuzz_policy import FuzzPolicy
from compiler_optimizer.workers.package import bundle
from compiler_optimizer.workers.verify import _snapshot

# Operator-owned adapter: candidates cannot replace the oracle or entry point.
DRIVER = b"""
#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <vector>
extern "C" void co_sort(int *, size_t);
extern "C" int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
    std::vector<int> a;
    for (size_t i=0; i<size; ++i) a.push_back(static_cast<int>(data[i])-128);
    auto expected=a;
    std::sort(expected.begin(),expected.end());
    co_sort(a.data(),a.size());
    if(a!=expected) std::abort();
    return 0;
}
"""


def run_fuzz(
    repository, task_id, proposal_id, runner, *, policy=None, replay=None, _verified_job=None
):
    """Require a successful mandatory gate; fuzzing cannot manufacture verification success."""
    from compiler_optimizer.workers.package import unbundle

    policy = policy or FuzzPolicy()
    policy.validate()
    task, original, _ = _snapshot(repository, task_id)
    require(
        runner.native_execution and original.manifest.language == "cpp17",
        "sort-library-v1 requires native C++17",
    )
    require(
        runner.compiler("cpp17") == task["submission"].compiler, "compiler differs from baseline"
    )
    proposals = [p for p in repository.list_proposals(task_id) if p.proposal_id == proposal_id]
    require(len(proposals) == 1 and not proposals[0].HasField("ir"), "source proposal required")
    proposal = proposals[0]
    passed = [
        r
        for r in repository.list_results(task_id)
        if r.proposal_id == proposal_id and r.WhichOneof("outcome") == "success"
    ]
    require(
        bool(passed) or _verified_job is not None,
        "mandatory reference and sanitizer verification must pass first",
    )
    owner = _verified_job if _verified_job is not None else passed[-1]
    sources = unbundle(repository.artifacts.get(proposal.source.bundle))
    require("co_fuzz_driver.cpp" not in sources, "reserved harness source")
    # Contract: main is guarded by CO_FUZZ; co_sort remains linked in both builds.
    sources["co_fuzz_driver.cpp"] = DRIVER
    manifest = type(original.manifest)()
    manifest.CopyFrom(original.manifest)
    manifest.sources.append("co_fuzz_driver.cpp")
    package = replace(
        original, manifest=manifest, sources=sources, files={**original.files, **sources}
    )
    build = runner.compile(package, build_profile="fuzzer")
    build_ref = repository.artifacts.put(json.dumps(build.log(), sort_keys=True).encode())
    if build.failure or not build.binary:
        raise InfrastructureError("fuzz harness compilation failed; log " + build_ref.sha256)
    if replay is None:
        seeds = tuple(v[: policy.max_length] for v in (b"\x80", b"\xff\x00", bytes(range(16))))
    else:
        require(len(replay) <= policy.max_length, "replay input exceeds maximum length")
        seeds = (replay,)
    invocation = runner.fuzz(build.binary, policy, seeds, replay=replay is not None)
    if not invocation.stdout and not invocation.failure:
        raise InfrastructureError("fuzzer returned no artifact envelope")
    envelope = (
        json.loads(invocation.stdout)
        if invocation.stdout
        else {
            "schema_version": 1,
            "policy": policy.validate(),
            "artifacts": {},
            "run": invocation.log(),
            "minimization": None,
            "failure_inputs": [],
            "replay": replay is not None,
        }
    )
    envelope["artifacts"]["coverage.log"] = base64.b64encode(invocation.stderr).decode()
    artifacts = {}
    for name, data in envelope.pop("artifacts").items():
        blob = repository.artifacts.put(base64.b64decode(data, validate=True))
        repository.attach_artifact(
            task_id, blob, kind="correctness", job_id=owner.job_id, attempt=owner.attempt
        )
        artifacts[name] = {"key": blob.key, "sha256": blob.sha256, "size_bytes": blob.size_bytes}
    report = {
        **envelope,
        "task_id": task_id,
        "proposal_id": proposal_id,
        "source_sha256": proposal.source.bundle.sha256,
        "compiler_image": runner.image,
        "contract": "sort-library-v1",
        "harness_sha256": repository.artifacts.put(DRIVER).sha256,
        "seed_bundle_sha256": repository.artifacts.put(
            bundle({str(i): v for i, v in enumerate(seeds)})
        ).sha256,
        "binary_sha256": repository.artifacts.put(build.binary).sha256,
        "build_log_sha256": build_ref.sha256,
        "artifacts": artifacts,
        "invocation": invocation.log(),
        "status": "rejected" if invocation.failure else "bounded-no-finding",
    }
    reference = repository.artifacts.put(json.dumps(report, sort_keys=True).encode())
    repository.attach_artifact(
        task_id, reference, kind="correctness", job_id=owner.job_id, attempt=owner.attempt
    )
    return report, reference


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id")
    parser.add_argument("proposal_id")
    parser.add_argument("--state", default="var")
    parser.add_argument("--image", default="cmpe273-arm:18")
    parser.add_argument("--target", default="linux-aarch64")
    parser.add_argument("--seconds", type=int, default=5)
    parser.add_argument("--replay", help="Raw persisted failure input")
    args = parser.parse_args()
    repo = Repository(args.state)
    report, reference = run_fuzz(
        repo,
        args.task_id,
        args.proposal_id,
        DockerRunner(
            args.image, target=args.target, watchdog_directory=Path(args.state) / "watchdogs"
        ),
        policy=FuzzPolicy(seconds=args.seconds),
        replay=None if args.replay is None else Path(args.replay).read_bytes(),
    )
    print(json.dumps({"status": report["status"], "report_sha256": reference.sha256}))
    return int(report["status"] == "rejected")


if __name__ == "__main__":
    raise SystemExit(main())
