"""Connect the repository-owned CMake executable to the example checks.

``--postgres-url`` (or ``TEST_POSTGRES_URL``) reruns the suite against PostgreSQL: every
``Repository(root)`` without an explicit database gets its own schema derived from its
directory, so reopening a directory reopens the same state, as with SQLite.
"""

import hashlib
import os
from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--program",
        default="build/bin/sum-array",
        help="Path to the repository-owned sum-array executable built by CMake",
    )
    parser.addoption(
        "--postgres-url",
        default=os.environ.get("TEST_POSTGRES_URL", ""),
        help="Run Repository-backed tests against this PostgreSQL database",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "sqlite_only: inspects or forks the SQLite file; skipped with --postgres-url"
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("postgres_url"):
        skip = pytest.mark.skip(reason="SQLite-specific behavior")
        for item in items:
            if "sqlite_only" in item.keywords:
                item.add_marker(skip)


def schema_for(root):
    return "test_" + hashlib.sha256(str(Path(root).absolute()).encode()).hexdigest()[:24]


@pytest.fixture
def service_database_env(pytestconfig):
    """Environment that points a spawned coordinator at the same state as ``Repository(root)``."""
    url = pytestconfig.getoption("postgres_url")
    return lambda root: {"DATABASE_URL": url, "DATABASE_SCHEMA": schema_for(root)} if url else {}


@pytest.fixture(scope="session", autouse=True)
def postgres_repositories(pytestconfig):
    url = pytestconfig.getoption("postgres_url")
    if not url:
        yield None
        return
    import psycopg

    from compiler_optimizer.storage import Repository
    from compiler_optimizer.storage.postgres import PostgresDatabase

    databases = {}
    original = Repository.__init__

    def init(self, root, *, artifacts=None, database=None):
        if database is None:
            schema = schema_for(root)
            database = databases.setdefault(
                schema, PostgresDatabase(url, schema=schema, pool_size=1)
            )
        original(self, root, artifacts=artifacts, database=database)

    Repository.__init__ = init
    try:
        yield databases
    finally:
        Repository.__init__ = original
        for database in databases.values():
            database.close()
        with psycopg.connect(url, autocommit=True) as connection:
            for schema in databases:
                connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.fixture(scope="session")
def example_program(pytestconfig):
    program = Path(pytestconfig.getoption("program")).resolve()
    if not program.is_file():
        pytest.fail(f"Example executable missing: {program}. Build with CMake first.")
    return program


@pytest.fixture(autouse=True)
def release_postgres_connections(postgres_repositories):
    yield
    for database in (postgres_repositories or {}).values():
        database.close()  # Keep idle pooled connections from accumulating across tests.
