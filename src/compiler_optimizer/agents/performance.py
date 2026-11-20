"""Performance Agent with a replaceable structured-patch backend."""

import hashlib
import json
from pathlib import Path
from typing import Protocol

from compiler_optimizer.shared.contracts import validate_proposal_for_request
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.patches import MAX_PATCH_BYTES, apply_patch
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.workers.baseline import metadata
from compiler_optimizer.workers.package import bundle, unbundle


class PatchBackend(Protocol):
    def generate(self, request: pb.OptimizationRequest, sources: dict[str, bytes]) -> bytes:
        """Return JSON matching shared.patches; source is untrusted data, never a command."""
        ...


class ExamplePerformanceBackend:
    """Deterministic rule for the repository's original C sum-array example only."""

    def generate(self, request, sources):
        require(
            request.manifest.language == "c17"
            and set(sources) == {"main.c"}
            and hashlib.sha256(sources["main.c"]).hexdigest()
            == "ee9733cab3f6852c98c2bfeedb2f5c50683e4a478562bb738a5ff9a2a12db8af",
            "starter agent supports only the original sum-array example; supply a patch backend",
        )
        return json.dumps(
            {
                "schema_version": 1,
                "parent_source_sha256": request.source.bundle.sha256,
                "transformation": "stdio-input-buffer-64k",
                "rationale": "Increase stdin buffering to 64 KiB to reduce refill overhead on "
                "integer streams. A static buffer remains live until process exit. "
                "Correctness and runtime/memory gains must be independently measured.",
                "edits": [
                    {
                        "path": "main.c",
                        "old": "int main(void) {\n",
                        "new": "int main(void) {\n    static char input_buffer[65536];\n"
                        "    (void)setvbuf(stdin, input_buffer, _IOFBF, sizeof(input_buffer));\n",
                    }
                ],
            },
            sort_keys=True,
        ).encode()


class FilePatchBackend:
    """Replay a structured patch produced by a model or an operator, without executing it."""

    def __init__(self, path):
        self.path = Path(path)

    def generate(self, request, sources):
        with self.path.open("rb") as stream:
            data = stream.read(MAX_PATCH_BYTES + 1)
        require(len(data) <= MAX_PATCH_BYTES, "patch exceeds 1 MiB")
        return data


class PerformanceAgent:
    def __init__(self, artifacts, backend):
        self.artifacts, self.backend = artifacts, backend

    def generate(self, request):
        require(request.role == pb.PERFORMANCE, "Performance Agent requires PERFORMANCE assignment")
        sources = unbundle(self.artifacts.get(request.source.bundle))
        data = self.backend.generate(request, dict(sources))
        patch, changed = apply_patch(data, sources, request.source.bundle.sha256)
        canonical = json.dumps(patch, sort_keys=True, allow_nan=False).encode()
        proposal = pb.Proposal(
            meta=metadata(request.meta.task_id),
            proposal_id=request.proposal_id,
            role=pb.PERFORMANCE,
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
        return proposal, self.artifacts.put(canonical)
