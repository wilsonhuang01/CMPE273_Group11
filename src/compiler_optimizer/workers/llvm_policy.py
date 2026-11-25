"""Trusted LLVM 18 pass recipes, shared by the host and isolated supervisor."""

PIPELINES = {
    "scalar-v1": "function(mem2reg,instcombine<max-iterations=1000>,simplifycfg,adce)",
    "sroa-v1": "function(sroa,instcombine<max-iterations=1000>,simplifycfg)",
}
TRIPLES = {
    "linux-x86_64": "x86_64-unknown-linux-gnu",
    "linux-aarch64": "aarch64-unknown-linux-gnu",
}
STAGES = {"emit", "link", "verify", "optimize", "lower"}
