# Action P: expanded native CPU comparison

Recorded November 26, 2026 on a native ARM64 Docker engine. Three independent
seeded trials covered C17 reduction, C17 histogram and C++17 sorting, each with
250,000 benchmark integers. Four configurations produced 36 results: original,
single-generator, multi-generator and debate-enabled. Backends are frozen
handwritten rules; external model calls and tokens are exactly zero.

Open [the report](report.html) for individual results and 95% descriptive
trial-block bootstrap intervals. [protocol.json](protocol.json) freezes the
schedule, seeds, corpus/held-out hashes, budgets and policies before measurement.
[report.json](report.json), [summary.csv](summary.csv) and
[uncertainty.csv](uncertainty.csv) expose the underlying reported values.

Across the nine family/trial cases, equal-family mean speedup was 1.124x for
single, 1.015x for multi and 1.121x for debate. Multi's mean peak-RSS change was
-26.98%; single and debate retained baseline RSS. These averages include retained
originals as 1x and 0% change. Multi selected a candidate in 6/9 cases; single and
debate selected in 3/9 each. All sorting cases selected; C-family improvements
were intermittent. This describes these candidate catalogs on this shared host.
It does not establish general agent or language-model superiority.

All 63 generated proposals passed verification, and every final Python-oracle
held-out check passed. No correctness failures or retry attempts were observed.
Every optimizer stayed within three slots, 256 sandbox calls and 1,800 reserved
sandbox seconds. Debate's initial proposals passed their checks, so its remaining
revision slots were unused. These runs do not measure repair effectiveness after
an initial rejection. Separate debate/fault tests cover that behavior.

The intervals resample three whole trial blocks, preserving paired families and
strategies. Precision is limited; deterministic zero counts are observations on
this corpus, not estimates of universal failure risk. The source/image/CPU
identities and measurement qualification failures remain in raw evidence.

## Raw evidence and reproduction

`raw-evidence.tar.xz` contains 54 SQLite checkpoints and their content-addressed
artifacts, audits, actual benchmark pairs, cost ledgers, independent reference
samples and held-out inputs/receipts. Transient watchdog/lock files are excluded.
`events.jsonl` records structured lifecycle events. `implementation.zip` freezes
the measured implementation, bound to `source_tree_sha256` in the report;
`provenance.json` records source/configuration/package/image identities captured
for that run. Later source changes do not rewrite the frozen measurements.

Verify and extract with:

```sh
shasum -a 256 -c SHA256SUMS
tar -xJf raw-evidence.tar.xz
```

To run a fresh comparison from the project root with native images and healthy
RabbitMQ/Redis services already available:

```sh
./project evaluate --no-start --trials 3 --size 250000 --output var/evaluate/new-action-p-run
```

The output must not already exist. Hardware qualification requires native
measurement v2; the recorded image ID is in `report.json` and `provenance.json`.
Rebuilt images and different hardware are new experiments. This evidence covers
local CPU execution, not emulation, GPU execution, cloud failover or physical-host
loss. See [the protocol guide](../../../docs/evaluation.md) for full definitions.

## Validation

At delivery: 696 unit/service tests and three native Docker corpus checks passed,
plus lint, formatting and generated-contract checks. `validation.json` records
observed budget maxima and evidence-integrity checks. The frozen implementation
and its provenance hashes were verified against the saved source snapshot.
