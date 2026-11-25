# LLVM IR proposals

The cohort command accepts an explicit, versioned LLVM pass recipe in place of a
source generator. This first version supports C17/C++17, including multiple
translation units, on native Linux ARM64 and x86-64 Docker workers. It uses local
agent adapters. RPC/model generation, distributed fleet transport and source
revision/debate rounds do not yet accept this proposal type. Source generation
remains the default.

From the project directory, build the native compiler image and start RabbitMQ
and Redis (`./project up` does this), then run:

```sh
AGENT_BACKEND=local python -m compiler_optimizer.coordinator.round \
  --package examples/cpu-transformations/unsigned-scale \
  --agents performance --ir-pipeline performance=scalar-v1 \
  --state var/llvm-scalar
```

The command detects the native CPU target. Use `--target linux-aarch64` or
`--target linux-x86_64` explicitly if needed. `--image` must identify the compiler
image built for that target. To resume, use the same state directory with
`--task-id TASK_UUID`; the stored cohort, pipeline and plans are authoritative.
`--ir-pipeline` may be assigned to performance, memory or architecture, and source
and IR candidates can share one cohort. Duplicate role assignments are rejected.

Two trusted recipes are defined in `workers/llvm_policy.py`:

- `scalar-v1`: `function(mem2reg,instcombine<max-iterations=1000>,simplifycfg,adce)`.
- `sroa-v1`: `function(sroa,instcombine<max-iterations=1000>,simplifycfg)`.

These are fixed recipes, not user-supplied `opt` command lines. Unknown IDs,
plugins and arbitrary flags are rejected. InstCombine has an explicit bounded
iteration limit; the process and external watchdog deadlines remain authoritative.
The pipeline follows LLVM 18's [pass-manager nesting rules](https://releases.llvm.org/18.1.1/docs/NewPassManager.html)
and [IR verification contract](https://releases.llvm.org/18.1.8/docs/Passes.html#verify-module-verifier).
Verification establishes well-formed IR, not semantic equivalence.

Each translation unit is emitted with Clang 18, the frozen target/language flags
and LLVM middle-end passes disabled. `llvm-link-18` creates baseline bitcode.
`opt-18` verifies that baseline, applies the allowlisted recipe with `-verify-each`,
and verifies the optimized bitcode. Normal and sanitizer variants are emitted
independently. Clang, opt and llvm-link must report the same exact LLVM release
inside the same content-addressed compiler image.

Every stage runs in a separate existing compiler sandbox with network isolation,
resource limits, bounded output, a process-group deadline and a host watchdog.
Stage inputs, output hashes, commands, compiler image, LLVM version, pass recipe,
profile and watchdog receipts are retained. Successful stages are cached by
immutable inputs. Baseline emission/linking can be reused across recipes; an
optimized module never overwrites baseline bitcode. Compiler failures retain
bounded diagnostics and stop proposal generation. Malformed IR and compiler
hangs cannot enter selection.

`IRPassProposal` is a typed protobuf field on both Proposal and VerificationJob.
It identifies the baseline source hash, target triple, pipeline, LLVM version,
normal/sanitizer bitcode and stage reports. For IR candidates, `source.bundle`
contains the canonical JSON serialization of this descriptor. Baseline and source
candidates continue to contain source ZIP bundles. The descriptor's content hash
is the candidate identity; its parent remains the immutable submitted source.
Artifacts are registered for task-scoped retention and download.

Verification lowers both baseline and optimized bitcode with the same pinned
Clang settings, validates the native executable's ELF architecture, and applies
the existing supplied/generated differential tests and sanitizer checks. The
correctness report binds every executable to its exact input bitcode and profile.
The critic checks the trusted pass chain, and both mandatory review roles remain
required. Benchmarking reuses the exact tested normal executables; selection
retains the existing weighted measurements, reliability thresholds and baseline
fallback. A selected IR result includes `selected_representation: llvm-ir-v1` and
`selected_ir`; `selected_source` downloads its descriptor, whose artifact refs
identify the bitcode and evidence. The download client writes `selected-ir.json`
and an `ir/` directory containing normal/sanitizer bitcode and stage reports.
No performance gain is assumed from an IR change.

Run focused checks with:

```sh
python -m pytest tests/test_llvm_ir.py
LLVM_INTEGRATION_TESTS=1 WORKER_TARGET=linux-aarch64 \
  COMPILER_IMAGE=cmpe273-arm:18 python -m pytest tests/test_llvm_ir_docker.py
```

On native amd64 use `WORKER_TARGET=linux-x86_64` and
`COMPILER_IMAGE=cmpe273-clang:18`. The Docker suite exercises a full native cohort,
multiple C++ translation units, both recipes, malformed IR, and an actual compiler
deadline. It requests AddressSanitizer for the C++ case on native amd64; ARM uses
UBSan. Unit tests cover recipe/identity tampering, stage-chain substitutions,
immutable cache reuse, lowering-input binding, selection and resumption.
