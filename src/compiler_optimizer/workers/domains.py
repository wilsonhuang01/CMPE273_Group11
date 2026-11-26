"""Versioned bounded stdin contracts and independent Python output oracles."""

import math
import random
from dataclasses import asdict, dataclass

from compiler_optimizer.shared.manifest import require

KINDS = ("sort", "search", "matrix", "reverse", "dot", "norm")


@dataclass(frozen=True)
class DomainGenerator:
    kind: str
    seed: int = 273
    cases: int = 8
    max_size: int = 8
    bound: int = 100

    @classmethod
    def parse(cls, value):
        require(
            isinstance(value, dict) and set(value) == set(asdict(cls("sort"))),
            "invalid domain fields",
        )
        require(value["kind"] in KINDS, "unsupported oracle domain")
        for key, low, high in [
            ("seed", 0, 2**32 - 1),
            ("cases", 1, 32),
            ("max_size", 1, 16),
            ("bound", 1, 1000),
        ]:
            require(
                type(value[key]) is int and low <= value[key] <= high, "domain bound out of range"
            )
        return cls(**value)

    def generate(self):
        rng = random.Random(self.seed)
        for i in range(self.cases):
            n = i if i < 2 else self.max_size if i in (2, 3) else rng.randint(0, self.max_size)

            def values(count, case_index=i):
                if case_index == 2:
                    return [self.bound if k % 2 else -self.bound for k in range(count)]
                if case_index == 3:
                    return [0] * count
                return [rng.randint(-self.bound, self.bound) for _ in range(count)]

            a = values(n)
            if self.kind in ("dot", "norm"):
                a = [v / 8 for v in a]
            tokens = [n, *a]
            if self.kind == "sort":
                result = sorted(a)
            elif self.kind == "search":
                a.sort()
                target = rng.choice(a) if a and i % 2 else self.bound + 1
                tokens = [n, *a, target]
                result = [a.index(target) if target in a else -1]
            elif self.kind == "matrix":
                # Square matrix product; arbitrary precision integers are the oracle.
                a, b = values(n * n), values(n * n)
                tokens = [n, *a, *b]
                result = [
                    sum(a[r * n + k] * b[k * n + c] for k in range(n))
                    for r in range(n)
                    for c in range(n)
                ]
            elif self.kind == "reverse":
                s = "".join(rng.choice("abcXYZ019") for _ in range(n))
                yield (s + "\n").encode(), (s[::-1] + "\n").encode()
                continue
            elif self.kind == "dot":
                b = [v / 8 for v in values(n)]
                tokens = [n, *a, *b]
                result = [math.fsum(float(x) * y for x, y in zip(a, b, strict=True))]
            else:
                result = [math.sqrt(math.fsum(float(x) * x for x in a))]
            yield (
                (" ".join(map(str, tokens)) + "\n").encode(),
                (
                    " ".join(format(x, ".17g") if isinstance(x, float) else str(x) for x in result)
                    + "\n"
                ).encode(),
            )
