FROM ubuntu:24.04
ARG EXECUTION_TARGET=linux-x86_64
LABEL compiler-optimizer.target="${EXECUTION_TARGET}"
LABEL compiler-optimizer.policy-version="1"
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
        clang-18 llvm-18 mlir-18-tools python3 libc6-dev libstdc++-13-dev binutils libseccomp2 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /work/src /work/build /work/run /opt/compiler-worker
COPY src/compiler_optimizer/workers/supervisor.py /opt/compiler-worker/supervisor.py
COPY src/compiler_optimizer/workers/policy.py /opt/compiler-worker/policy.py
COPY src/compiler_optimizer/workers/instrumentation.py /opt/compiler-worker/instrumentation.py
COPY src/compiler_optimizer/workers/llvm_policy.py /opt/compiler-worker/
COPY src/compiler_optimizer/workers/mlir_policy.py /opt/compiler-worker/
COPY src/compiler_optimizer/workers/targets.py /opt/compiler-worker/targets.py
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends libclang-rt-18-dev \
    && rm -rf /var/lib/apt/lists/*
LABEL compiler-optimizer.verification-version="1"
COPY docker/measure.c /opt/compiler-worker/measure.c
RUN clang-18 -O2 -Wall -Wextra /opt/compiler-worker/measure.c -o /opt/compiler-worker/measure \
    && rm /opt/compiler-worker/measure.c
LABEL compiler-optimizer.measurement-version="2"
# The trusted supervisor drops the compiler/program to UID/GID 10001.
ENTRYPOINT ["python3", "/opt/compiler-worker/supervisor.py"]
COPY src/compiler_optimizer/workers/fuzz_policy.py src/compiler_optimizer/workers/fuzz_runtime.py /opt/compiler-worker/
