"""Operator-only, bounded libFuzzer policy; no submission-controlled flags."""

from dataclasses import asdict, dataclass

try:
    from compiler_optimizer.shared.manifest import require
except ImportError:

    def require(condition, message):
        if not condition:
            raise ValueError(message)


@dataclass(frozen=True)
class FuzzPolicy:
    seed: int = 273
    seconds: int = 5
    runs: int = 1000
    max_length: int = 256
    minimize_seconds: int = 2

    def validate(self):
        for key, low, high in [
            ("seed", 1, 2**31 - 1),
            ("seconds", 1, 30),
            ("runs", 1, 100000),
            ("max_length", 1, 4096),
            ("minimize_seconds", 1, 5),
        ]:
            value = getattr(self, key)
            require(type(value) is int and low <= value <= high, "invalid fuzz budget: " + key)
        return asdict(self)
