# Proposal correctness verification

The verifier compiles a stored proposal, compares it against the original program
and expected outputs, and checks instrumented builds before allowing measurements.
Each run stores an immutable verification plan, exact generated inputs, binaries,
invocation logs, and a correctness report. This provides evidence for the tested
inputs; it is not a proof of general semantic equivalence or memory safety.

## Run the example

Activate the development environment and build the updated toolchain. Sanitizer
runtimes change the image ID, so create a fresh baseline using this image:

```sh
docker compose --profile toolchain build compiler
python -m compiler_optimizer.workers.baseline examples/sum-array --state var/verification
```

Copy the printed `task_id`, replacing `TASK_ID` in:

```sh
python -m compiler_optimizer.workers.verify TASK_ID \
  --candidate examples/sum-array-optimized \
  --plan examples/sum-array.verification.json \
  --state var/verification
```

The source-only candidate directory must contain the original translation-unit
paths and optional local `.h`/`.hpp` headers. It cannot contain a manifest, fixtures,
test scripts, or build configuration. Headers cannot overwrite fixture paths.
The CLI registers a versioned proposal against the task's original source; an
existing stored proposal can instead be checked with `--proposal-id PROPOSAL_ID`.
The library API `run_verification(repository, task_id, proposal_id, runner, plan)`
also supports proposals derived from later parent versions.

The command prints task/proposal/job IDs, verification status, failure reason,
and report hash. Exit code 0 means verified, 1 means a persisted failed verification,
and 2 means an invalid request or missing prerequisite. A failure reason of
`INFRASTRUCTURE_ERROR` identifies toolchain/worker trouble separately from a
program correctness failure. A passing result leaves the task running and does
not automatically select an optimization.

Baseline, proposal, and instrumented builds must use the same immutable compiler
image/version and base flags. Use `--image` to select the baseline's retained image
by ID. Changing image tags does not authorize changing a stored task's toolchain.
The command uses local `--state` storage, separate from Compose's named volume.
The standalone verifier does not dispatch proposals. The [host cohort workflow](coordinator-selection.md)
implements generation, scoped broker delivery, reviews, benchmarking and recovery.

## Correctness gates

1. Require a running task and an already-passing baseline. Verify the stored
   package/source hashes, manifest, compiler settings, and proposal ownership.
2. Reconstruct bounded regular ZIP_STORED snapshots in memory, without extracting
   files onto the host. Candidate files cannot replace the original fixtures.
3. Persist the operator's plan and every input/argv/expected-output byte before
   running a verification job. Generated cases have distinct recorded IDs.
4. Compile normal baseline and candidate binaries in the hardened sandbox.
5. For every fixed and generated input, execute the baseline and candidate in
   separate fresh containers. Check supplied expected outputs where available,
   and compare the candidate directly with the fresh baseline reference.
6. Compile both programs with the chosen fixed sanitizer profile and rerun every
   case. Instrumented outputs must match the normal reference too.
7. Only after every correctness/sanitizer check passes, run the original selected
   benchmark cases on the normal candidate binary. Recheck their output, exit
   status, and resource limits. These are provisional single samples;
   repeated/statistically controlled [benchmarking](benchmarking.md) is required
   separately before a candidate can be selected.

Any candidate compile/test/sanitizer/resource failure disqualifies that candidate
and leaves the task available for another proposal. If fresh reference execution
or baseline sanitization fails, the result and task invalidation are committed
atomically: the task becomes failed and cannot select an optimization against an
unsafe reference. Worker/daemon failures leave the task running but do not verify
the candidate. No failing verification can produce a successful selected result.

## Trusted verification plan

Omitting `--plan` selects exact-byte output comparison, mandatory UBSan/bounds
checks, and supplied inputs only. The full plan has exactly these fields:

```json
{
  "schema_version": 1,
  "comparison": {"mode": "exact"},
  "sanitizer": "undefined",
  "generator": null
}
```

Plans reject unknown/duplicate/missing fields, invalid types, and nonfinite
tolerances. They are selected by the trusted operator, outside candidate source.
They cannot request shell commands, compiler flags, environment overrides, or
executable input generators.

Exact mode includes all whitespace and trailing newlines. Numeric-only stdout may
opt into an explicit comparator:

```json
{
  "mode": "numeric",
  "absolute_tolerance": 1e-9,
  "relative_tolerance": 1e-6
}
```

Replace the `comparison` object with this value. It requires the same nonempty
number of whitespace-delimited finite decimal/scientific numeric tokens. For each
actual/reference pair, require
`abs(actual-reference) <= absolute_tolerance + relative_tolerance*abs(reference)`.
NaN, infinity, textual labels, extra tokens, and nonnumeric formatting are rejected.
Tolerances must be explicit, finite, and nonnegative; relative tolerance is at most
1 and absolute tolerance at most 1e12. Initial baseline intake still checks its
supplied fixtures byte for byte; the numeric policy applies to proposal verification.

