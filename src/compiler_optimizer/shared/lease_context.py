"""Thread-local execution authority; it is never inherited by an unrelated worker thread."""

from contextvars import ContextVar

EXECUTION_LEASE = ContextVar("compiler_optimizer_execution_lease", default=None)

RECOVERY_SCOPE = ContextVar("compiler_optimizer_recovery_scope", default="evaluate")

MEASUREMENT_WINDOW = ContextVar("measurement_window", default=None)

# The coordinator's dispatch claim on one task; every write made under it is fenced.
DISPATCH_CLAIM = ContextVar("compiler_optimizer_dispatch_claim", default=None)
