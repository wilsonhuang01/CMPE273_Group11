"""PostgreSQL backend for the coordinator repository (action R, phase 2).

The repository's SQL is written for SQLite. This backend runs the same statements on
PostgreSQL (Amazon RDS) without forking the storage modules:

- ``?`` placeholders become ``%s`` and ``INSERT OR IGNORE`` becomes
  ``INSERT ... ON CONFLICT DO NOTHING``. Nothing else is rewritten.
- Every table has a trailing ``rowid`` identity column (see ``postgres.sql``), so
  ``ORDER BY rowid`` keeps SQLite's insertion order. Rows hide that column, and support
  index and name access like ``sqlite3.Row``.
- Writes keep SQLite's ``BEGIN IMMEDIATE`` semantics: each write transaction first takes
  one transaction-scoped advisory lock per schema, so writers are serialized and every
  statement sees all previously committed writes. Reads use a consistent read-only
  REPEATABLE READ snapshot, like a deferred SQLite read transaction.
- Parameters are bound client-side, which gives SQLite's loose typing for untyped
  expressions such as ``? IS NULL``, and Python booleans are stored as 0/1 integers.

Unique, foreign-key and check violations raise ``ConflictError`` (as SQLite
``IntegrityError`` does). Connection, timeout and server errors raise ``UnavailableError``,
which existing handlers map to gRPC ``UNAVAILABLE``. Row-level claims and fencing that
let several coordinators commit concurrently are phase 3.

With ``credentials`` (a rotating Secrets Manager view, phase 4), every new connection
uses the current username and password, and a rejected password is re-read
immediately, so an RDS-managed rotation never needs a coordinator restart.
"""

import hashlib
import os
import re
import threading
import time
from contextlib import contextmanager
from functools import lru_cache
from importlib.resources import files
from urllib.parse import parse_qs, urlsplit

from compiler_optimizer.runtime.secrets import SecretUnavailable
from compiler_optimizer.storage.errors import ConflictError, StateError, UnavailableError

BASELINE_VERSION = 12  # postgres.sql reproduces SQLite schema version 12.
# Each later version is one SQL file, applied in order inside the migration transaction.
MIGRATIONS = {13: "postgres_dispatch.sql"}
SCHEMA_VERSION = max(MIGRATIONS)
_INSERT_OR_IGNORE = re.compile(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\b", re.IGNORECASE)
_SCHEMA_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")
LOOPBACK = {"localhost", "127.0.0.1", "::1"}


@lru_cache(maxsize=1024)
def translate(sql: str, has_params: bool = True) -> str:
    """Rewrite one SQLite-dialect statement for psycopg/PostgreSQL."""
    out, quote = [], None
    for char in sql:
        if quote is not None:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "?":
            out.append("%s")
            continue
        if char == "%" and has_params:
            out.append("%%")  # psycopg treats % as a placeholder marker everywhere.
            continue
        out.append(char)
    statement = "".join(out)
    if _INSERT_OR_IGNORE.match(statement):
        statement = _INSERT_OR_IGNORE.sub("INSERT INTO", statement, count=1)
        statement = statement.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return statement


class Row:
    """Read-only row with ``sqlite3.Row`` access: ``row[0]``, ``row["name"]``, ``dict(row)``."""

    __slots__ = ("_names", "_values", "_index")

    def __init__(self, names, values, index):
        self._names, self._values, self._index = names, values, index

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return self._values[self._index[key]]
            except KeyError:
                raise IndexError(f"no such column: {key}") from None
        return self._values[key]

    def keys(self):
        return list(self._names)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __eq__(self, other):
        return (
            isinstance(other, Row) and self._names == other._names and self._values == other._values
        )

    __hash__ = None

    def __repr__(self):
        return f"Row({dict(zip(self._names, self._values, strict=True))!r})"


def _row_factory(cursor):
    description = cursor.description or ()
    keep = [i for i, column in enumerate(description) if column.name != "rowid"]
    names = tuple(description[i].name for i in keep)
    index = {name: position for position, name in enumerate(names)}
    if len(keep) == len(description):
        return lambda values: Row(names, tuple(values), index)
    return lambda values: Row(names, tuple(values[i] for i in keep), index)


class _Session:
    """The ``db`` object storage methods receive: ``execute`` accepts SQLite-dialect SQL."""

    __slots__ = ("connection",)

    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=()):
        params = tuple(params)
        return self.connection.execute(
            translate(sql, bool(params)), params if params else None, prepare=False
        )


