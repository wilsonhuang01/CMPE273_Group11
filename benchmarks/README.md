# Reproducible optimizer evaluation

Run `./project evaluate`. The frozen workload generator,
three-family single/multi/debate catalogs, budgets and reporting code live in
`src/compiler_optimizer/evaluation/`. See [the evaluation protocol](../docs/evaluation.md)
for definitions, limitations and reproduction commands.

[Action P native results](results/action-p/report.html) compare original,
single-generator, multi-generator and debate-enabled configurations across three
C/C++ families and three trials. The [evidence bundle](results/action-p/README.md)
includes the frozen protocol, implementation, raw SQLite/CAS evidence and
trial-block uncertainty. Backends are deterministic rules with zero model calls.

[Legacy local results](results/step16/report.html) compare original,
single-generator and multi-generator optimization using real native ARM64
measurements. JSON/CSV snapshots and the recovery demo are in that directory.
Full checkpoint/CAS evidence is included in the local release bundle when packaged
with `--evidence`; generated runs themselves live under ignored `var/` directories.
The tiny sum-array example remains a correctness fixture, not a performance claim.
