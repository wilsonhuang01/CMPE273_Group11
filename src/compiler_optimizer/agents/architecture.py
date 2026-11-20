"""Architecture Agent: portable source changes under the immutable generic x86-64 profile."""

import hashlib
import json

from compiler_optimizer.agents.generation import SourcePatchAgent
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.v1 import contracts_pb2 as pb


class ExampleArchitectureBackend:
    def generate(self, request, sources):
        require(
            request.manifest.language == "c17"
            and request.compiler.target == "linux-x86_64"
            and set(sources) == {"main.c"}
            and hashlib.sha256(sources["main.c"]).hexdigest()
            == "ee9733cab3f6852c98c2bfeedb2f5c50683e4a478562bb738a5ff9a2a12db8af",
            "Architecture starter requires the original sum-array and generic x86-64 profile",
        )
        return json.dumps(
            {
                "schema_version": 1,
                "parent_source_sha256": request.source.bundle.sha256,
                "transformation": "portable-independent-accumulators",
                "rationale": "Expose two independent accumulation streams to the fixed compiler. "
                "Retain all scanf validation and integer bounds; use portable C without "
                "intrinsics, inline assembly, CPU-specific flags or fast-math assumptions. "
                "The extra branch may cost more than it saves, so measured gain is required.",
                "edits": [
                    {
                        "path": "main.c",
                        "old": "long long sum = 0;",
                        "new": "long long sum = 0, alternate = 0;",
                    },
                    {
                        "path": "main.c",
                        "old": "        sum += value;",
                        "new": "        if ((i & 1) == 0) {\n            sum += value;\n"
                        "        } else {\n            alternate += value;\n        }",
                    },
                    {
                        "path": "main.c",
                        "old": 'printf("%lld\\n", sum);',
                        "new": 'printf("%lld\\n", sum + alternate);',
                    },
                ],
            },
            sort_keys=True,
        ).encode()


class ArchitectureAgent(SourcePatchAgent):
    role = pb.ARCHITECTURE
