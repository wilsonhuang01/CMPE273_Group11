"""Validate manifest data without accessing or executing submission files."""

import json
import re
from importlib.resources import files

from google.protobuf.json_format import MessageToDict, ParseDict, ParseError
from jsonschema import Draft202012Validator, ValidationError, validators

from compiler_optimizer.shared.v1 import contracts_pb2 as pb

SCHEMA = json.loads(
    files("compiler_optimizer.shared.v1")
    .joinpath("submission.schema.json")
    .read_text(encoding="utf-8")
)
# JSON Schema's mathematical integer type accepts 1.0; the contract requires int.
StrictValidator = validators.extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
        "integer", lambda checker, value: type(value) is int
    ),
)
StrictValidator.check_schema(SCHEMA)
VALIDATOR = StrictValidator(SCHEMA)


class ContractError(ValueError):
    """A message or manifest violates the versioned contract."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def validate_path(path: str) -> None:
    require(
        bool(path)
        and len(path) <= 512
        and "\\" not in path
        and ":" not in path
        and not any(ord(char) < 32 for char in path)
        and all(part not in ("", ".", "..") for part in path.split("/")),
        "paths must be normalized relative POSIX paths without traversal",
    )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_manifest(data: str | bytes | dict) -> pb.SubmissionManifest:
    """Reject structural errors and cross-reference errors; do not inspect files."""
    if isinstance(data, str | bytes):
        try:
            require(
                len(data.encode("utf-8") if isinstance(data, str) else data) <= 10 * 1024**2,
                "manifest exceeds package size limit",
            )
            data = json.loads(data, object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise ContractError(f"invalid manifest JSON: {error}") from error
    try:
        VALIDATOR.validate(data)
    except ValidationError as error:
        raise ContractError(
            f"invalid manifest at {list(error.absolute_path)}: {error.message}"
        ) from error

    extension = ".c" if data["language"] == "c17" else ".cpp"
    for source in data["sources"]:
        validate_path(source)
        require(source.endswith(extension), f"source must end in {extension}")
    ids = [case["id"] for case in data["tests"]]
    require(
        all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) for value in [data["name"], *ids]),
        "invalid name or test ID",
    )
    require(len(ids) == len(set(ids)), "test IDs must be unique")
    require(set(data["benchmark_test_ids"]) <= set(ids), "unknown benchmark test ID")
    for case in data["tests"]:
        validate_path(case["stdin"])
        validate_path(case["expected_stdout"])
        for arg in case["args"]:
            try:
                valid = "\0" not in arg and len(arg.encode("utf-8")) <= 1024
            except UnicodeError:
                valid = False
            require(valid, "arguments must be UTF-8, NUL-free, and at most 1024 bytes")
    try:
        return ParseDict(data, pb.SubmissionManifest())
    except (ValueError, UnicodeError, ParseError) as error:
        raise ContractError(f"invalid manifest: {error}") from error


def validate_manifest(manifest: pb.SubmissionManifest) -> None:
    # Include empty repeated fields so that empty args remains a valid JSON array.
    parse_manifest(
        MessageToDict(
            manifest, preserving_proto_field_name=True, always_print_fields_with_no_presence=True
        )
    )
