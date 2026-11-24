"""Role-scoped agent RPCs: immutable content, bounded calls and checked evidence."""

import hashlib
import tempfile
from contextlib import contextmanager

import grpc

from compiler_optimizer.agents.architecture import ArchitectureAgent
from compiler_optimizer.agents.correctness import checked_verification
from compiler_optimizer.agents.correctness import deterministic_review as correctness_review
from compiler_optimizer.agents.critic import deterministic_review as critic_review
from compiler_optimizer.agents.generation import SourcePatchAgent
from compiler_optimizer.agents.memory import MemoryAgent
from compiler_optimizer.agents.model import (
    ModelBudgetExceeded,
    ModelOutputError,
    ModelUnavailable,
    call_deadline,
)
from compiler_optimizer.agents.model_backends import model_backend
from compiler_optimizer.agents.performance import PerformanceAgent
from compiler_optimizer.agents.rpc_store import AgentStore
from compiler_optimizer.agents.rules import (
    ArchitectureRuleBackend,
    MemoryRuleBackend,
    PerformanceRuleBackend,
)
from compiler_optimizer.shared.agent_roles import role_spec
from compiler_optimizer.shared.agents_rpc import (
    MAX_AGENT_ARTIFACT_BYTES,
    MAX_AGENT_FRAMES,
    canonical,
    payload,
    sha,
    validate_assessment,
    wire,
)
from compiler_optimizer.shared.benchmarking import BenchmarkPlan
from compiler_optimizer.shared.c_syntax import TransformationAbstained
from compiler_optimizer.shared.contracts import validate_message
from compiler_optimizer.shared.manifest import ContractError, require
from compiler_optimizer.shared.measurement import measurement_work
from compiler_optimizer.shared.reviews import review_context, review_operation, validate_review
from compiler_optimizer.shared.transfer import (
    TRANSFER_CHUNK_BYTES,
    validate_reference,
    validate_task_id,
)
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc
from compiler_optimizer.storage.errors import (
    ArtifactError,
    ConflictError,
    NotFoundError,
    StateError,
)
from compiler_optimizer.workers.package import unbundle

GENERATORS = {
    pb.PERFORMANCE: (PerformanceAgent, PerformanceRuleBackend),
    pb.MEMORY: (MemoryAgent, MemoryRuleBackend),
    pb.ARCHITECTURE: (ArchitectureAgent, ArchitectureRuleBackend),
}


@contextmanager
def agent_errors(context):
    try:
        yield
    except ConflictError:
        context.abort(grpc.StatusCode.ALREADY_EXISTS, "agent operation inputs are frozen")
    except StateError:
        context.abort(grpc.StatusCode.ABORTED, "agent operation is already running")
    except NotFoundError:
        context.abort(grpc.StatusCode.NOT_FOUND, "agent input artifact is not registered")
    except ArtifactError:
        context.abort(grpc.StatusCode.DATA_LOSS, "agent artifact integrity check failed")
    except TimeoutError:
        context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "agent call deadline expired")
    except ModelBudgetExceeded as error:
        context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, str(error)[:200])
    except ModelUnavailable as error:
        context.abort(grpc.StatusCode.UNAVAILABLE, "model unavailable: " + str(error)[:200])
    except ModelOutputError as error:
        context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(error)[:300])
    except TransformationAbstained as error:
        context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(error)[:300])
    except (ContractError, ValueError, TypeError, KeyError, StopIteration):
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid agent request or evidence")
    except OSError:
        context.abort(grpc.StatusCode.UNAVAILABLE, "agent storage unavailable")


def deadline(context):
    remaining = context.time_remaining()
    require(
        remaining is not None and 0 < remaining <= 120, "agent calls require a bounded deadline"
    )
    if not context.is_active():
        raise TimeoutError("agent call cancelled")


