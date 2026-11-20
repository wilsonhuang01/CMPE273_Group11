# Reproducible optimizer evaluation

Run `./project evaluate`. The frozen workload generator,
three-candidate single/multi catalogs, budgets and reporting code live in
`src/compiler_optimizer/evaluation/`. See [the evaluation protocol](../docs/evaluation.md)
for definitions, limitations and reproduction commands.

[Recorded local results](results/step16/report.html) compare original,
single-generator and multi-generator optimization using real native ARM64
measurements. JSON/CSV snapshots and the recovery demo are in that directory.
Full checkpoint/CAS evidence is included in the local release bundle when packaged
with `--evidence`; generated runs themselves live under ignored `var/` directories.
The tiny sum-array example remains a correctness fixture, not a performance claim.
