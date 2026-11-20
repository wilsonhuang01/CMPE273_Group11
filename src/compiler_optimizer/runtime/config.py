"""Environment-only configuration; credential values are excluded from repr."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


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
    health_host: str = "0.0.0.0"
    health_port: int = 8080
    grpc_port: int = 50051
    probe_interval_seconds: int = 5

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
            "health_host": env.get("HEALTH_HOST", "0.0.0.0"),
        }
        if any(not value for value in values.values()):
            raise ValueError("service hosts, credentials, and virtual host must be nonempty")
        return cls(
            role=role,
            **values,
            rabbitmq_port=number("RABBITMQ_PORT", 5672),
            redis_port=number("REDIS_PORT", 6379),
            health_port=number("HEALTH_PORT", 8080),
            grpc_port=number("GRPC_PORT", 50051),
            probe_interval_seconds=number("PROBE_INTERVAL_SECONDS", 5, 60),
            coordinator_health_url=url,
            state_directory=Path(env.get("STATE_DIRECTORY", "var")),
        )
