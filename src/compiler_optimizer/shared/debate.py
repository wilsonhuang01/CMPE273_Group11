"""Operator-owned revision limits and a final corpus never included in agent prompts."""

import json
from dataclasses import asdict, dataclass

from compiler_optimizer.shared.manifest import require


def unique(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "duplicate debate field")
        value[key] = item
    return value


@dataclass(frozen=True)
class DebatePolicy:
    schema_version: int = 1
    max_rounds: int = 2
    max_agent_calls: int = 32
    max_seconds: int = 1800
    heldout_cases: tuple = ()

    @classmethod
    def parse(cls, data):
        if isinstance(data, bytes | str):
            require(len(data) <= 131072, "debate policy exceeds 128 KiB")
            data = json.loads(data, object_pairs_hook=unique)
        require(isinstance(data, dict) and set(data) == set(asdict(cls())), "invalid debate policy")
        for key, low, high in (
            ("schema_version", 1, 1),
            ("max_rounds", 1, 6),
            ("max_agent_calls", 1, 128),
            ("max_seconds", 1, 86400),
        ):
            require(type(data[key]) is int and low <= data[key] <= high, "invalid debate " + key)
        cases = data["heldout_cases"]
        require(
            isinstance(cases, (list, tuple)) and 1 <= len(cases) <= 32,
            "debate requires 1–32 held-out final cases",
        )
        for case in cases:
            require(
                isinstance(case, dict)
                and set(case) == {"stdin", "args"}
                and isinstance(case["stdin"], str)
                and len(case["stdin"].encode()) <= 32768
                and isinstance(case["args"], list)
                and len(case["args"]) <= 32
                and all(
                    isinstance(arg, str) and len(arg.encode()) <= 1024 and "\x00" not in arg
                    for arg in case["args"]
                ),
                "invalid held-out case",
            )
        return cls(**{**data, "heldout_cases": tuple(cases)})

    def encode(self):
        data = json.dumps(asdict(self), sort_keys=True, allow_nan=False).encode()
        self.parse(data)
        return data


def revision_context(data):
    require(
        isinstance(data, str) and len(data.encode()) <= 262144, "revision context exceeds 256 KiB"
    )
    value = json.loads(data, object_pairs_hook=unique)
    require(
        isinstance(value, dict)
        and set(value)
        == {
            "schema_version",
            "round",
            "parent_proposal_id",
            "parent_source_sha256",
            "previous_patch",
            "feedback",
        },
        "invalid revision context",
    )
    require(
        type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and type(value["round"]) is int
        and 2 <= value["round"] <= 6
        and isinstance(value["previous_patch"], dict)
        and isinstance(value["feedback"], dict),
        "invalid revision feedback",
    )
    from uuid import UUID

    require(
        isinstance(value["parent_proposal_id"], str)
        and str(UUID(value["parent_proposal_id"])) == value["parent_proposal_id"],
        "invalid revision parent",
    )
    from compiler_optimizer.shared.contracts import SHA256

    require(
        isinstance(value["parent_source_sha256"], str)
        and SHA256.fullmatch(value["parent_source_sha256"]),
        "invalid revision source hash",
    )
    return value
