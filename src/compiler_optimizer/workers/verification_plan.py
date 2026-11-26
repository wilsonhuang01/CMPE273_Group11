"""Strict operator policy, numeric comparison, and valid structured input generation."""

import base64
import json
import math
import random
import re
from dataclasses import asdict, dataclass

from compiler_optimizer.shared.manifest import ContractError, require
from compiler_optimizer.workers.domains import DomainGenerator
from compiler_optimizer.workers.fuzz_policy import FuzzPolicy


def _object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate verification-plan key: {key}")
        result[key] = value
    return result


def _fields(value, expected):
    require(
        isinstance(value, dict) and set(value) == set(expected),
        "verification policy has missing/unknown fields",
    )


def _integer(value, low, high):
    require(type(value) is int and low <= value <= high, "verification policy integer out of range")


@dataclass(frozen=True)
class ArrayGenerator:
    seed: int
    random_cases: int
    fuzz_cases: int
    min_length: int
    max_length: int
    min_value: int
    max_value: int
    args: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerificationPlan:
    comparison: str = "exact"
    absolute_tolerance: float = 0.0
    relative_tolerance: float = 0.0
    sanitizer: str = "undefined"
    generator: ArrayGenerator | DomainGenerator | None = None

    coverage: FuzzPolicy | None = None

    @classmethod
    def parse(cls, data):
        if isinstance(data, bytes | str):
            try:
                require(len(data) <= 65536, "verification plan exceeds 64 KiB")
                data = json.loads(data, object_pairs_hook=_object)
            except (ValueError, UnicodeError, RecursionError) as error:
                raise ContractError("invalid verification plan JSON") from error
        require(isinstance(data, dict), "verification plan must be an object")
        coverage = data.get("coverage")
        if "coverage" in data:
            require(
                isinstance(coverage, dict) and set(coverage) == set(asdict(FuzzPolicy())),
                "invalid coverage policy",
            )
            coverage = FuzzPolicy(**coverage)
            coverage.validate()
        _fields(
            data,
            {"schema_version", "comparison", "sanitizer", "generator"}
            | ({"coverage"} if coverage is not None else set()),
        )
        _integer(data["schema_version"], 1, 1)
        require(data["sanitizer"] in ("undefined", "address-undefined"), "unsupported sanitizer")
        compare = data["comparison"]
        require(isinstance(compare, dict), "comparison must be an object")
        mode = compare.get("mode")
        require(mode in ("exact", "numeric"), "unsupported output comparator")
        _fields(
            compare,
            {"mode"} if mode == "exact" else {"mode", "absolute_tolerance", "relative_tolerance"},
        )
        absolute = compare.get("absolute_tolerance", 0.0)
        relative = compare.get("relative_tolerance", 0.0)
        for value in (absolute, relative):
            require(
                type(value) in (int, float) and 0 <= value <= 1e12 and math.isfinite(value),
                "tolerances must be finite nonnegative numbers at most 1e12",
            )
        require(relative <= 1, "relative tolerance cannot exceed 1")
        generator = data["generator"]
        require(generator is None or isinstance(generator, dict), "invalid generator")
        if generator is not None and generator.get("kind") != "integer_array":
            generator = DomainGenerator.parse(generator)
        elif generator is not None:
            _fields(
                generator,
                {
                    "kind",
                    "seed",
                    "random_cases",
                    "fuzz_cases",
                    "min_length",
                    "max_length",
                    "min_value",
                    "max_value",
                    "args",
                },
            )
            require(generator["kind"] == "integer_array", "unsupported input generator")
            for key, bounds in {
                "seed": (0, 2**32 - 1),
                "random_cases": (0, 32),
                "fuzz_cases": (0, 32),
                "min_length": (0, 1024),
                "max_length": (0, 1024),
                "min_value": (-1000000, 1000000),
                "max_value": (-1000000, 1000000),
            }.items():
                _integer(generator[key], *bounds)
            require(
                generator["min_length"] <= generator["max_length"]
                and generator["min_value"] <= generator["max_value"],
                "invalid generator domain",
            )
            require(
                0 < generator["random_cases"] + generator["fuzz_cases"] <= 32,
                "generator must request 1–32 total cases",
            )
            args = generator["args"]
            require(isinstance(args, list) and len(args) <= 32, "invalid generator argv")
            try:
                require(
                    all(
                        isinstance(arg, str) and "\0" not in arg and len(arg.encode()) <= 1024
                        for arg in args
                    ),
                    "invalid generator argv",
                )
            except UnicodeError as error:
                raise ContractError("generator argv must be UTF-8") from error
            generator = ArrayGenerator(
                **{key: value for key, value in generator.items() if key not in ("kind", "args")},
                args=tuple(args),
            )
        return cls(mode, float(absolute), float(relative), data["sanitizer"], generator, coverage)

    def as_dict(self):
        comparison = {"mode": self.comparison}
        if self.comparison == "numeric":
            comparison.update(
                absolute_tolerance=self.absolute_tolerance,
                relative_tolerance=self.relative_tolerance,
            )
        return {
            **({"coverage": asdict(self.coverage)} if self.coverage is not None else {}),
            "schema_version": 1,
            "comparison": comparison,
            "sanitizer": self.sanitizer,
            "generator": None
            if self.generator is None
            else asdict(self.generator)
            if isinstance(self.generator, DomainGenerator)
            else {
                "kind": "integer_array",
                **asdict(self.generator),
                "args": list(self.generator.args),
            },
        }

    def encode(self):
        # Revalidate direct dataclass construction too, before persisting/running anything.
        data = json.dumps(self.as_dict(), sort_keys=True, allow_nan=False).encode()
        self.parse(data)
        return data

    def matches(self, actual: bytes, reference: bytes) -> bool:
        if self.comparison == "exact":
            return actual == reference
        left, right = actual.split(), reference.split()
        if not left or len(left) != len(right):
            return False
        pattern = rb"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
        for a, b in zip(left, right, strict=True):
            if not re.fullmatch(pattern, a) or not re.fullmatch(pattern, b):
                return False
            x, y = float(a), float(b)
            if not math.isfinite(x) or not math.isfinite(y):
                return False
            if abs(x - y) > self.absolute_tolerance + self.relative_tolerance * abs(y):
                return False
        return True


