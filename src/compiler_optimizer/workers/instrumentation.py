"""Trusted compiler profiles and sanitizer runtime settings (no submission overrides)."""

BUILD_PROFILES = {
    "normal": [],
    "microbenchmark": ["-DCO_BENCHMARK=1"],
    "fuzzer": [
        "-DCO_FUZZ=1",
        "-fsanitize=fuzzer,undefined,bounds",
        "-fno-sanitize-recover=all",
        "-fprofile-instr-generate",
        "-fcoverage-mapping",
        "-gline-tables-only",
    ],
    "undefined": [
        "-fsanitize=undefined,bounds",
        "-fno-sanitize-recover=all",
        "-fno-omit-frame-pointer",
        "-gline-tables-only",
    ],
    "address-undefined": [
        "-fsanitize=address,undefined,bounds",
        "-fno-sanitize-recover=all",
        "-fno-omit-frame-pointer",
        "-gline-tables-only",
    ],
}
SANITIZER_ENV = {
    "UBSAN_OPTIONS": "halt_on_error=1:print_stacktrace=0:symbolize=0",
    # LeakSanitizer's ptrace/fork behavior is outside the single-process sandbox.
    "ASAN_OPTIONS": "detect_leaks=0:halt_on_error=1:abort_on_error=0:symbolize=0",
}
