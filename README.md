# CMPE273_Group11

## Compiler Optimizer

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
