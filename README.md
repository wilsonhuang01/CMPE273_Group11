# CMPE273_Group11
Repo for CMPE 273 group 11

# Project ideas

## 1. Distributed file system
Store and replicate files across several storage

## 2. Peer-to-peer file system
Distribute peer-uploaded files among peers, no central server

## 3. Agent-Assisted Ransomware-Resistant Distributed File System
This project develops an enterprise distributed file-storage system designed to detect ransomware-like activity and recover files safely. Files are divided into chunks, identified using cryptographic hashes, and replicated across at least three storage nodes. A metadata coordinator tracks file locations, versions, replicas, and node health.

Key features include immutable snapshots, versioned file storage, automatic replication, heartbeat-based failure detection, node recovery, and restoration from a known-good snapshot. The system monitors file activity for mass modifications, deletions, renames, unusual extensions, and encryption-like entropy changes.

An agentic AI component analyzes security events, explains suspicious behavior, classifies incident severity, and recommends actions. Using controlled tools, it can create emergency snapshots, pause replication, quarantine nodes, notify administrators, and suggest recovery points. A policy engine limits the AI’s permissions, while destructive actions require administrator approval.

The technology stack can include Python or Go, Docker Compose, REST or gRPC communication, SQLite or JSON metadata storage, and local disk volumes for node data. A smaller deployment can use AWS EC2.

Evaluation will measure upload/download throughput, replication overhead, detection time, false positives, recovery time, data loss, node-failure tolerance, and AI recommendation quality. The demonstration will simulate ransomware on test files and restore them from an immutable snapshot.

## 4. Distributed Multi-Agent Compiler Optimizer

Build a distributed system where multiple specialized AI agents collaborate to generate, debate, verify, and select compiler optimizations.

Each agent focuses on a different responsibility:

- Performance agent: proposes loop vectorization, parallelization, and loop fusion
- Memory agent: analyzes cache locality and memory access patterns
- Correctness agent: checks data dependencies and semantic equivalence
- Architecture agent: recommends optimizations for CPUs, GPUs, or RISC-V
- Critic agent: reviews and challenges optimization proposals
- Benchmark agent: measures runtime and memory improvements
- Coordinator agent: manages communication and selects the final solution

The agents communicate through distributed services using message passing. They independently propose optimizations, debate their tradeoffs, verify the generated code against the original program, benchmark the results, and reach a final consensus.

The system will include agent failure detection, retries, timeouts, versioned proposals, distributed caching, and consensus-based decision-making.

The final optimized code must pass compilation, unit tests, randomized differential testing, and performance benchmarks.

**Technologies:** Python, LLVM/MLIR, gRPC or RabbitMQ, Docker, Redis, C/C++, and CUDA.

**Research question:** Can a distributed group of specialized AI agents produce safer and more effective compiler optimizations than a single AI agent while tolerating communication delays and agent failures?

## 5. Distributed Code Vulnerability Scanner

Build a distributed system that splits a codebase across worker nodes, scans each part for security vulnerabilities in parallel, and combines the findings into one ranked report.

Each component has its own job:

- Coordinator: clones the repo, splits it into shards by file or module, and assigns work over gRPC
- Static analysis workers: run SAST tools (Bandit, SonarQube) on their shard
- Dependency worker: checks manifests (package.json, requirements.txt, go.mod) against CVE databases (OSV, NVD)
- Secrets worker: scans files and git history for leaked keys and credentials
- Aggregator: removes duplicate findings, links related issues across files, and ranks them by severity (CVSS)
- LLM triage agent: reviews each finding, flags likely false positives, and suggests a fix

Evaluation:

- Scan time as the number of workers grows (1, 2, 4, 8)
- How long it takes to recover when a worker is killed mid-scan
- Speedup from the cache on incremental rescans
- Detection accuracy on known-vulnerable repos
