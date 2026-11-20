"""Connect the repository-owned CMake executable to the example checks."""

from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--program",
        default="build/bin/sum-array",
        help="Path to the repository-owned sum-array executable built by CMake",
    )


@pytest.fixture(scope="session")
def example_program(pytestconfig):
    program = Path(pytestconfig.getoption("program")).resolve()
    if not program.is_file():
        pytest.fail(f"Example executable missing: {program}. Build with CMake first.")
    return program