def _driver():
    try:
        import psycopg
    except ImportError as error:
        raise RuntimeError(
            "the PostgreSQL backend requires psycopg: pip install -e '.[postgres]'"
        ) from error
    return psycopg


def _authentication_failed(error) -> bool:
    # libpq reports connection-time failures without a SQLSTATE, so match its message too.
    return getattr(error, "sqlstate", None) in ("28P01", "28000") or (
        "password authentication failed" in str(error)
    )


def validate_schema(schema: str) -> str:
    if not _SCHEMA_NAME.fullmatch(schema) or schema.startswith("pg_"):
        raise ValueError("DATABASE_SCHEMA must be a lowercase SQL identifier")
    return schema


def validate_database_url(url: str) -> None:
    """Reject URLs that embed passwords or reach a remote host without verified TLS."""
    parsed = urlsplit(url)
    query = {key.lower(): values for key, values in parse_qs(parsed.query).items()}
    if parsed.scheme not in ("postgresql", "postgres") or not parsed.hostname:
        raise ValueError("DATABASE_URL must be a postgresql:// URL with a host")
    if parsed.password or "password" in query:
        raise ValueError("DATABASE_URL must not contain a password; use DATABASE_PASSWORD")
    if not parsed.path.strip("/"):
        raise ValueError("DATABASE_URL must name a database")
    if parsed.hostname not in LOOPBACK and query.get("sslmode") != ["verify-full"]:
        raise ValueError("remote DATABASE_URL requires sslmode=verify-full")


