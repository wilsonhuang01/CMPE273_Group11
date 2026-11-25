FROM cmpe273-optimizer:dev
USER root
RUN pip install --no-cache-dir pytest ruff && apt-get update && apt-get install -y --no-install-recommends docker.io && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
ENV PYTHONPATH=/workspace/src PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python", "-m", "pytest"]