class EvidenceView:
    def __init__(self, request, artifacts):
        self.request, self.artifacts = request, artifacts

    def get_task(self, task_id):
        require(task_id == self.request.meta.task_id, "assessment task mismatch")
        return {"submission": self.request.submission, "status": "running"}

    def list_results(self, task_id):
        self.get_task(task_id)
        return [self.request.verification]

    def list_jobs(self, task_id):
        self.get_task(task_id)
        return [self.request.job]


class AgentService(rpc.AgentServiceServicer):
    def __init__(self, root, role, *, backend=None, release="0.1.0", model=None, provider=None):
        role_spec(role)
        require(0 < len(release.encode()) <= 128, "invalid agent release")
        self.store, self.role, self.backend, self.release = AgentStore(root), role, backend, release
        self.backend_name, self.model_check = "deterministic-v1", None
        if backend is None and model is not None and model.enabled:
            self.backend = model_backend(role, model, self.store, provider)
            if self.backend is not None:
                self.backend_name = self.backend.client.backend_name
                self.model_check = self.backend.client.provider.check

    def close(self):
        self.store.close()

    def Ping(self, request, context):
        self.store.check_health()
        return pb.AgentIdentity(
            role=self.role,
            service_id=self.store.service_id,
            backend=self.backend_name,
            release=self.release,
        )

    def PutArtifact(self, frames, context):
        with agent_errors(context):
            deadline(context)
            header, size, digest = None, 0, hashlib.sha256()
            with tempfile.SpooledTemporaryFile(max_size=1024**2) as stream:
                for index, frame in enumerate(frames):
                    deadline(context)
                    require(index < MAX_AGENT_FRAMES, "too many agent artifact frames")
                    if index == 0:
                        require(frame.WhichOneof("frame") == "header", "artifact header required")
                        header = frame.header
                        validate_task_id(header.task_id)
                        validate_reference(header.artifact, MAX_AGENT_ARTIFACT_BYTES)
                    else:
                        require(
                            frame.WhichOneof("frame") == "data"
                            and 0 < len(frame.data) <= TRANSFER_CHUNK_BYTES,
                            "invalid artifact chunk",
                        )
                        size += len(frame.data)
                        require(
                            size <= header.artifact.size_bytes, "artifact exceeds declared size"
                        )
                        stream.write(frame.data)
                        digest.update(frame.data)
                require(
                    header is not None
                    and size == header.artifact.size_bytes
                    and digest.hexdigest() == header.artifact.sha256,
                    "artifact hash/size mismatch",
                )
                stream.seek(0)
                ref = self.store.artifacts.put(stream.read())
                deadline(context)
                self.store.attach(header.task_id, ref)
                return ref

    def GetArtifact(self, request, context):
        with agent_errors(context):
            deadline(context)
            data = self.store.get(request.task_id, request.artifact)
            for offset in range(0, len(data), TRANSFER_CHUNK_BYTES):
                deadline(context)
                yield pb.ArtifactChunk(
                    offset=offset, data=data[offset : offset + TRANSFER_CHUNK_BYTES]
                )

    def _operate(self, method, request, context, operation, response_type, action, *, replay=False):
        with agent_errors(context):
            deadline(context)
            digest = sha(wire(request))
            key = f"{method}:{operation}"
            cached = self.store.begin(
                request.meta.task_id, key, digest, role_spec(self.role).max_iterations
            )
            if cached is not None:
                return response_type.FromString(cached)
            try:
                with call_deadline(max(0.0, context.time_remaining() or 0.0)):
                    response = action()
                provenance = pb.AgentProvenance(
                    schema_version=1,
                    role=self.role,
                    service_id=self.store.service_id,
                    backend="replay-v1" if replay else self.backend_name,
                    release=self.release,
                    request_sha256=digest,
                    response_sha256=sha(payload(response)),
                )
                if isinstance(response, pb.Proposal):
                    response.agent_provenance.CopyFrom(provenance)
                else:
                    response.provenance.CopyFrom(provenance)
                result = wire(response)
                deadline(context)  # Timed-out computations can never publish a late result.
                self.store.finish(request.meta.task_id, key, response=result)
                return response
            except BaseException as error:
                self.store.finish(request.meta.task_id, key, error=type(error).__name__)
                raise

    def GenerateProposal(self, request, context):
        with agent_errors(context):
            validate_message(request)
            require(
                self.role in GENERATORS and request.role == self.role, "incorrect generator role"
            )

            def generate():
                artifacts = self.store.task_artifacts(request.meta.task_id)
                agent_type, default_backend = GENERATORS[self.role]
                backend = self.backend or default_backend()
                if request.HasField("replay_patch"):
                    validate_reference(request.replay_patch, 1024**2)
                    data = artifacts.get(request.replay_patch)

                    class Replay:
                        def generate(self, assignment, sources):
                            return data

                    backend = Replay()
                agent = agent_type(artifacts, backend)
                require(
                    isinstance(agent, (PerformanceAgent, SourcePatchAgent)), "invalid generator"
                )
                proposal, patch = agent.generate(request)
                proposal.structured_patch.CopyFrom(patch)
                return proposal

            return self._operate(
                "generate",
                request,
                context,
                request.proposal_id,
                pb.Proposal,
                generate,
                replay=request.HasField("replay_patch"),
            )

    def _assessment(self, request):
        validate_assessment(request, self.role)
        view = EvidenceView(request, self.store.task_artifacts(request.meta.task_id))
        evidence = checked_verification(view, request.proposal, request.verification)
        return view, evidence

    def Review(self, request, context):
        with agent_errors(context):
            require(self.role in (pb.CORRECTNESS, pb.CRITIC), "incorrect reviewer role")

            def review():
                view, evidence = self._assessment(request)
                bound = review_context(request.proposal, request.verification)
                if self.role == pb.CORRECTNESS:
                    if self.backend is None:
                        value = correctness_review(bound, evidence, request.verification)
                    elif getattr(self.backend, "accepts_change", False):
                        before = unbundle(view.artifacts.get(request.submission.source.bundle))
                        after = unbundle(view.artifacts.get(request.proposal.source.bundle))
                        change = {
                            "rationale": request.proposal.rationale,
                            "before": dict(before),
                            "after": dict(after),
                        }
                        value = self.backend.review(bound, evidence, change)
                    else:
                        value = self.backend.review(bound, evidence)
                else:
                    before = unbundle(view.artifacts.get(request.submission.source.bundle))
                    after = unbundle(view.artifacts.get(request.proposal.source.bundle))
                    require(set(before) == set(after), "critic source membership changed")
                    value = (
                        self.backend.review(bound, before, after, request.proposal.rationale)
                        if self.backend
                        else critic_review(bound, before, after, request.proposal.rationale)
                    )
                require(
                    isinstance(value, dict) and set(value) == {"verdict", "summary", "findings"},
                    "invalid review backend fields",
                )
                checked = validate_review(
                    {**bound, "role": role_spec(self.role).name, **value}, bound, self.role
                )
                return pb.AgentAssessment(record_json=canonical(checked).decode())

            validate_assessment(request, self.role)
            return self._operate(
                "review",
                request,
                context,
                review_operation(request.proposal, request.verification),
                pb.AgentAssessment,
                review,
            )

    def PlanBenchmark(self, request, context):
        with agent_errors(context):
            require(self.role == pb.BENCHMARK, "incorrect planner role")
            validate_assessment(request, self.role)

            def plan():
                view, evidence = self._assessment(request)
                require(
                    request.verification.WhichOneof("outcome") == "success",
                    "benchmark planning requires passing verification",
                )
                policy = BenchmarkPlan.parse(view.artifacts.get(request.benchmark_plan))
                work = measurement_work(
                    request.submission,
                    request.proposal,
                    request.job,
                    request.verification,
                    evidence,
                    policy,
                    request.benchmark_plan,
                )
                return pb.AgentAssessment(record_json=canonical(work).decode())

            return self._operate(
                "plan",
                request,
                context,
                review_operation(request.proposal, request.verification),
                pb.AgentAssessment,
                plan,
            )
