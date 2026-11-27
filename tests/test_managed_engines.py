"""Broker/cache behavior against the managed engine versions, over TLS (action R, phase 4).

Amazon MQ runs RabbitMQ 3.13 and ElastiCache runs Valkey 8.0 (see infra/aws/environment).
These tests start those engine versions locally with TLS and authentication and run the
same ``service_check`` used by ``./project infra validate`` in AWS, plus the broker
rotation against the real RabbitMQ management API and cache token rotation through
the rotating-secret client. They need Docker:

    MANAGED_ENGINE_TESTS=1 python -m pytest tests/test_managed_engines.py
"""

import importlib.util
import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pika
import pytest
import redis

import compiler_optimizer.runtime.connections as connections
from compiler_optimizer.runtime import secrets as secret_module
from compiler_optimizer.runtime.config import Settings
from compiler_optimizer.runtime.connections import rabbitmq_parameters, redis_client
from compiler_optimizer.runtime.service_check import run_checks

ROOT = Path(__file__).resolve().parents[1]
RABBITMQ_IMAGE = "rabbitmq:3.13-management-alpine"
VALKEY_IMAGE = "valkey/valkey:8.0-alpine"

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("MANAGED_ENGINE_TESTS") != "1",
        reason="set MANAGED_ENGINE_TESTS=1 to run against RabbitMQ 3.13 and Valkey 8.0",
    ),
]

spec = importlib.util.spec_from_file_location("rotation", ROOT / "infra/aws/lambda/rotation.py")
rotation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rotation)


def _openssl(*arguments):
    subprocess.run(["openssl", *map(str, arguments)], check=True, capture_output=True)


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    """A private CA and a localhost server certificate that strict verification accepts."""
    directory = tmp_path_factory.mktemp("tls")
    config = directory / "openssl.cnf"
    config.write_text(
        "[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=managed-engine-test-ca\n"
        "[ca]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n"
        "subjectKeyIdentifier=hash\n"
        "[server]\nbasicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n"
        "subjectAltName=DNS:localhost,IP:127.0.0.1\nsubjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid\n"
    )
    ca, ca_key = directory / "ca.pem", directory / "ca.key"
    server, server_key = directory / "server.pem", directory / "server.key"
    _openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "2",
        "-config",
        config,
        "-extensions",
        "ca",
        "-keyout",
        ca_key,
        "-out",
        ca,
    )
    _openssl(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-config",
        config,
        "-subj",
        "/CN=localhost",
        "-keyout",
        server_key,
        "-out",
        directory / "server.csr",
    )
    _openssl(
        "x509",
        "-req",
        "-in",
        directory / "server.csr",
        "-CA",
        ca,
        "-CAkey",
        ca_key,
        "-CAcreateserial",
        "-days",
        "2",
        "-extfile",
        config,
        "-extensions",
        "server",
        "-out",
        server,
    )
    for path in directory.iterdir():
        path.chmod(0o644)  # Readable by the unprivileged users inside the containers.
    directory.chmod(0o755)
    return directory


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _docker(*arguments, check=True):
    return subprocess.run(["docker", *arguments], check=check, capture_output=True, text=True)


def _wait(condition, timeout=90):
    deadline, error = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            if condition():
                return
        except Exception as caught:  # noqa: BLE001 - keep polling until the deadline
            error = caught
        time.sleep(0.5)
    raise TimeoutError(f"service not ready: {error!r}")


