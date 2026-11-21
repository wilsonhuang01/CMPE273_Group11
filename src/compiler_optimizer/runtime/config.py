"""Environment-only configuration; credential values are excluded from repr."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from compiler_optimizer.runtime.security import validate_rpc_security


@dataclass(frozen=True)
class Settings:
    role: str
    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    rabbitmq_user: str = field(default="optimizer", repr=False)
    rabbitmq_password: str = field(default="optimizer-dev-password", repr=False)
    rabbitmq_vhost: str = "/"
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = field(default="redis-dev-password", repr=False)
    coordinator_health_url: str = "http://localhost:18080/readyz"
    state_directory: Path = Path("var")
    health_host: str = "127.0.0.1"
    health_port: int = 8080
    grpc_port: int = 50051
    grpc_host: str = "127.0.0.1"
    probe_interval_seconds: int = 5
    execution_enabled: bool = True
    transfer_enabled: bool = True
    api_token: str = field(default="", repr=False)
    rpc_tls_cert: Path | None = None
    rpc_tls_key: Path | None = field(default=None, repr=False)
    execution_backend: str = "local"
    fleet_enabled: bool = True
    service_enabled: bool = True
    coordinator_endpoint: str = "127.0.0.1:50052"
    coordinator_tls: bool = False
    coordinator_ca: Path | None = None
    worker_directory: Path = Path("var/worker")
    worker_target: str = "auto"

    @classmethod
    def from_env(cls, role: str, environ=None):
        env = os.environ if environ is None else environ
        if role not in ("coordinator", "scheduler", "worker"):
            raise ValueError("unknown service role")

        def number(name, default, maximum=65535):
            try:
                value = int(env.get(name, default))
            except (ValueError, TypeError):
                raise ValueError(f"{name} must be an integer") from None
            if not 1 <= value <= maximum:
                raise ValueError(f"{name} is out of range")
            return value

        url = env.get("COORDINATOR_HEALTH_URL", "http://localhost:18080/readyz")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("COORDINATOR_HEALTH_URL must be an HTTP(S) URL without credentials")
        values = {
            "rabbitmq_host": env.get("RABBITMQ_HOST", "localhost"),
            "rabbitmq_user": env.get("RABBITMQ_USER", "optimizer"),
            "rabbitmq_password": env.get("RABBITMQ_PASSWORD", "optimizer-dev-password"),
            "rabbitmq_vhost": env.get("RABBITMQ_VHOST", "/"),
            "redis_host": env.get("REDIS_HOST", "localhost"),
            "redis_password": env.get("REDIS_PASSWORD", "redis-dev-password"),
            "health_host": env.get("HEALTH_HOST", "127.0.0.1"),
            "grpc_host": env.get("GRPC_HOST", "127.0.0.1"),
        }
        if any(not value for value in values.values()):
            raise ValueError("service hosts, credentials, and virtual host must be nonempty")
        execution = env.get("EXECUTION_ENABLED", "true" if role == "coordinator" else "false")
        if execution not in {"true", "false"}:
            raise ValueError("EXECUTION_ENABLED must be true or false")
        if execution == "true" and role != "coordinator":
            raise ValueError("automatic execution requires the coordinator role")
        transfer = env.get("TRANSFER_ENABLED", "true" if role == "coordinator" else "false")
        if transfer not in {"true", "false"}:
            raise ValueError("TRANSFER_ENABLED must be true or false")
        if transfer == "true" and role != "coordinator":
            raise ValueError("artifact transfer requires the coordinator role")
        backend = env.get("EXECUTION_BACKEND", "local")
        if backend not in {"local", "remote"}:
            raise ValueError("EXECUTION_BACKEND must be local or remote")
        switches = {}
        for key, default in (
            ("FLEET_ENABLED", "true"),
            ("SERVICE_ENABLED", "true"),
            ("COORDINATOR_TLS", "false"),
        ):
            switch = env.get(key, default)
            if switch not in {"true", "false"}:
                raise ValueError(f"{key} must be true or false")
            switches[key] = switch == "true"
        target = env.get("WORKER_TARGET", "auto")
        if target not in {"auto", "linux-x86_64", "linux-aarch64", "linux-riscv64"}:
            raise ValueError("unsupported WORKER_TARGET")
        if role == "coordinator" and backend == "remote" and not switches["FLEET_ENABLED"]:
            raise ValueError("remote execution requires FLEET_ENABLED")
        settings = cls(
            role=role,
            **values,
            rabbitmq_port=number("RABBITMQ_PORT", 5672),
            redis_port=number("REDIS_PORT", 6379),
            health_port=number("HEALTH_PORT", 8080),
            grpc_port=number("GRPC_PORT", 50051),
            probe_interval_seconds=number("PROBE_INTERVAL_SECONDS", 5, 60),
            coordinator_health_url=url,
            state_directory=Path(env.get("STATE_DIRECTORY", "var")),
            execution_enabled=execution == "true",
            transfer_enabled=transfer == "true",
            api_token=env.get("OPTIMIZER_API_TOKEN", ""),
            rpc_tls_cert=Path(env["RPC_TLS_CERT"]) if env.get("RPC_TLS_CERT") else None,
            rpc_tls_key=Path(env["RPC_TLS_KEY"]) if env.get("RPC_TLS_KEY") else None,
            execution_backend=backend,
            fleet_enabled=switches["FLEET_ENABLED"],
            service_enabled=switches["SERVICE_ENABLED"],
            coordinator_endpoint=env.get("COORDINATOR_ENDPOINT", "127.0.0.1:50052"),
            coordinator_tls=switches["COORDINATOR_TLS"],
            coordinator_ca=Path(env["COORDINATOR_CA"]) if env.get("COORDINATOR_CA") else None,
            worker_directory=Path(env.get("WORKER_DIRECTORY", "var/worker")),
            worker_target=target,
        )
        validate_rpc_security(settings)
        return settings
