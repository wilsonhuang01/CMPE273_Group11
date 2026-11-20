# Recorded native ARM64 run — 2026-11-20 UTC

Open [report.html](report.html) for original/single/multi comparison and recovery
outcomes. [report.json](report.json) contains all candidate observations, selection
audits, costs and limitations. [summary.csv](summary.csv) is the portable comparison;
[demo.json](demo.json) records the four actual demo scenarios.

The multi-generator arm selected streaming reduction on 750,000 integers:
1.0572x paired-median speedup, 80% lower median peak RSS (7.03125 to 1.40625 MiB).
All comparison proposals passed correctness; no correctness failures were observed.
On 250,000 integers both arms retained the original. Single retained the original
on both sizes. Each optimizer used exactly 180 sandbox invocations and 723 reserved
sandbox seconds per size, below equal caps of 256 / 1,800. Actual optimization costs
were 65–77 seconds per arm/workload; these are distinct from the reserved budget.

The four-scenario demo passed: accepted optimization, compiler disconnect with
successful retry on a different worker, two-worker loop quarantine, and output
mismatch rejection. The last two retain the original. Full worker IDs, retry
statuses, timings and blacklist receipt are in demo.json. The compiler disconnect
was deliberately injected at the runner boundary; no host failure is claimed.

`implementation.zip` preserves the package code that produced the comparison.
Its Python-tree hash matches the report. Subsequent release changes add automatic
source snapshots, cross-arm environment validation, packaging and reporting tests;
the frozen implementation remains intact. A post-run check confirmed equal
engine/boot/CPU/target/native identity across every candidate in both arms.

Full raw SQLite/CAS state, cost ledgers and audit artifacts reside in local
`var/evaluate/step16-native-comparison` and `var/demo/step16-native-release` and are
included in the packaged release's `evidence/` directory. The report snapshots here
omit those larger artifacts. Reproduce with `./project evaluate`; do not expect
identical timing on another host. These two sizes/one trial of a handwritten
reduction optimizer do not establish general strategy superiority. See
[protocol](../../../docs/evaluation.md) for caps, sampling, definitions and caveats.
