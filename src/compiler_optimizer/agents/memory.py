"""Memory Agent and legacy example sources retained for reproducible evaluation recipes."""

import hashlib
import json

from compiler_optimizer.agents.generation import SourcePatchAgent
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.v1 import contracts_pb2 as pb

BUFFERED_SOURCE = b"""#include <stdio.h>
#include <stdlib.h>

/* Input: count in [0, 1000000], then count integers in [-1000000, 1000000]. */
int main(void) {
    int count;
    if (scanf("%d", &count) != 1 || count < 0 || count > 1000000) {
        return 1;
    }
    long long *values = malloc(((size_t)count + 1) * sizeof(*values));
    if (values == NULL) {
        return 1;
    }
    for (int i = 0; i < count; ++i) {
        if (scanf("%lld", &values[i]) != 1 || values[i] < -1000000 || values[i] > 1000000) {
            free(values);
            return 1;
        }
    }
    long long sum = 0;
    for (int i = 0; i < count; ++i) {
        sum += values[i];
    }
    free(values);
    printf("%lld\\n", sum);
    return 0;
}
"""

STREAMING_SOURCE = b"""#include <stdio.h>

/* Same bounded integer input domain; sum without retaining the full array. */
int main(void) {
    int count;
    if (scanf("%d", &count) != 1 || count < 0 || count > 1000000) {
        return 1;
    }
    long long sum = 0;
    for (int i = 0; i < count; ++i) {
        long long value;
        if (scanf("%lld", &value) != 1 || value < -1000000 || value > 1000000) {
            return 1;
        }
        sum += value;
    }
    printf("%lld\\n", sum);
    return 0;
}
"""


class ExampleMemoryBackend:
    def generate(self, request, sources):
        require(
            request.manifest.language == "c17"
            and set(sources) == {"main.c"}
            and hashlib.sha256(sources["main.c"]).digest()
            == hashlib.sha256(BUFFERED_SOURCE).digest(),
            "Memory starter supports only sum-array-buffered; supply a patch backend",
        )
        return json.dumps(
            {
                "schema_version": 1,
                "parent_source_sha256": request.source.bundle.sha256,
                "transformation": "stream-bounded-integer-sum",
                "rationale": "Replace the retained heap array with a streaming sum, reducing array "
                "storage from O(count) to O(1). The declared integer bounds keep the "
                "long long sum representable. Allocation failure is removed; equivalence "
                "is evaluated for valid inputs on a worker with sufficient baseline memory. "
                "Sanitizers, differential tests and measured peak RSS remain mandatory.",
                "edits": [
                    {
                        "path": "main.c",
                        "old": BUFFERED_SOURCE.decode(),
                        "new": STREAMING_SOURCE.decode(),
                    }
                ],
            },
            sort_keys=True,
        ).encode()


class MemoryAgent(SourcePatchAgent):
    role = pb.MEMORY
