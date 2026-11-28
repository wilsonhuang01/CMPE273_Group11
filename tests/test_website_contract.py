"""Browser contract fixtures and invalid boundary cases, without a running backend."""

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1] / "contracts/web/v1"
SCHEMA = json.loads((ROOT / "schema.json").read_text())


def validator(name):
    return Draft202012Validator({**SCHEMA, "oneOf": [{"$ref": f"#/$defs/{name}"}]})


@pytest.mark.parametrize("path", sorted((ROOT / "fixtures").glob("*.json")), ids=lambda p: p.stem)
def test_browser_fixture(path):
    Draft202012Validator.check_schema(SCHEMA)
    name = path.stem if path.stem in {"submission", "receipt", "examples", "error"} else "task"
    validator(name).validate(json.loads(path.read_text()))


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "completed"},
        {"state": "failed", "reason": None},
        {"state": "disconnected"},
        {"task_id": "00000000-0000-0000-0000-000000000000"},
        {"task_id": "not-a-task"},
        {"percent_complete": 50},
    ],
)
def test_invalid_task_boundaries(changes):
    task = json.loads((ROOT / "fixtures/queued.json").read_text())
    task.update(changes)
    assert not validator("task").is_valid(task)


def test_result_only_belongs_to_completed_task():
    task = json.loads((ROOT / "fixtures/completed.json").read_text())
    task["state"] = "running"
    assert not validator("task").is_valid(task)


def test_submission_cannot_select_paths_credentials_or_emulated_target():
    submission = json.loads((ROOT / "fixtures/submission.json").read_text())
    for change in (
        {"example_id": "../../etc/passwd"},
        {"target": "linux-riscv64"},
        {"token": "secret"},
    ):
        assert not validator("submission").is_valid({**submission, **change})


def test_openapi_message_references_resolve():
    document = json.loads((ROOT / "openapi.json").read_text())

    def walk(value):
        if isinstance(value, dict):
            if "$ref" in value:
                filename, pointer = value["$ref"].split("#")
                target = json.loads((ROOT / filename).read_text())
                for part in pointer.lstrip("/").split("/"):
                    target = target[part]
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(copy.deepcopy(document))
    assert set(document["paths"]) == {
        "/api/v1/examples",
        "/api/v1/tasks",
        "/api/v1/tasks/{task_id}",
        "/api/v1/tasks/{task_id}/download",
    }
