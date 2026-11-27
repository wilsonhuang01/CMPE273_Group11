"""Environment-only configuration; credential values are excluded from repr."""

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from compiler_optimizer.runtime.security import validate_rpc_security
from compiler_optimizer.shared.agents_rpc import AGENT_NAMES, AGENT_PORTS


@dataclass(frozen=True)
class Settings:
    role: str
    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    rabbitmq_user: str = field(default="optimizer", repr=False)
    rabbitmq_password: str = field(default="optimizer-dev-password", repr=False)
    rabbitmq_vhost: str = "/"
    rabbitmq_tls: bool = False
    rabbitmq_ca: Path | None = None
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = field(default="redis-dev-password", repr=False)
    redis_username: str | None = None
    redis_tls: bool = False
    redis_ca: Path | None = None
    artifact_backend: str = "local"
    artifact_bucket: str = ""
    artifact_prefix: str = ""
    artifact_kms_key_id: str = ""
    aws_region: str | None = None
    s3_endpoint_url: str | None = None
    database_url: str = field(default="", repr=False)
    database_password: str = field(default="", repr=False)
    database_schema: str = "optimizer"
    dispatch_lease_seconds: int = 30
    dispatch_concurrency: int = 1
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

    agent_backend: str = "local"
    agent_endpoints: str = ""
    agent_timeout_seconds: float = 15.0
    agent_token: str = field(default="", repr=False)
    agent_tls: bool = False
    agent_ca_directory: Path | None = None
    agent_dev_tls: bool = False
    agent_tls_directory: Path | None = None
    agent_release: str = "0.1.0"
    debate_policy_json: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, role: str, environ=None):
        env = os.environ if environ is None else environ
        debate_policy_json = env.get("DEBATE_POLICY_JSON", "")
        if debate_policy_json:
            from compiler_optimizer.shared.debate import DebatePolicy

            DebatePolicy.parse(debate_policy_json)
        if role not in ("coordinator", "scheduler", "worker", *AGENT_NAMES):
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
            ("RABBITMQ_TLS", "false"),
            ("REDIS_TLS", "false"),
        ):
            switch = env.get(key, default)
            if switch not in {"true", "false"}:
                raise ValueError(f"{key} must be true or false")
            switches[key] = switch == "true"
        for service in ("RABBITMQ", "REDIS"):
            if env.get(service + "_CA") and not switches[service + "_TLS"]:
                raise ValueError(f"{service}_CA requires {service}_TLS=true")
        artifact_backend = env.get("ARTIFACT_BACKEND", "local")
        if artifact_backend not in {"local", "s3"}:
            raise ValueError("ARTIFACT_BACKEND must be local or s3")
        if artifact_backend == "s3" and role != "coordinator":
            raise ValueError("the S3 artifact backend is owned by the coordinator role")
        if artifact_backend == "s3" and not env.get("ARTIFACT_S3_BUCKET"):
            raise ValueError("ARTIFACT_BACKEND=s3 requires ARTIFACT_S3_BUCKET")
        from compiler_optimizer.storage.postgres import validate_database_url, validate_schema

        database_url = env.get("DATABASE_URL", "")
        if database_url:
            if role != "coordinator":
                raise ValueError("the PostgreSQL repository is owned by the coordinator role")
            validate_database_url(database_url)
        database_schema = validate_schema(env.get("DATABASE_SCHEMA", "optimizer"))
        dispatch_concurrency = number("DISPATCH_CONCURRENCY", 1, 8)
        if dispatch_concurrency > 1 and backend != "local":
            # Remote fleet workers are shared across tasks and hold one recovery assignment.
            raise ValueError("DISPATCH_CONCURRENCY above 1 requires EXECUTION_BACKEND=local")
        s3_endpoint = env.get("S3_ENDPOINT_URL") or None
        if s3_endpoint is not None:
            endpoint = urlsplit(s3_endpoint)
            loopback = endpoint.hostname in ("localhost", "127.0.0.1", "::1")
            if (
                endpoint.scheme not in ("https", "http")
                or (endpoint.scheme == "http" and not loopback)
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
            ):
                raise ValueError("S3_ENDPOINT_URL must be HTTPS (HTTP only on loopback)")
        target = env.get("WORKER_TARGET", "auto")
        if target not in {"auto", "linux-x86_64", "linux-aarch64", "linux-riscv64"}:
            raise ValueError("unsupported WORKER_TARGET")
        if role == "coordinator" and backend == "remote" and not switches["FLEET_ENABLED"]:
            raise ValueError("remote execution requires FLEET_ENABLED")
        agent_backend = env.get("AGENT_BACKEND", "local")
        if agent_backend not in ("local", "rpc"):
            raise ValueError("AGENT_BACKEND must be local or rpc")
        endpoints = env.get("AGENT_ENDPOINTS", "")
        if endpoints:
            parsed_endpoints = json.loads(endpoints)
            if not isinstance(parsed_endpoints, dict) or set(parsed_endpoints) != set(AGENT_NAMES):
                raise ValueError("AGENT_ENDPOINTS must configure exactly six roles")
            for endpoint in parsed_endpoints.values():
                if not isinstance(endpoint, str) or not endpoint.rpartition(":")[2].isdigit():
                    raise ValueError("invalid agent endpoint")
        timeout = float(env.get("AGENT_TIMEOUT_SECONDS", "15"))
        if not math.isfinite(timeout) or not 0.05 <= timeout <= 120:
            raise ValueError("AGENT_TIMEOUT_SECONDS must be 0.05–120")
        for key in ("AGENT_TLS", "AGENT_DEV_TLS"):
            if env.get(key, "false") not in ("true", "false"):
                raise ValueError(key + " must be true or false")
        dev_tls = env.get("AGENT_DEV_TLS", "false") == "true"
        if dev_tls and role not in AGENT_NAMES:
            raise ValueError("AGENT_DEV_TLS is only valid for agent processes")
        tls_directory = Path(env.get("AGENT_TLS_DIRECTORY", "var/agent-tls")) if dev_tls else None
        settings = cls(
            role=role,
            **values,
            rabbitmq_port=number("RABBITMQ_PORT", 5671 if switches["RABBITMQ_TLS"] else 5672),
            rabbitmq_tls=switches["RABBITMQ_TLS"],
            rabbitmq_ca=Path(env["RABBITMQ_CA"]) if env.get("RABBITMQ_CA") else None,
            redis_port=number("REDIS_PORT", 6379),
            redis_username=env.get("REDIS_USERNAME") or None,
            redis_tls=switches["REDIS_TLS"],
            redis_ca=Path(env["REDIS_CA"]) if env.get("REDIS_CA") else None,
            artifact_backend=artifact_backend,
            artifact_bucket=env.get("ARTIFACT_S3_BUCKET", ""),
            artifact_prefix=env.get("ARTIFACT_S3_PREFIX", ""),
            artifact_kms_key_id=env.get("ARTIFACT_S3_KMS_KEY_ID", ""),
            aws_region=env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or None,
            s3_endpoint_url=s3_endpoint,
            database_url=database_url,
            database_password=env.get("DATABASE_PASSWORD", ""),
            database_schema=database_schema,
            dispatch_lease_seconds=number("DISPATCH_LEASE_SECONDS", 30, 300),
            dispatch_concurrency=dispatch_concurrency,
            health_port=number(
                "HEALTH_PORT", 18091 + AGENT_NAMES.index(role) if role in AGENT_NAMES else 8080
            ),
            grpc_port=number("GRPC_PORT", AGENT_PORTS.get(role, 50051)),
            probe_interval_seconds=number("PROBE_INTERVAL_SECONDS", 5, 60),
            coordinator_health_url=url,
            state_directory=Path(env.get("STATE_DIRECTORY", "var")),
            execution_enabled=execution == "true",
            transfer_enabled=transfer == "true",
            api_token=env.get("AGENT_API_TOKEN", "")
            if role in AGENT_NAMES
            else env.get("OPTIMIZER_API_TOKEN", ""),
            rpc_tls_cert=(tls_directory / (role + ".crt"))
            if dev_tls
            else (Path(env["RPC_TLS_CERT"]) if env.get("RPC_TLS_CERT") else None),
            rpc_tls_key=(tls_directory / (role + ".key"))
            if dev_tls
            else (Path(env["RPC_TLS_KEY"]) if env.get("RPC_TLS_KEY") else None),
            execution_backend=backend,
            fleet_enabled=switches["FLEET_ENABLED"],
            service_enabled=switches["SERVICE_ENABLED"],
            coordinator_endpoint=env.get("COORDINATOR_ENDPOINT", "127.0.0.1:50052"),
            coordinator_tls=switches["COORDINATOR_TLS"],
            coordinator_ca=Path(env["COORDINATOR_CA"]) if env.get("COORDINATOR_CA") else None,
            worker_directory=Path(env.get("WORKER_DIRECTORY", "var/worker")),
            worker_target=target,
            agent_backend=agent_backend,
            agent_endpoints=endpoints,
            agent_timeout_seconds=timeout,
            agent_token=env.get("AGENT_API_TOKEN", ""),
            agent_tls=env.get("AGENT_TLS", "false") == "true",
            agent_ca_directory=Path(env["AGENT_CA_DIRECTORY"])
            if env.get("AGENT_CA_DIRECTORY")
            else None,
            agent_dev_tls=dev_tls,
            agent_tls_directory=tls_directory,
            agent_release=env.get("AGENT_RELEASE", "0.1.0"),
            debate_policy_json=debate_policy_json,
        )
        validate_rpc_security(settings)
        return settings