class PostgresDatabase:
    """Connection pool, transactions and migrations for one PostgreSQL schema."""

    def __init__(
        self,
        url: str,
        *,
        schema: str = "optimizer",
        password: str | None = None,
        credentials=None,
        pool_size: int = 4,
        connect_timeout: int = 5,
        lock_timeout_ms: int = 10_000,
        statement_timeout_ms: int = 30_000,
    ):
        validate_database_url(url)
        validate_schema(schema)
        if not 0 <= pool_size <= 64:
            raise ValueError("pool_size must be 0–64")
        parsed = urlsplit(url)
        self.url, self.schema = url, schema
        if password and credentials is not None:
            raise ValueError("pass a password or rotating credentials, not both")
        self._password, self._credentials = password or None, credentials
        self.identity = (
            f"postgresql://{parsed.hostname}:{parsed.port or 5432}"
            f"/{parsed.path.strip('/')}?schema={schema}"
        )
        self.pool_size, self.connect_timeout = pool_size, connect_timeout
        self.lock_timeout_ms, self.statement_timeout_ms = lock_timeout_ms, statement_timeout_ms
        # One advisory lock per schema serializes writers, like SQLite's database lock.
        self.lock_key = int.from_bytes(
            hashlib.sha256(b"compiler-optimizer:" + schema.encode()).digest()[:8],
            "big",
            signed=True,
        )
        self._idle, self._mutex, self._pid = [], threading.Lock(), os.getpid()

    def __repr__(self):
        return f"PostgresDatabase({self.identity!r})"

    def __str__(self):
        return self.identity

    # Connections -----------------------------------------------------------------

    def _connect(self):
        import psycopg
        from psycopg import ClientCursor
        from psycopg.adapt import Dumper

        class BooleanAsInteger(Dumper):
            oid = psycopg.adapters.types["int8"].oid

            def dump(self, obj):
                return b"1" if obj else b"0"

            def quote(self, obj):
                return self.dump(obj)

        def connect(secret):
            login = {"password": self._password}
            if secret is not None:
                login = {"password": secret["password"]}
                if secret["username"]:
                    login["user"] = secret["username"]
            return psycopg.connect(
                self.url,
                **login,
                autocommit=True,
                connect_timeout=self.connect_timeout,
                cursor_factory=ClientCursor,
                row_factory=_row_factory,
                application_name="compiler-optimizer",
                options=f"-c search_path={self.schema}",
            )

        if self._credentials is None:
            connection = connect(None)
        else:
            try:
                connection = connect(self._credentials.value())
            except psycopg.OperationalError as error:
                if not _authentication_failed(error):
                    raise
                # The secret rotated since it was cached; retry once with the new value.
                connection = connect(self._credentials.refresh())
        connection.adapters.register_dumper(bool, BooleanAsInteger)
        return connection

    def _checkout(self):
        """Return ``(connection, pooled)``; pooled connections may have died while idle."""
        with self._mutex:
            if self._pid != os.getpid():  # Never share inherited sockets across a fork.
                self._idle, self._pid = [], os.getpid()
            while self._idle:
                connection = self._idle.pop()
                if not connection.closed:
                    return connection, True
        return self._connect(), False

    def _begin(self, write):
        """Open a transaction, replacing idle connections a restart or failover closed.

        Retrying is safe only here: nothing has executed in the transaction yet.
        """
        import psycopg

        connection, pooled = self._checkout()
        while True:
            try:
                if write:
                    connection.execute("BEGIN ISOLATION LEVEL READ COMMITTED")
                else:
                    connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
                return connection
            except psycopg.OperationalError:
                connection.close()
                if not pooled:
                    raise
                connection, pooled = self._connect(), False

    def _checkin(self, connection):
        import psycopg

        healthy = (
            not connection.closed
            and not connection.broken
            and connection.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        )
        with self._mutex:
            if healthy and self._pid == os.getpid() and len(self._idle) < self.pool_size:
                self._idle.append(connection)
                return
        connection.close()

    def close(self):
        with self._mutex:
            idle, self._idle = self._idle, []
        for connection in idle:
            connection.close()

    # Transactions ----------------------------------------------------------------

    @contextmanager
    def transaction(self, *, write=True):
        psycopg = _driver()
        try:
            connection = self._begin(write)
        except (psycopg.Error, SecretUnavailable) as error:
            raise UnavailableError(f"database unavailable: {self.identity}") from error
        try:
            connection.execute(f"SET LOCAL statement_timeout = {int(self.statement_timeout_ms)}")
            connection.execute(f"SET LOCAL lock_timeout = {int(self.lock_timeout_ms)}")
            if write:
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (self.lock_key,))
            yield _Session(connection)
            connection.execute("COMMIT")
        except BaseException as error:
            self._rollback(connection)
            if isinstance(error, psycopg.errors.IntegrityError):
                raise ConflictError(str(error)) from error
            if isinstance(error, psycopg.Error):
                raise UnavailableError(f"database operation failed: {error}") from error
            raise
        finally:
            self._checkin(connection)

    @staticmethod
    def _rollback(connection):
        import psycopg

        if connection.closed or connection.broken:
            return
        try:
            connection.execute("ROLLBACK")
        except psycopg.Error:
            connection.close()

    # Schema ----------------------------------------------------------------------

    def migrate(self) -> int:
        """Create the schema at the current version or verify an existing one."""
        with self.transaction() as db:
            db.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
            db.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version BIGINT PRIMARY KEY, applied_at_unix_ms BIGINT NOT NULL)"
            )
            version = db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            if version is not None and version not in (BASELINE_VERSION, *MIGRATIONS):
                raise StateError(f"unsupported database schema version: {version}")
            steps = [] if version is not None else [(BASELINE_VERSION, "postgres.sql")]
            steps += sorted(item for item in MIGRATIONS.items() if item[0] > (version or 0))
            for target, name in steps:
                script = files("compiler_optimizer.storage").joinpath(name).read_text()
                for statement in script.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute(
                    "INSERT INTO schema_version VALUES (?, ?)",
                    (target, time.time_ns() // 1_000_000),
                )
                version = target
        return version

    def check(self) -> None:
        with self.transaction(write=False) as db:
            db.execute("SELECT 1 FROM schema_version LIMIT 1").fetchone()
