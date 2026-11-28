FROM python:3.11-slim-bookworm@sha256:ef56ae98435cc40d8a27020b0b32a9941515acd5840f0fba34858bb34dd362d1 AS builder
# Optional extras baked into the image; "bedrock" lets agent services call Amazon Bedrock.
ARG PACKAGE_EXTRAS=bedrock
WORKDIR /build
COPY pyproject.toml README.md constraints.txt ./
COPY src/ src/
ENV PIP_CONSTRAINT=/build/constraints.txt
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels -c constraints.txt ".[${PACKAGE_EXTRAS}]"

FROM python:3.11-slim-bookworm@sha256:ef56ae98435cc40d8a27020b0b32a9941515acd5840f0fba34858bb34dd362d1
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install --yes --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 optimizer \
    && useradd --uid 10001 --gid optimizer --no-create-home optimizer \
    && groupadd --gid 10002 agent \
    && useradd --uid 10002 --gid agent --no-create-home agent \
    && mkdir -p /var/lib/compiler-optimizer /var/lib/compiler-agent /agent-ca \
    && chown optimizer:optimizer /var/lib/compiler-optimizer \
    && chown agent:agent /var/lib/compiler-agent /agent-ca
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index /wheels/*.whl && rm -rf /wheels
USER 10001:10001
WORKDIR /app
EXPOSE 8080 50051
ENTRYPOINT ["python", "-m", "compiler_optimizer.runtime"]
# Release provenance (action S), last so local builds keep their layer cache.
ARG RELEASE_VERSION=dev
ARG SOURCE_REVISION=unknown
ARG BUILD_MODE=local
LABEL org.opencontainers.image.version="${RELEASE_VERSION}" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.source="https://github.com/wilsonhuang01/CMPE273_Group11" \
      compiler-optimizer.build-mode="${BUILD_MODE}"