@pytest.fixture(scope="module")
def engines(certificates):
    name = f"managed-engines-{uuid.uuid4().hex[:8]}"
    amqps, api, cache = _free_port(), _free_port(), _free_port()
    password, token = uuid.uuid4().hex, uuid.uuid4().hex
    (certificates / "rabbitmq.conf").write_text(
        "listeners.tcp = none\nlisteners.ssl.default = 5671\n"
        "ssl_options.cacertfile = /tls/ca.pem\nssl_options.certfile = /tls/server.pem\n"
        "ssl_options.keyfile = /tls/server.key\nssl_options.verify = verify_none\n"
        "ssl_options.fail_if_no_peer_cert = false\nmanagement.tcp.port = 15672\n"
        "loopback_users = none\n"
    )
    (certificates / "rabbitmq.conf").chmod(0o644)
    _docker(
        "run",
        "-d",
        "--rm",
        "--name",
        f"{name}-rabbitmq",
        "-p",
        f"127.0.0.1:{amqps}:5671",
        "-p",
        f"127.0.0.1:{api}:15672",
        "-v",
        f"{certificates}:/tls:ro",
        "-e",
        "RABBITMQ_CONFIG_FILE=/tls/rabbitmq.conf",
        "-e",
        "RABBITMQ_DEFAULT_USER=optimizer",
        "-e",
        f"RABBITMQ_DEFAULT_PASS={password}",
        RABBITMQ_IMAGE,
    )
    _docker(
        "run",
        "-d",
        "--rm",
        "--name",
        f"{name}-valkey",
        "-p",
        f"127.0.0.1:{cache}:6379",
        "-v",
        f"{certificates}:/tls:ro",
        VALKEY_IMAGE,
        "valkey-server",
        "--port",
        "0",
        "--tls-port",
        "6379",
        "--tls-cert-file",
        "/tls/server.pem",
        "--tls-key-file",
        "/tls/server.key",
        "--tls-ca-cert-file",
        "/tls/ca.pem",
        "--tls-auth-clients",
        "no",
        "--requirepass",
        token,
    )
    environ = {
        "RABBITMQ_HOST": "localhost",
        "RABBITMQ_PORT": str(amqps),
        "RABBITMQ_TLS": "true",
        "RABBITMQ_CA": str(certificates / "ca.pem"),
        "RABBITMQ_USER": "optimizer",
        "RABBITMQ_PASSWORD": password,
        "REDIS_HOST": "localhost",
        "REDIS_PORT": str(cache),
        "REDIS_TLS": "true",
        "REDIS_CA": str(certificates / "ca.pem"),
        "REDIS_PASSWORD": token,
        "EXECUTION_ENABLED": "false",
        "TRANSFER_ENABLED": "false",
    }
    settings = Settings.from_env("coordinator", environ)
    try:
        _wait(
            lambda: (
                pika.BlockingConnection(
                    rabbitmq_parameters(
                        settings,
                        socket_timeout=2,
                        stack_timeout=4,
                        blocked_connection_timeout=2,
                        heartbeat=10,
                    )
                ).close()
                is None
            )
        )
        _wait(lambda: redis_client(settings).ping())
        yield {
            "environ": environ,
            "settings": settings,
            "api": f"http://127.0.0.1:{api}",
        }
    finally:
        _docker("rm", "-f", f"{name}-rabbitmq", f"{name}-valkey", check=False)


# Tests run in file order: the rotation tests below change the broker and cache
# credentials, so the checks that use the start-up credentials come first.


def test_service_check_passes_against_the_managed_engine_versions(engines):
    report = run_checks(engines["settings"], ("broker", "cache"))
    failed = [check for check in report["checks"] if not check["ok"]]
    assert report["ok"], json.dumps(failed, indent=2)
    names = {check["name"] for check in report["checks"]}
    assert {
        "broker.publisher_confirms",
        "broker.acknowledgements",
        "broker.dead_letter",
        "broker.retry_ttl",
        "cache.requires_auth",
        "cache.quarantine_commands",
    } <= names
    assert report["versions"]["rabbitmq"].startswith("3.13.")
    assert report["versions"]["cache"].startswith("valkey 8.0.")


def test_service_check_reports_a_wrong_credential_instead_of_raising(engines):
    environ = {**engines["environ"], "REDIS_PASSWORD": "wrong-token-value"}
    report = run_checks(Settings.from_env("coordinator", environ), ("cache",))
    assert not report["ok"]
    assert report["checks"][0]["name"] == "cache.connect"


