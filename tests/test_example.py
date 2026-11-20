"""Check the trusted example against its committed submission fixtures.

This is a development check, not the untrusted-submission execution pipeline.
"""

import json
import subprocess
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sum-array"
MANIFEST = json.loads((EXAMPLE / "submission.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", MANIFEST["tests"], ids=lambda case: case["id"])
def test_example_matches_expected_output(example_program, case):
    result = subprocess.run(
        [str(example_program), *case["args"]],
        input=(EXAMPLE / case["stdin"]).read_bytes(),
        capture_output=True,
        timeout=MANIFEST["limits"]["run_wall_seconds"],
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout == (EXAMPLE / case["expected_stdout"]).read_bytes()
