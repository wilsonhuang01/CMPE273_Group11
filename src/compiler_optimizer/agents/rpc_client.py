"""Coordinator-side agent clients; transport results never replace acceptance validation."""

import json
import time

import grpc
from google.protobuf.json_format import MessageToDict

from compiler_optimizer.client import client_channel
from compiler_optimizer.shared.agent_roles import ROLES, role_spec
from compiler_optimizer.shared.agents_rpc import (
    AGENT_NAMES,
    AGENT_PORTS,
    MAX_AGENT_ARTIFACT_BYTES,
    parse_record,
    sha,
    valid_backend,
    validate_provenance,
    wire,
)
from compiler_optimizer.shared.c_syntax import TransformationAbstained
from compiler_optimizer.shared.contracts import validate_message, validate_proposal_for_request
from compiler_optimizer.shared.manifest import ContractError, require
from compiler_optimizer.shared.patches import apply_patch
from compiler_optimizer.shared.reviews import review_context, review_operation, validate_review
from compiler_optimizer.shared.transfer import (
    TRANSFER_CHUNK_BYTES,
    content_reference,
    validate_reference,
)
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc
from compiler_optimizer.storage.pipeline import reference
from compiler_optimizer.workers.package import bundle, unbundle


class AgentUnavailable(ContractError):
    """A failed/deadline-limited agent call consumes its coordinator journal attempt."""