class StoredSecret:
    """Secrets Manager stand-in that only answers get_secret_value."""

    def __init__(self, value):
        self.value, self.reads = value, 0

    def get_secret_value(self, SecretId):
        self.reads += 1
        return {"SecretString": self.value}


def _login(settings):
    connection = pika.BlockingConnection(
        rabbitmq_parameters(
            settings, socket_timeout=3, stack_timeout=5, blocked_connection_timeout=3, heartbeat=10
        )
    )
    connection.close()


def test_broker_rotation_alternates_users_and_keeps_the_previous_login(engines, monkeypatch):
    base = engines["settings"]
    current = json.dumps({"username": "optimizer", "password": base.rabbitmq_password})
    rotator = rotation.RabbitMQRotator(engines["api"], "optimizer")

    first = rotator.create(current, uuid.uuid4().hex)
    assert json.loads(first)["username"] == "optimizer-alt"
    rotator.apply(None, None, None, current, first, {})
    rotator.test(first)
    second = rotator.create(first, uuid.uuid4().hex)
    assert json.loads(second)["username"] == "optimizer"
    rotator.apply(None, None, None, first, second, {})
    rotator.test(second)

    # Services reading the secret move to the newest login; the previous one still works
    # until the next rotation, so a client with a cached value is not locked out.
    store = StoredSecret(first)
    secret = secret_module.RotatingSecret("broker", client=store, ttl=0)
    monkeypatch.setattr(connections, "rotating_secret", lambda *_: secret)
    environ = {k: v for k, v in engines["environ"].items() if k != "RABBITMQ_PASSWORD"}
    settings = Settings.from_env("coordinator", {**environ, "RABBITMQ_SECRET_ID": "broker"})
    _login(settings)  # Previous credential (optimizer-alt) is still valid.
    store.value = second
    _login(settings)  # New credential (optimizer with a new password).
    store.value = current
    with pytest.raises(pika.exceptions.ProbableAuthenticationError):
        _login(settings)  # The bootstrap password was replaced by the second rotation.


def test_broker_rotation_refuses_unknown_users_and_bad_logins(engines):
    rotator = rotation.RabbitMQRotator(engines["api"], "optimizer")
    with pytest.raises(rotation.RotationError, match="must be one of"):
        rotator.create(json.dumps({"username": "intruder", "password": "x"}), "y")
    wrong = json.dumps({"username": "optimizer", "password": "not-the-password"})
    pending = json.dumps({"username": "optimizer-alt", "password": uuid.uuid4().hex})
    with pytest.raises(rotation.RotationError, match="401"):
        rotator.apply(None, None, None, wrong, pending, {})


def test_cache_token_rotation_with_two_valid_tokens(engines, monkeypatch):
    """ElastiCache ROTATE keeps the current and new token; Valkey ACL models the same."""
    settings = engines["settings"]
    admin = redis_client(settings)
    new_token = uuid.uuid4().hex
    admin.execute_command("ACL", "SETUSER", "default", f">{new_token}")  # Both now valid.
    store = StoredSecret(settings.redis_password)
    clock = [0.0]
    secret = secret_module.RotatingSecret("cache", client=store, ttl=60, clock=lambda: clock[0])
    monkeypatch.setattr(connections, "rotating_secret", lambda *_: secret)
    environ = {k: v for k, v in engines["environ"].items() if k != "REDIS_PASSWORD"}
    rotating = Settings.from_env("coordinator", {**environ, "REDIS_SECRET_ID": "cache"})
    assert redis_client(rotating).ping()
    store.value = new_token
    clock[0] = 61  # The cached value expires; the next connection reads the new token.
    client = redis_client(rotating)
    assert client.ping()
    assert store.reads == 2
    # The next rotation drops the oldest token, which no refreshed client still uses.
    admin.execute_command("ACL", "SETUSER", "default", f"<{settings.redis_password}")
    assert redis_client(rotating).ping()
    with pytest.raises(redis.AuthenticationError):
        redis_client(settings).ping()
    admin.close()
