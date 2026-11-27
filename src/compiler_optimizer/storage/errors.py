"""Storage failures callers can distinguish from transport failures."""


class StorageError(Exception):
    """Base storage error."""


class NotFoundError(StorageError):
    """A required task, message, or artifact is missing."""


class ConflictError(StorageError):
    """An identity was reused or a concurrent update won."""


class StateError(StorageError):
    """An operation is invalid in the current task state."""


class ArtifactError(StorageError):
    """An artifact is unsafe, oversized, or fails integrity verification."""