class AgentClient:
    def __init__(self, repository, settings):
        self.repository, self.settings = repository, settings
        self.endpoints = (
            json.loads(settings.agent_endpoints)
            if settings.agent_endpoints
            else {name: f"127.0.0.1:{port}" for name, port in AGENT_PORTS.items()}
        )
        require(
            isinstance(self.endpoints, dict) and set(self.endpoints) == set(AGENT_NAMES),
            "agent endpoints must configure exactly six roles",
        )

    def check_health(self):
        for role, spec in ROLES.items():
            ca = (
                self.settings.agent_ca_directory / (spec.name + ".crt")
                if self.settings.agent_ca_directory
                else None
            )
            with client_channel(
                self.endpoints[spec.name],
                tls=self.settings.agent_tls,
                ca=ca,
                token=self.settings.agent_token,
            ) as channel:
                metadata = (
                    (("authorization", "Bearer " + self.settings.agent_token),)
                    if self.settings.agent_token
                    else ()
                )
                identity = rpc.AgentServiceStub(channel).Ping(
                    pb.AgentPing(), timeout=2, metadata=metadata
                )
                require(
                    identity.role == role
                    and identity.service_id
                    and identity.backend != "replay-v1"
                    and valid_backend(identity.backend),
                    "agent readiness identity mismatch",
                )

    def _invoke(self, role, method, request, refs=(), output_refs=None, validate=None):
        repository, settings = self.repository, self.settings
        name = role_spec(role).name
        operation = (
            request.proposal_id
            if isinstance(request, pb.OptimizationRequest)
            else review_operation(request.proposal, request.verification)
        )
        digest = sha(wire(request))
        end = time.monotonic() + settings.agent_timeout_seconds
        debate = repository.debate_run(request.meta.task_id)
        if debate is not None:
            end = min(end, time.monotonic() + max(0, debate["deadline_ms"] / 1000 - time.time()))

        def remaining():
            seconds = end - time.monotonic()
            if seconds <= 0:
                raise AgentUnavailable("agent deadline expired")
            return seconds

        receipt = {
            "schema_version": 1,
            "role": name,
            "operation": operation,
            "request_sha256": digest,
            "status": "failed",
            "request": reference(repository.artifacts.put(wire(request))),
        }
        try:
            ca = (
                settings.agent_ca_directory / (name + ".crt")
                if settings.agent_ca_directory
                else None
            )
            with client_channel(
                self.endpoints[name], tls=settings.agent_tls, ca=ca, token=settings.agent_token
            ) as channel:
                stub = rpc.AgentServiceStub(channel)
                metadata = (
                    (("authorization", "Bearer " + settings.agent_token),)
                    if settings.agent_token
                    else ()
                )
                for ref in {r.key: r for r in refs}.values():
                    validate_reference(ref, MAX_AGENT_ARTIFACT_BYTES)
                    data = repository.artifacts.get(ref)

                    def frames(ref=ref, data=data):
                        yield pb.AgentArtifactFrame(
                            header=pb.ArtifactRequest(task_id=request.meta.task_id, artifact=ref)
                        )
                        for offset in range(0, len(data), TRANSFER_CHUNK_BYTES):
                            remaining()
                            yield pb.AgentArtifactFrame(
                                data=data[offset : offset + TRANSFER_CHUNK_BYTES]
                            )

                    stored = stub.PutArtifact(frames(), timeout=remaining(), metadata=metadata)
                    require(stored == ref, "agent changed uploaded artifact identity")
                response = getattr(stub, method)(request, timeout=remaining(), metadata=metadata)
                receipt["response"] = reference(repository.artifacts.put(wire(response)))
                provenance = (
                    response.agent_provenance
                    if isinstance(response, pb.Proposal)
                    else response.provenance
                )
                validate_provenance(provenance, request, response, role)
                if output_refs is not None:
                    for ref in output_refs(response):
                        validate_reference(ref, MAX_AGENT_ARTIFACT_BYTES)
                        parts, size = [], 0
                        for chunk in stub.GetArtifact(
                            pb.ArtifactRequest(task_id=request.meta.task_id, artifact=ref),
                            timeout=remaining(),
                            metadata=metadata,
                        ):
                            require(
                                chunk.offset == size
                                and 0 < len(chunk.data) <= TRANSFER_CHUNK_BYTES,
                                "invalid agent artifact stream",
                            )
                            size += len(chunk.data)
                            require(size <= ref.size_bytes, "agent artifact exceeds declared size")
                            parts.append(chunk.data)
                        data = b"".join(parts)
                        require(content_reference(data) == ref, "agent artifact hash mismatch")
                        require(
                            repository.artifacts.put(data) == ref,
                            "agent artifact identity mismatch",
                        )
                if validate is not None:
                    validate(response)
                remaining()
                receipt.update(
                    status="completed",
                    provenance=MessageToDict(provenance, preserving_proto_field_name=True),
                )
                return response
        except grpc.RpcError as error:
            receipt["error"] = error.code().name
            if error.code() == grpc.StatusCode.FAILED_PRECONDITION and (
                error.details() or ""
            ).startswith("rule backend abstained:"):
                raise TransformationAbstained(error.details()[:300]) from None
            raise AgentUnavailable("agent RPC failed: " + error.code().name) from None
        except (ValueError, OSError) as error:
            receipt["error"] = type(error).__name__
            raise
        finally:
            repository.record_agent_rpc(request.meta.task_id, role, operation, digest, receipt)

    def generator(self, role, patch_ref=None):
        client = self

        class Generator:
            def generate(self, request):
                assignment = pb.OptimizationRequest()
                assignment.CopyFrom(request)
                if patch_ref is not None:
                    assignment.replay_patch.CopyFrom(patch_ref)
                refs = [request.source.bundle, *([patch_ref] if patch_ref is not None else [])]

                def checked(proposal):
                    validate_message(proposal)
                    validate_proposal_for_request(proposal, request)
                    require(
                        proposal.HasField("structured_patch")
                        and proposal.HasField("agent_provenance"),
                        "agent generation evidence missing",
                    )
                    before = unbundle(client.repository.artifacts.get(request.source.bundle))
                    patch, after = apply_patch(
                        client.repository.artifacts.get(proposal.structured_patch),
                        before,
                        request.source.bundle.sha256,
                        compiler=request.compiler,
                    )
                    require(
                        client.repository.artifacts.put(bundle(after)) == proposal.source.bundle
                        and patch["rationale"] == proposal.rationale
                        and patch["transformation"] == proposal.transformation,
                        "agent proposal/patch mismatch",
                    )

                proposal = client._invoke(
                    role,
                    "GenerateProposal",
                    assignment,
                    refs,
                    output_refs=lambda p: [p.structured_patch],
                    validate=checked,
                )
                return proposal, proposal.structured_patch

        return Generator()

    def _assessment(self, role, proposal, result, plan_ref=None):
        task_id = proposal.meta.task_id
        job = next(
            j
            for j in self.repository.list_jobs(task_id)
            if j.job_id == result.job_id and j.attempt == result.attempt
        )
        submission = self.repository.get_task(task_id)["submission"]
        request = pb.AgentAssessmentRequest(
            meta=proposal.meta,
            role=role,
            submission=submission,
            proposal=proposal,
            job=job,
            verification=result,
        )
        refs = [
            submission.source.bundle,
            proposal.source.bundle,
            job.verification_plan,
            job.verification_inputs,
            result.correctness_report,
            result.log,
        ]
        if plan_ref is not None:
            request.benchmark_plan.CopyFrom(plan_ref)
            refs.append(plan_ref)
        return request, refs

    def reviewer(self, role):
        client = self

        class Backend:
            def review(self, context, *inputs):
                task_id = context["task_id"]
                proposal = next(
                    p
                    for p in client.repository.list_proposals(task_id)
                    if p.proposal_id == context["proposal_id"]
                )
                result = next(
                    r
                    for r in client.repository.list_results(task_id)
                    if r.job_id == context["verification_job_id"]
                    and r.attempt == context["verification_attempt"]
                )
                request, refs = client._assessment(role, proposal, result)

                def checked(response):
                    validate_review(
                        parse_record(response.record_json), review_context(proposal, result), role
                    )

                response = client._invoke(role, "Review", request, refs, validate=checked)
                record = parse_record(response.record_json)
                return {key: record[key] for key in ("verdict", "summary", "findings")}

        return Backend()

    def plan(self, proposal, result, plan_ref, expected):
        request, refs = self._assessment(pb.BENCHMARK, proposal, result, plan_ref)

        def checked(response):
            require(
                parse_record(response.record_json) == expected,
                "agent benchmark work differs from frozen policy",
            )

        response = self._invoke(pb.BENCHMARK, "PlanBenchmark", request, refs, validate=checked)
        return parse_record(response.record_json)


def configured_client(repository, settings=None):
    if settings is None:
        from compiler_optimizer.runtime.config import Settings

        settings = Settings.from_env("coordinator")
    return AgentClient(repository, settings) if settings.agent_backend == "rpc" else None
