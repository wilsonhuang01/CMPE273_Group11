"""Critic Agent: bounded source-risk review, separate from automated semantic checks."""

import json
import re
from collections import Counter

from compiler_optimizer.agents.correctness import checked_verification
from compiler_optimizer.agents.journal import AgentJournal
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.reviews import review_context, review_operation, validate_review
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.workers.package import unbundle

LEX = re.compile(
    r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|'
    r"[A-Za-z_]\w*|[^\s]"
)
RISKY_CALLS = frozenset(
    {
        "system",
        "popen",
        "fork",
        "vfork",
        "clone",
        "execve",
        "execv",
        "execl",
        "posix_spawn",
        "socket",
        "connect",
        "fopen",
        "freopen",
        "open",
        "unlink",
        "remove",
        "rand",
        "srand",
        "time",
        "getenv",
        "dlopen",
        "mprotect",
        "alloca",
        "__builtin_assume",
        "__builtin_assume_aligned",
        "pthread_create",
    }
)
RISKY_TOKENS = frozenset({"asm", "__asm", "__asm__", "__restrict", "__restrict__", "restrict"})


def risk_counts(sources):
    counts = Counter()
    for data in sources.values():
        tokens = [
            token
            for token in LEX.findall(data.decode("utf-8", errors="replace"))
            if not token.startswith(('"', "'", "//", "/*"))
        ]
        for index, token in enumerate(tokens):
            if token in RISKY_TOKENS or (
                token in RISKY_CALLS and tokens[index + 1 : index + 2] == ["("]
            ):
                counts[token] += 1
    return counts


class CriticAgent:
    def __init__(self, repository, backend=None):
        self.repository, self.backend = repository, backend

    def review(self, proposal, result):
        repository = self.repository
        checked_verification(repository, proposal, result)
        context = review_context(proposal, result)
        baseline = repository.get_task(proposal.meta.task_id)["submission"].source.bundle
        require(
            proposal.source.parent_source_sha256 == baseline.sha256,
            "pipeline critic requires the assigned baseline parent",
        )
        before = unbundle(repository.artifacts.get(baseline))
        after = unbundle(repository.artifacts.get(proposal.source.bundle))
        require(set(before) == set(after), "critic requires unchanged source file membership")

        def action():
            if self.backend is not None:
                response = self.backend.review(
                    json.loads(json.dumps(context)), dict(before), dict(after), proposal.rationale
                )
                require(
                    isinstance(response, dict)
                    and set(response) == {"verdict", "summary", "findings"},
                    "critic backend may return verdict, summary and findings only",
                )
            else:
                additions = risk_counts(after) - risk_counts(before)
                findings = [
                    {
                        "code": "new-risky-operation",
                        "severity": "error",
                        "message": "New operation requires manual review: " + token,
                    }
                    for token in sorted(additions)
                ]
                if not findings:
                    findings.append(
                        {
                            "code": "static-review-limit",
                            "severity": "info",
                            "message": "No blocked additions. Static review cannot establish "
                            "equivalence; automated verification and benchmarks remain required.",
                        }
                    )
                if any(
                    term in proposal.rationale.lower()
                    for term in ("guaranteed", "always faster", "skip tests", "proven equivalent")
                ):
                    findings.append(
                        {
                            "code": "unsupported-claim",
                            "severity": "warning",
                            "message": "Optimization claims need independent evidence.",
                        }
                    )
                response = {
                    "verdict": "reject" if additions else "approve",
                    "summary": "New high-risk operations require manual review."
                    if additions
                    else "No blocking source-risk findings.",
                    "findings": findings,
                }
            return validate_review({**context, "role": "critic", **response}, context, pb.CRITIC)

        record = AgentJournal(repository, pb.CRITIC).run(
            proposal.meta.task_id, review_operation(proposal, result), context, action
        )
        return validate_review(record, context, pb.CRITIC)