## Randomized differential testing and structured fuzzing

The [sample plan](../examples/sum-array.verification.json) enables the trusted
`integer_array` generator: a count followed by that many bounded signed integers.
Its seed, random/fuzz case counts, length bounds, value bounds, and argv are all
explicit. The initial implementation allows 1–32 generated cases, lengths 0–1024,
and values within -1000000 to 1000000. Inputs must fit the submitted program's valid
domain; the verifier cannot infer arbitrary C/C++ input preconditions.

Random cases use a seeded PRNG. Fuzz cases include domain boundaries, then mutate
valid corpus values/lengths while recomputing counts and retaining bounds. Suitable
supplied fixtures seed the corpus. Every generated input is saved as exact bytes,
so replay does not depend solely on a particular Python PRNG version. Candidate
outputs use the fresh original program as their oracle.

This integer-array generator uses structured mutations rather than coverage feedback.
The separate, opt-in sort-library libFuzzer adapter and additional independent domain
oracles are described in [correctness-fuzzing.md](correctness-fuzzing.md). The
maximum total case count is 132 including the original 100 supplied tests.
Generated IDs cannot collide with supplied IDs.

## Sanitizer profiles and isolation

- `undefined` (default): `-fsanitize=undefined,bounds` with recovery disabled,
  frame pointers, and line-table diagnostics.
- `address-undefined`: also enables AddressSanitizer. This profile requires a
  native Linux amd64 Docker worker; it is rejected on native ARM64, emulated
  x86-64 and RISC-V profiles rather than silently dropping checks.

Instrumentation flags are owned by the worker and logged with each actual compiler
command. They are never used for performance samples. UBSan halts on a finding;
symbolizer subprocesses are disabled. ASan leak detection is disabled because
LeakSanitizer's fork/ptrace behavior is outside the single-process sandbox. An
instrumented runtime finding has the distinct `SANITIZER_FAILURE` reason and
preserves its diagnostic/exit evidence. Recognized shadow-memory initialization
failures are infrastructure failures; other abnormal exits retain their exit/signal
evidence. Such failures never produce a partly verified success.

All executions retain the [sandbox policy and independent watchdog](sandbox.md).
The syscall profile additionally allows only personality queries, the normal Linux
personality, and disabling ASLR for the current process for native sanitizer
startup; it does not modify host sysctls. Aggregate invocation-log evidence is
capped at 64 MiB. A worker that cannot complete the requested checks returns a
failure rather than accepting a partly checked candidate.

UBSan catches supported undefined behavior; it does not replace ASan's heap-memory
checks. Neither profile detects every possible bug. The optional native ASan Docker
test is skipped on ARM-emulated development hosts and runs in native Linux CI.

## Evidence, compatibility, and selection

`VerificationJob` now carries `additional_test_ids`, `verification_plan`, and
`verification_inputs` artifact references. `VerificationResult` carries a
`correctness_report` reference, and its success IDs cover every fixed/generated
case. Benchmark IDs continue to match only the original manifest's selected cases.
The new `SANITIZER_FAILURE` enum requires updated receivers.

Reports bind task/job/proposal IDs, baseline/candidate source hashes, compiler
image, plan/input hashes, sanitizer profile, and invocation-log hash. Successful
reports must include all normal/instrumented build evidence and complete
differential/sanitizer coverage. The repository validates those bindings before
accepting a result and again before selecting a proposal. Missing, swapped, or
incomplete evidence cannot be selected. Historical plan-less jobs remain readable;
their proposal results need new verification before final selection.
Candidate selection also requires the latest [benchmark report](benchmarking.md)
to demonstrate a reliable gain; correctness evidence alone is insufficient.

Inspect results with `Repository.list_jobs`, `list_results`, and `list_artifacts`.
Correctness inputs/plans/reports/instrumented binaries use the `correctness`
artifact kind; normal binaries and logs keep their existing kinds. A report's
`correctness_passed` becomes true only for an entirely successful result. Logs
include phase/case IDs, build profiles, compiler commands, bounded stdout/stderr,
timing, memory, and watchdog confirmation.

## Checks

```sh
python -m pytest tests/test_verification.py
BASELINE_DOCKER_TESTS=1 python -m pytest tests/test_verification_docker.py
```

Tests cover C/C++ success, a candidate that passes fixed tests but fails generated
inputs, sanitizer-detected overflow, explicit floating tolerances, unsafe reference
invalidation, evidence tampering, and selection eligibility. Compiler behavior and
instrumentation settings follow the [Clang UBSan](https://clang.llvm.org/docs/UndefinedBehaviorSanitizer.html)
and [Clang ASan](https://clang.llvm.org/docs/AddressSanitizer.html) documentation.
