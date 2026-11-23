FROM python:3.11-slim-bookworm AS builder
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ src/
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.11-slim-bookworm
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
