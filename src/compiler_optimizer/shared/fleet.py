"""Versioned trusted-worker profiles and bounded compiler invocation transport."""

import base64
import json
import math
import re
from dataclasses import asdict

from google.protobuf.json_format import MessageToDict, ParseDict

from compiler_optimizer.shared.contracts import _compiler
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.transfer import validate_task_id
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.workers.docker_runner import Invocation
from compiler_optimizer.workers.policy import POLICY_DIGEST

LEASE_MS = 10000
FRESH_MS = 6000
MAX_RESULT_BYTES = 32 * 1024**2


def canonical(value):
    return json.dumps(value, sort_keys=True, allow_nan=False).encode()


def describe_runner(runner):
    return {
        "schema_version": 1,
        "target": runner.target,
        "image": runner.image,
        "hardware": runner.hardware,
        "platform": runner.platform,
        "native_execution": runner.native_execution,
        "native_amd64": runner.native_amd64,
        "supports_sanitizers": runner.supports_sanitizers,
        "measurement_version": runner.measurement_version,
        "compilers": {
            lang: MessageToDict(runner.compiler(lang), preserving_proto_field_name=True)
            for lang in ("c17", "cpp17")
        },
        "context": runner.recovery_context(),
    }


def validate_profile(text):
    require(len(text.encode()) <= 65536, "worker profile too large")
    value = json.loads(text)
    require(
        set(value)
        == {
            "schema_version",
            "target",
            "image",
            "hardware",
            "platform",
            "native_execution",
            "native_amd64",
            "supports_sanitizers",
            "measurement_version",
            "compilers",
            "context",
        }
        and value["schema_version"] == 1,
        "invalid worker profile",
    )
    require(
        isinstance(value["hardware"], str) and len(value["hardware"].encode()) <= 4096,
        "invalid worker hardware",
    )
    for key in ("native_execution", "native_amd64", "supports_sanitizers"):
        require(type(value[key]) is bool, "invalid capability")
    require(value["measurement_version"] in (1, 2), "invalid measurement version")
    require(set(value["compilers"]) == {"c17", "cpp17"}, "missing compiler capabilities")
    for language, compiler in value["compilers"].items():
        item = ParseDict(compiler, pb.CompilerSettings())
        _compiler(item)
        require(
            item.language == language
            and item.target == value["target"]
            and item.image_digest == value["image"],
            "compiler profile mismatch",
        )
    require(
        value["context"]["policy_digest"] == POLICY_DIGEST
        and value["context"]["target"] == value["target"]
        and value["context"]["platform"] == value["platform"],
        "sandbox profile mismatch",
    )
    engine = value["context"]["hardware"]["engine_id"]
    require(isinstance(engine, str) and 0 < len(engine) <= 256, "invalid worker engine")
    return value


def validate_identity(identity):
    validate_task_id(identity.worker_id)
    validate_task_id(identity.incarnation_id)
    require(re.fullmatch(r"[0-9a-f]{64}", identity.secret) is not None, "invalid worker secret")


def lease_from_row(row):
    return pb.FleetLease(
        job_id=row["job_id"],
        token=row["token"],
        fence=row["fence"],
        expires_ms=row["expires_ms"] or 0,
        deadline_ms=row["deadline_ms"],
    )


def encode_invocation(invocation):
    value = asdict(invocation)
    for key in ("stdout", "stderr", "binary"):
        if value[key] is not None:
            value[key] = base64.b64encode(value[key]).decode()
    return {"invocation": value}


def decode_result(data, operation):
    require(len(data) <= MAX_RESULT_BYTES, "worker result too large")
    value = json.loads(data)
    if set(value) == {"error"}:
        require(value["error"] == "InfrastructureError", "unsupported worker error")
        return value
    if operation == "environment":
        require(
            set(value) == {"environment"} and isinstance(value["environment"], dict),
            "invalid worker environment",
        )
        return value
    require(
        set(value) == {"invocation"} and isinstance(value["invocation"], dict),
        "invalid worker result",
    )
    row = dict(value["invocation"])
    require(set(row) == set(Invocation.__dataclass_fields__), "incomplete worker invocation")
    for key in ("stdout", "stderr", "binary"):
        if row[key] is not None:
            row[key] = base64.b64decode(row[key], validate=True)
    require(
        isinstance(row["stdout"], bytes)
        and isinstance(row["stderr"], bytes)
        and len(row["stdout"]) + len(row["stderr"]) <= 1024**2,
        "invalid worker output",
    )
    require(row["binary"] is None or len(row["binary"]) <= 16 * 1024**2, "worker binary too large")
    require(
        type(row["returncode"]) is int
        and type(row["peak_memory_bytes"]) is int
        and row["peak_memory_bytes"] >= 0,
        "invalid worker resource result",
    )
    require(
        type(row["wall_seconds"]) in (int, float)
        and math.isfinite(row["wall_seconds"])
        and row["wall_seconds"] > 0,
        "invalid worker timing",
    )
    require(
        row["failure"]
        in (None, "timeout", "memory_limit", "output_limit", "signal", "nonzero_exit")
        and (row["failure"] is not None or row["returncode"] == 0),
        "invalid failure result",
    )
    require(
        isinstance(row["sandbox"], dict)
        and row["sandbox"].get("policy_digest") == POLICY_DIGEST
        and row["sandbox"].get("watchdog", {}).get("state") == "removed",
        "worker cleanup/policy unconfirmed",
    )
    return Invocation(**row)
