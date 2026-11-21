"""Thread-local execution authority; it is never inherited by an unrelated worker thread."""

from contextvars import ContextVar

EXECUTION_LEASE = ContextVar("compiler_optimizer_execution_lease", default=None)

RECOVERY_SCOPE = ContextVar("compiler_optimizer_recovery_scope", default="evaluate")
