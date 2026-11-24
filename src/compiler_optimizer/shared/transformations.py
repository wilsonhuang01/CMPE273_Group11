"""Versioned source-target declarations; never enable flags or infer alias promises."""

import re
from collections import Counter

from compiler_optimizer.shared.manifest import require
from compiler_optimizer.workers.targets import TARGETS

FEATURE_HEADERS = {"arm_neon.h": "neon", "emmintrin.h": "sse2", "xmmintrin.h": "sse2"}
BASELINE_INTRINSICS = {
    "sse2": {
        "__m128i",
        "_mm_loadu_si128",
        "_mm_storeu_si128",
        "_mm_set1_epi32",
        "_mm_add_epi32",
        "_mm_sub_epi32",
        "_mm_xor_si128",
        "_mm_and_si128",
        "_mm_or_si128",
    },
    "neon": {
        "vld1q_u32",
        "vst1q_u32",
        "vdupq_n_u32",
        "vaddq_u32",
        "vsubq_u32",
        "vmulq_u32",
        "veorq_u32",
        "vandq_u32",
        "vorrq_u32",
    },
}
LITERALS = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
FORBIDDEN = re.compile(
    r"\b(?:asm|__asm|__asm__|__attribute__|__declspec|restrict|__restrict|__restrict__|"
    r"__builtin_assume|__builtin_assume_aligned|_Pragma)\b|^[ \t]*#[ \t]*(?:pragma|define)\b",
    re.MULTILINE,
)
INTRINSICS = re.compile(
    r"\b(?:_mm\w*|__m128\w*|__m256\w*|__m512\w*|__builtin_(?:ia32|aarch64|arm)_\w+)\b|"
    r"\b(?:v(?:ld|st|add|sub|mul|dup|mov|zip|uzp|shl|shr|reinterpret|cvt|eor|and|orr)"
    r"\w*_[sufp](?:8|16|32|64))\b"
)


def parse_contract(value):
    require(
        isinstance(value, dict)
        and set(value) == {"schema_version", "mode", "target", "language", "required_features"},
        "invalid transformation contract fields",
    )
    require(
        type(value["schema_version"]) is int and value["schema_version"] == 1,
        "unsupported transformation contract version",
    )
    require(
        isinstance(value["target"], str)
        and value["target"] in TARGETS
        and value["language"] in ("c17", "cpp17")
        and value["mode"] in ("portable", "baseline-intrinsics"),
        "unsupported transformation target/language/mode",
    )
    features = value["required_features"]
    require(
        isinstance(features, list)
        and all(isinstance(f, str) for f in features)
        and len(features) == len(set(features))
        and set(features) <= set(TARGETS[value["target"]].source_features),
        "source features exceed the fixed target profile",
    )
    require(
        (not features) if value["mode"] == "portable" else bool(features),
        "transformation mode/features disagree",
    )
    return value


def code(data):
    return LITERALS.sub(
        lambda match: re.sub(r"[^\n]", " ", match.group()), data.decode("utf-8", errors="replace")
    )


def includes(data):
    text = LITERALS.sub(
        lambda match: (
            match.group()
            if match.group().startswith(('"', "'"))
            else re.sub(r"[^\n]", " ", match.group())
        ),
        data.decode(errors="replace"),
    )
    return set(
        re.findall(r'^[ \t]*#[ \t]*include[ \t]*[<"]([A-Za-z0-9_.]+)[>"]', text, re.MULTILINE)
    )


def validate_change(patch, before, after, compiler=None):
    contract = patch.get("contract")
    if contract is not None:
        parse_contract(contract)
        if compiler is not None:
            require(
                contract["target"] == compiler.target and contract["language"] == compiler.language,
                "transformation contract differs from assigned compiler target/language",
            )
    old = Counter(token for data in before.values() for token in FORBIDDEN.findall(code(data)))
    new = Counter(token for data in after.values() for token in FORBIDDEN.findall(code(data)))
    require(not new - old, "patch introduces unsupported alias/assembly/compiler assumptions")
    required = set()
    intrinsic = False
    for data in after.values():
        text = code(data)
        headers = includes(data)
        for header, feature in FEATURE_HEADERS.items():
            if header in headers:
                required.add(feature)
        intrinsic = intrinsic or bool(INTRINSICS.search(text))
        require(
            not headers.intersection({"immintrin.h", "riscv_vector.h"}),
            "unsupported target-specific source header",
        )
    if required or intrinsic:
        require(
            contract is not None and contract["mode"] == "baseline-intrinsics",
            "target-specific source requires a versioned explicit transformation contract",
        )
        require(required <= set(contract["required_features"]), "intrinsic header is not declared")
        # Recognized intrinsics must agree with the declared baseline ISA family.
        for data in after.values():
            for token in INTRINSICS.findall(code(data)):
                feature = "sse2" if token.startswith(("_mm", "__m", "__builtin_ia32")) else "neon"
                require(
                    feature in contract["required_features"], "intrinsic family/target mismatch"
                )
                require(
                    token in BASELINE_INTRINSICS[feature],
                    "intrinsic is outside the supported baseline source contract",
                )
