FROM ubuntu:24.04
LABEL compiler-optimizer.policy-version="1" \
      compiler-optimizer.verification-version="1" \
      compiler-optimizer.target="linux-riscv64"
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
       clang-18 llvm-18 python3 qemu-user gcc-riscv64-linux-gnu g++-riscv64-linux-gnu \
       libc6-dev-riscv64-cross binutils-riscv64-linux-gnu libseccomp2 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /work/src /work/build /work/run /opt/compiler-worker
COPY src/compiler_optimizer/workers/supervisor.py /opt/compiler-worker/supervisor.py
COPY src/compiler_optimizer/workers/policy.py /opt/compiler-worker/policy.py
COPY src/compiler_optimizer/workers/instrumentation.py /opt/compiler-worker/instrumentation.py
COPY src/compiler_optimizer/workers/targets.py /opt/compiler-worker/targets.py
COPY src/compiler_optimizer/workers/emulator_policy.py /opt/compiler-worker/emulator_policy.py
COPY docker/riscv-smoke.c /opt/compiler-worker/riscv-smoke.c
RUN clang-18 --target=riscv64-linux-gnu --gcc-toolchain=/usr -march=rv64gc -mabi=lp64d \
    /opt/compiler-worker/riscv-smoke.c -o /opt/compiler-worker/riscv-smoke \
    && rm /opt/compiler-worker/riscv-smoke.c
ENTRYPOINT ["python3", "/opt/compiler-worker/supervisor.py"]
