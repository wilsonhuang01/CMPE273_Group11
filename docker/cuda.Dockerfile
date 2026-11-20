FROM cmpe273-clang:18
LABEL compiler-optimizer.cuda-compile="13.0-ptx-v1"
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
       ca-certificates wget g++-13 \
    && wget -q https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb -O /tmp/cuda-keyring.deb \
    && dpkg -i /tmp/cuda-keyring.deb \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends cuda-nvcc-13-0 \
    && rm -rf /var/lib/apt/lists/* /tmp/cuda-keyring.deb
COPY src/compiler_optimizer/workers/cuda_supervisor.py /opt/compiler-worker/cuda_supervisor.py
ENV NVIDIA_VISIBLE_DEVICES=void CUDA_VISIBLE_DEVICES=""
