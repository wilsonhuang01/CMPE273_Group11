# Automatic submission execution

Start the trusted host coordinator with Python 3.11+ and Docker running:

```sh
./project serve
```

This starts/checks the existing infrastructure and native compiler image, then
runs the coordinator in the foreground. Use `./project serve --no-start` to reuse
a healthy stack. Defaults are `127.0.0.1:50052` for gRPC, `127.0.0.1:18083` for
HTTP health/metrics, and `var/service` for state. `GRPC_PORT`, `HEALTH_PORT`,
`GRPC_HOST`, `HEALTH_HOST` and `STATE_DIRECTORY` override these values. The existing Compose
coordinator remains on port 50051, with its separate persistent volume and
`EXECUTION_ENABLED=false`: its image has no Docker CLI/socket. Submit automatic
work to the host endpoint on 50052. Neither its state nor previously submitted
Compose tasks are moved by this command.

The host coordinator is trusted infrastructure with Docker access. It is not a
public upload endpoint. Packages and source bundles must already exist in its
local artifact store. A separate host submission client can register them and
call the RPC as follows, using the same `STATE_DIRECTORY` as the server:

```sh
.venv/bin/python - <<'PY'
import os
from uuid import uuid4

import grpc

from compiler_optimizer.evaluation.__main__ import native_target
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc
from compiler_optimizer.storage import Repository
from compiler_optimizer.workers.baseline import metadata
from compiler_optimizer.workers.docker_runner import DockerRunner
from compiler_optimizer.workers.package import read_package
from compiler_optimizer.workers.targets import DEFAULT_IMAGES

repo = Repository(os.environ.get("STATE_DIRECTORY", "var/service"))
package = read_package("examples/sum-array")
target = native_target()
runner = DockerRunner(DEFAULT_IMAGES[target], target=target)
request = pb.Submission(
    meta=metadata(str(uuid4())), manifest=package.manifest,
    package=repo.artifacts.put(package.package_bundle),
    source=pb.SourceVersion(version=1, bundle=repo.artifacts.put(package.source_bundle)),
    compiler=runner.compiler(package.manifest.language),
)
with grpc.insecure_channel("127.0.0.1:" + os.environ.get("GRPC_PORT", "50052")) as channel:
    client = rpc.CoordinatorServiceStub(channel)
    receipt = client.SubmitSubmission(request, timeout=10)
    print("task_id:", receipt.meta.task_id)
    print(client.GetOptimizationResult(pb.OptimizationQuery(task_id=request.meta.task_id)))
PY
```

Poll `GetOptimizationResult` with the printed task ID. The result contains task
status and execution stage; a completed selection also contains the audit report
and the selected-source artifact reference. Artifact download remains separate
future work. No optimization CLI command is needed after intake. The tiny example
commonly retains the baseline because it cannot establish reliable performance.

## Durable acceptance and restart behavior

`SubmitSubmission` validates the manifest/package/source binding and commits both
the submission and its execution row in one SQLite transaction before returning a
receipt. An identical message returns another receipt without changing policy,
creating another task or resetting the attempt budget. Existing manual baseline
and cohort commands do not enqueue automatic work.

The execution row freezes the generating roles, verification/benchmark/selection
and recovery policies, total dispatcher attempt cap and retry delay. Defaults
use the three deterministic generating roles and existing mandatory review and
qualification gates. Unsupported starter rules are recorded as generation
failures; they do not bypass selection or invent proposals. Operator-controlled
`workflow_configuration` on `CoordinatorService` can select a smaller cohort or
explicit validated plans before intake. Clients cannot alter policies in the RPC.

The dispatcher polls durable pending rows once per second and holds the existing
local pipeline lock across baseline and cohort work. It preserves the submitted
task ID. After a restart, it confirms cleanup of that task's owned sandbox
containers, reuses a committed successful baseline and resumes the frozen cohort,
agent journal, recovery leases and final audit. Completed/noisy benchmark batches
cannot be resampled. An interrupted baseline starts a new job attempt; it never
overwrites partial attempt artifacts. A crash after final selection only requires
settling the execution row, without recompilation or measurement.

Restart the server with the same state, toolchain image and service dependencies:

```sh
./project serve --no-start
```

Task rejection is terminal. Infrastructure failures wait five seconds before a
dispatcher retry, with three total dispatcher attempts, including interrupted
attempts across restarts. Candidate recovery has its own frozen bounded policy.
Exhaustion marks the task failed and remains visible in the result summary; it
never selects an unqualified candidate. A process loss may cause repeated
uncommitted execution, but committed results remain idempotent. If another local
orchestrator holds the state lock, dispatch waits without consuming an attempt.

The direct runtime command also enables automatic dispatch by default for the
coordinator role. `EXECUTION_ENABLED=false` explicitly selects intake-only mode.
The other service roles remain infrastructure-only. Independent remote workers,
parallel dispatch, model backends and public artifact transport are separate work.

## Validation

```sh
.venv/bin/python -m pytest -q tests/test_dispatch.py tests/test_runtime.py
PIPELINE_INTEGRATION_TESTS=1 .venv/bin/python -m pytest -q tests/test_dispatch_docker.py
```

The unit/service checks cover transactional intake, duplicate requests, invalid
package bindings, baseline failure, bounded infrastructure retry, lock exclusion,
live gRPC automatic completion and restart boundaries. The opt-in integration
test runs an actual coordinator subprocess against Docker, RabbitMQ and Redis,
kills it after committed candidate verification, and verifies resumption under
the original task ID.