@dataclass(frozen=True)
class InputCase:
    id: str
    stdin: bytes
    args: tuple[str, ...]
    expected: bytes | None
    kind: str = "supplied"

    def record(self):
        return {
            "id": self.id,
            "kind": self.kind,
            "args": list(self.args),
            "stdin_base64": base64.b64encode(self.stdin).decode(),
            "expected_stdout_base64": None
            if self.expected is None
            else base64.b64encode(self.expected).decode(),
        }


def generate_cases(package, plan):
    cases = [
        InputCase(
            case.id,
            package.files[case.stdin],
            tuple(case.args),
            package.files[case.expected_stdout],
        )
        for case in package.manifest.tests
    ]
    domain = plan.generator
    if domain is None:
        return cases
    if isinstance(domain, DomainGenerator):
        for i, (stdin, expected) in enumerate(domain.generate()):
            cases.append(InputCase(f"oracle-{domain.kind}-{i:04d}", stdin, (), expected, "oracle"))
        require(len({c.id for c in cases}) == len(cases), "generated IDs collide")
        return cases
    rng = random.Random(domain.seed)
    corpus = []
    for case in cases:
        try:
            values = [int(token) for token in case.stdin.split()]
            if (
                values
                and values[0] == len(values) - 1
                and domain.min_length <= values[0] <= domain.max_length
                and all(domain.min_value <= value <= domain.max_value for value in values[1:])
            ):
                corpus.append(values[1:])
        except ValueError:
            pass

    def append(kind, index, values):
        body = (str(len(values)) + "\n" + " ".join(map(str, values)) + "\n").encode()
        cases.append(InputCase(f"{kind}-{index:04d}", body, domain.args, None, kind))
        corpus.append(values)

    for index in range(domain.random_cases):
        append(
            "random",
            index,
            [
                rng.randint(domain.min_value, domain.max_value)
                for _ in range(rng.randint(domain.min_length, domain.max_length))
            ],
        )
    for index in range(domain.fuzz_cases):
        if index == 0:
            values = [max(domain.min_value, min(0, domain.max_value))] * domain.min_length
        elif index in (1, 2):
            values = [domain.max_value if index == 1 else domain.min_value] * domain.max_length
        else:
            values = list(rng.choice(corpus))
            if values:
                values[rng.randrange(len(values))] = rng.choice(
                    [
                        domain.min_value,
                        domain.max_value,
                        rng.randint(domain.min_value, domain.max_value),
                    ]
                )
            if len(values) < domain.max_length and rng.choice([True, False]):
                values.append(rng.randint(domain.min_value, domain.max_value))
            elif len(values) > domain.min_length:
                values.pop(rng.randrange(len(values)))
        append("fuzz", index, values)
    require(
        len({case.id for case in cases}) == len(cases), "generated IDs collide with supplied tests"
    )
    return cases
