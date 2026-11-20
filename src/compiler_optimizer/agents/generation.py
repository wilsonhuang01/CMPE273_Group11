"""Shared source-only proposal construction for candidate-generating roles."""

import base64
import json

from compiler_optimizer.shared.contracts import validate_message, validate_proposal_for_request
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.messaging import decode_message, encode_message
from compiler_optimizer.shared.patches import apply_patch
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage.pipeline import reference
from compiler_optimizer.workers.baseline import metadata
from compiler_optimizer.workers.package import bundle, unbundle


class SourcePatchAgent:
    role = pb.AGENT_ROLE_UNSPECIFIED

    def __init__(self, artifacts, backend):
        self.artifacts, self.backend = artifacts, backend

    def generate(self, request):
        validate_message(request)
        require(request.role == self.role, "agent requires its assigned generation role")
        sources = unbundle(self.artifacts.get(request.source.bundle))
        patch, changed = apply_patch(
            self.backend.generate(request, dict(sources)), sources, request.source.bundle.sha256
        )
        proposal = pb.Proposal(
            meta=metadata(request.meta.task_id),
            proposal_id=request.proposal_id,
            role=self.role,
            source=pb.SourceVersion(
                version=request.source.version + 1,
                bundle=self.artifacts.put(bundle(changed)),
                parent_source_sha256=request.source.bundle.sha256,
            ),
            rationale=patch["rationale"],
            transformation=patch["transformation"],
            compiler=request.compiler,
        )
        proposal.meta.created_at_unix_ms = max(
            proposal.meta.created_at_unix_ms, request.meta.created_at_unix_ms
        )
        validate_proposal_for_request(proposal, request)
        return proposal, self.artifacts.put(
            json.dumps(patch, sort_keys=True, allow_nan=False).encode()
        )


def generate_record(agent, request):
    proposal, patch_ref = agent.generate(request)
    return {
        "proposal_base64": base64.b64encode(encode_message(proposal)).decode(),
        "patch": reference(patch_ref),
    }


def unpack_generation(record, request):
    proposal = decode_message(
        pb.Proposal, base64.b64decode(record["proposal_base64"], validate=True)
    )
    validate_proposal_for_request(proposal, request)
    return proposal, pb.ArtifactRef(**record["patch"])
