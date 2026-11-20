"""Bounded exact-text edits against an immutable source snapshot; never invoke patch tools."""

import json

from compiler_optimizer.shared.contracts import SHA256, TEST_ID
from compiler_optimizer.shared.manifest import require, validate_path

MAX_PATCH_BYTES = 1024 * 1024


def _object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate patch field")
        result[key] = value
    return result


def parse_patch(data):
    require(
        isinstance(data, bytes) and len(data) <= MAX_PATCH_BYTES,
        "patch must be at most 1 MiB of JSON",
    )
    value = json.loads(data, object_pairs_hook=_object)
    require(
        isinstance(value, dict)
        and set(value)
        == {"schema_version", "parent_source_sha256", "rationale", "transformation", "edits"},
        "patch has missing/unknown fields",
    )
    require(
        type(value["schema_version"]) is int and value["schema_version"] == 1,
        "unsupported patch schema",
    )
    require(
        isinstance(value["parent_source_sha256"], str)
        and SHA256.fullmatch(value["parent_source_sha256"]),
        "invalid patch parent hash",
    )
    require(
        isinstance(value["rationale"], str) and 0 < len(value["rationale"].encode()) <= 16384,
        "patch requires a bounded rationale",
    )
    require(
        isinstance(value["transformation"], str) and TEST_ID.fullmatch(value["transformation"]),
        "invalid patch transformation ID",
    )
    edits = value["edits"]
    require(isinstance(edits, list) and 1 <= len(edits) <= 32, "patch requires 1–32 edits")
    for edit in edits:
        require(
            isinstance(edit, dict)
            and set(edit) == {"path", "old", "new"}
            and all(isinstance(v, str) for v in edit.values()),
            "invalid text edit",
        )
        validate_path(edit["path"])
        require(
            bool(edit["old"]) and edit["old"] != edit["new"] and "\x00" not in edit["new"],
            "empty, unchanged, or NUL-containing edit",
        )
    return value


def apply_patch(data, sources, parent_sha256):
    patch = parse_patch(data)
    require(patch["parent_source_sha256"] == parent_sha256, "patch parent hash mismatch")
    result = dict(sources)
    for edit in patch["edits"]:
        require(edit["path"] in sources, "patch may only edit existing source files/headers")
        old, new = edit["old"].encode(), edit["new"].encode()
        contents = result[edit["path"]]
        require(contents.count(old) == 1, "patch context is missing or ambiguous")
        result[edit["path"]] = contents.replace(old, new, 1)
    require(sum(map(len, result.values())) <= 10 * 1024**2, "patched sources exceed 10 MiB")
    require(result != sources, "patch does not change the source")
    return patch, result
