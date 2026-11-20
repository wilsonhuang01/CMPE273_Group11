"""Strict review records bound to exact proposal and automated verification evidence."""

import hashlib
import json

from compiler_optimizer.shared.agent_roles import role_spec
from compiler_optimizer.shared.contracts import TEST_ID
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.messaging import encode_message
from compiler_optimizer.shared.v1 import contracts_pb2 as pb


def review_operation(proposal, result):
    return f"{proposal.proposal_id}:{result.job_id}:{result.attempt}"


def review_context(proposal, result):
    ref = result.correctness_report
    return {
        "schema_version": 1,
        "task_id": proposal.meta.task_id,
        "proposal_id": proposal.proposal_id,
        "source_sha256": proposal.source.bundle.sha256,
        "proposal_sha256": hashlib.sha256(encode_message(proposal)).hexdigest(),
        "verification_job_id": result.job_id,
        "verification_attempt": result.attempt,
        "verification_sha256": hashlib.sha256(encode_message(result)).hexdigest(),
        "automated_passed": result.WhichOneof("outcome") == "success",
        "correctness_report": {"key": ref.key, "sha256": ref.sha256, "size_bytes": ref.size_bytes},
    }


def validate_review(value, context, role):
    require(role in (pb.CORRECTNESS, pb.CRITIC), "role does not produce reviews")
    require(
        isinstance(value, dict)
        and set(value) == set(context) | {"role", "verdict", "summary", "findings"},
        "invalid review fields",
    )
    require(
        all(value[key] == expected for key, expected in context.items())
        and value["role"] == role_spec(role).name,
        "review evidence binding mismatch",
    )
    require(value["verdict"] in ("approve", "reject", "abstain"), "invalid review verdict")
    require(
        isinstance(value["summary"], str) and 0 < len(value["summary"].encode()) <= 4096,
        "invalid review summary",
    )
    findings = value["findings"]
    require(isinstance(findings, list) and len(findings) <= 64, "review exceeds 64 findings")
    for finding in findings:
        require(
            isinstance(finding, dict)
            and set(finding) == {"code", "severity", "message"}
            and isinstance(finding["code"], str)
            and TEST_ID.fullmatch(finding["code"])
            and finding["severity"] in ("info", "warning", "error")
            and isinstance(finding["message"], str)
            and 0 < len(finding["message"].encode()) <= 2048,
            "invalid review finding",
        )
    errors = any(finding["severity"] == "error" for finding in findings)
    require(value["verdict"] != "approve" or not errors, "approval contains blocking findings")
    require(value["verdict"] != "reject" or errors, "rejection needs a blocking finding")
    require(
        role != pb.CORRECTNESS or value["verdict"] != "approve" or context["automated_passed"],
        "review cannot approve failed automated tests",
    )
    require(len(json.dumps(value).encode()) <= 65536, "review exceeds 64 KiB")
    return value
