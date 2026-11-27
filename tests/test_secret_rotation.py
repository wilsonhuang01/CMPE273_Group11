"""Secret rotation and rotating credentials, offline (action R, phase 4).

Covers the Secrets Manager rotation function in infra/aws/lambda/rotation.py, the
runtime's connect-time secret reads, and the service check's reporting. The same
rotation runs against a real RabbitMQ 3.13 management API in test_managed_engines.py.
"""

import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pika
import pytest
import redis
from test_cloud_storage import BUCKET, FakeS3

import compiler_optimizer.runtime.connections as connections
from compiler_optimizer.runtime import service_check
from compiler_optimizer.runtime.config import Settings
from compiler_optimizer.runtime.secrets import RotatingSecret, SecretUnavailable, parse_secret

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("rotation", ROOT / "infra/aws/lambda/rotation.py")
rotation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rotation)

ARN = "arn:aws:secretsmanager:us-west-2:123456789012:secret:p/dev/x"


# --- Fakes ------------------------------------------------------------------------------


class FakeSecretsManager:
    """Versions and staging labels with the rules Secrets Manager enforces."""

    class exceptions:  # noqa: N801 - mirrors the boto3 client attribute
        class ResourceNotFoundException(Exception):
            pass

    def __init__(self, current):
        self.values = {"v0": current}
        self.stages = {"v0": ["AWSCURRENT"]}
        self.calls = []

    def describe_secret(self, SecretId):
        return {"VersionIdsToStages": {k: list(v) for k, v in self.stages.items() if v}}

    def get_secret_value(self, SecretId, VersionId=None, VersionStage=None):
        for version, stages in self.stages.items():
            if (
                version in self.values
                and (VersionId is None or version == VersionId)
                and (VersionStage is None or VersionStage in stages)
            ):
                return {"SecretString": self.values[version], "VersionId": version}
        raise self.exceptions.ResourceNotFoundException()

    def get_random_password(self, **options):
        assert options["ExcludePunctuation"] and options["PasswordLength"] >= 32
        return {"RandomPassword": f"new{len(self.values)}" + "x" * 45}

    def put_secret_value(self, SecretId, ClientRequestToken, SecretString, VersionStages):
        self.values[ClientRequestToken] = SecretString
        self.stages.setdefault(ClientRequestToken, []).extend(VersionStages)

    def update_secret_version_stage(
        self, SecretId, VersionStage, MoveToVersionId, RemoveFromVersionId=None
    ):
        holder = next((v for v, s in self.stages.items() if VersionStage in s), None)
        if holder and holder != MoveToVersionId and holder != RemoveFromVersionId:
            raise ValueError(f"{VersionStage} is attached to {holder}")
        self.calls.append((VersionStage, MoveToVersionId, RemoveFromVersionId))
        if holder:
            self.stages[holder].remove(VersionStage)
        self.stages[MoveToVersionId].append(VersionStage)
        if VersionStage == "AWSCURRENT" and holder:
            self.stages[holder].append("AWSPREVIOUS")

    def start(self, token):
        """What RotateSecret does first: a pending version label with no value yet."""
        self.stages[token] = ["AWSPENDING"]


class FakeElastiCache:
    def __init__(self):
        self.tokens, self.modifications, self.status = [], [], "available"

    def describe_replication_groups(self, ReplicationGroupId):
        return {
            "ReplicationGroups": [
                {
                    "Status": self.status,
                    "AuthTokenEnabled": bool(self.tokens),
                    "TransitEncryptionEnabled": True,
                }
            ]
        }

    def modify_replication_group(
        self, ReplicationGroupId, AuthToken, AuthTokenUpdateStrategy, ApplyImmediately
    ):
        assert ApplyImmediately
        self.modifications.append(AuthTokenUpdateStrategy)
        if AuthTokenUpdateStrategy == "ROTATE":
            self.tokens = (self.tokens + [AuthToken])[-2:]  # Two most recent tokens.
        else:
            self.tokens = [AuthToken]


class FakeBrokerApi:
    """RabbitMQ management API: users, permissions and whoami with basic auth."""

    def __init__(self, users):
        self.users, self.permissions, self.requests = dict(users), {}, []

    def __call__(self, request):
        import base64

        user, _, password = (
            base64.b64decode(request.headers["Authorization"].split()[1]).decode().partition(":")
        )
        path = request.full_url.split("://", 1)[1].split("/", 1)[1]
        self.requests.append((request.get_method(), path, user))
        if self.users.get(user) != password:
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)
        body = json.loads(request.data) if request.data else None
        if path.startswith("api/users/"):
            self.users[path.rsplit("/", 1)[1]] = body["password"]
            assert body["tags"] == "administrator"
        elif path.startswith("api/permissions/%2F/"):
            self.permissions[path.rsplit("/", 1)[1]] = body
        elif path == "api/whoami":
            return _Response(json.dumps({"name": user}).encode())
        return _Response(b"")


class _Response(io.BytesIO):
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def run_rotation(secrets, token, environ, **clients):
    secrets.start(token)
    for step in ("createSecret", "setSecret", "testSecret", "finishSecret"):
        event = {"SecretId": ARN, "ClientRequestToken": token, "Step": step}
        rotation.handler(event, None, secrets=secrets, environ=environ, **clients)


REDIS_ENV = {"ROTATION_KIND": "redis", "REPLICATION_GROUP_ID": "p-dev-cache"}
BROKER_ENV = {
    "ROTATION_KIND": "rabbitmq",
    "RABBITMQ_API_URL": "https://b-1.mq.us-west-2.amazonaws.com",
    "RABBITMQ_USER": "optimizer",
}


# --- Rotation function --------------------------------------------------------------


def test_cache_rotation_keeps_the_current_token_valid():
    secrets, cache = FakeSecretsManager("token-0"), FakeElastiCache()
    cache.tokens = ["token-0"]
    redis_rotator = rotation.RedisRotator(cache, "p-dev-cache", poll_seconds=0)
    run_rotation(secrets, "v1", REDIS_ENV, elasticache=cache)
    new = secrets.get_secret_value(ARN, VersionStage="AWSCURRENT")["SecretString"]
    assert new != "token-0" and cache.tokens == ["token-0", new]
    assert cache.modifications == ["ROTATE"]
    assert secrets.stages["v0"] == ["AWSPREVIOUS"]
    run_rotation(secrets, "v2", REDIS_ENV, elasticache=cache)
    newest = secrets.get_secret_value(ARN, VersionStage="AWSCURRENT")["SecretString"]
    assert cache.tokens == [new, newest]  # The token from two rotations ago is gone.
    assert redis_rotator.wait_available()["AuthTokenEnabled"]


def test_retried_cache_set_step_does_not_rotate_twice():
    secrets, cache = FakeSecretsManager("token-0"), FakeElastiCache()
    cache.tokens = ["token-0"]
    secrets.start("v1")
    for step in ("createSecret", "setSecret", "setSecret", "testSecret", "finishSecret"):
        event = {"SecretId": ARN, "ClientRequestToken": "v1", "Step": step}
        rotation.handler(event, None, secrets=secrets, elasticache=cache, environ=REDIS_ENV)
    assert cache.modifications == ["ROTATE"]
    assert "token-0" in cache.tokens  # A second ROTATE would have dropped it.
    secrets.start("v2")
    rotation.handler(
        {"SecretId": ARN, "ClientRequestToken": "v2", "Step": "createSecret"},
        None,
        secrets=secrets,
        elasticache=cache,
        environ=REDIS_ENV,
    )
    rotation.handler(
        {"SecretId": ARN, "ClientRequestToken": "v2", "Step": "setSecret"},
        None,
        secrets=secrets,
        elasticache=cache,
        environ=REDIS_ENV,
    )
    assert ("APPLIED", "v2", "v1") in secrets.calls  # The label moves to the new version.


def test_broker_rotation_alternates_users_without_touching_the_current_one():
    secrets = FakeSecretsManager(json.dumps({"username": "optimizer", "password": "boot"}))
    api = FakeBrokerApi({"optimizer": "boot"})
    run_rotation(secrets, "v1", BROKER_ENV, opener=api)
    first = json.loads(secrets.get_secret_value(ARN, VersionStage="AWSCURRENT")["SecretString"])
    assert first["username"] == "optimizer-alt" and api.users["optimizer"] == "boot"
    assert api.permissions["optimizer-alt"] == {"configure": ".*", "write": ".*", "read": ".*"}
    run_rotation(secrets, "v2", BROKER_ENV, opener=api)
    second = json.loads(secrets.get_secret_value(ARN, VersionStage="AWSCURRENT")["SecretString"])
    assert second["username"] == "optimizer" and second["password"] != "boot"
    assert api.users == {"optimizer": second["password"], "optimizer-alt": first["password"]}
    # Each change was authenticated as the user that was current at the time.
    assert [r[2] for r in api.requests if r[0] == "PUT"] == [
        "optimizer",
        "optimizer",
        "optimizer-alt",
        "optimizer-alt",
    ]


def test_rotation_steps_are_idempotent_and_reject_stray_versions():
    secrets, cache = FakeSecretsManager("token-0"), FakeElastiCache()
    clients = {"secrets": secrets, "elasticache": cache, "environ": REDIS_ENV}
    configuration = {"SecretId": ARN, "ClientRequestToken": "never-created", "Step": "testSecret"}
    assert rotation.handler(configuration, None, **clients)["result"] == "configuration-check"
    with pytest.raises(rotation.RotationError, match="not part of"):
        rotation.handler({**configuration, "Step": "setSecret"}, None, **clients)
    current = {"SecretId": ARN, "ClientRequestToken": "v0", "Step": "setSecret"}
    assert rotation.handler(current, None, **clients)["result"] == "already-current"
    secrets.stages["v9"] = ["AWSPREVIOUS"]
    with pytest.raises(rotation.RotationError, match="not pending"):
        rotation.handler({**current, "ClientRequestToken": "v9"}, None, **clients)
    secrets.start("v1")
    create = {"SecretId": ARN, "ClientRequestToken": "v1", "Step": "createSecret"}
    rotation.handler(create, None, **clients)
    value = secrets.values["v1"]
    assert rotation.handler(create, None, **clients)["result"] == "pending-exists"
    assert secrets.values["v1"] == value


@pytest.mark.parametrize(
    "url", ["http://b-1.mq.us-west-2.amazonaws.com", "ftp://localhost", "https://"]
)
def test_broker_api_must_use_https(url):
    with pytest.raises(rotation.RotationError, match="https"):
        rotation.RabbitMQRotator(url, "optimizer")


def test_broker_rotation_refuses_unknown_users():
    rotator = rotation.RabbitMQRotator(BROKER_ENV["RABBITMQ_API_URL"], "optimizer")
    with pytest.raises(rotation.RotationError, match="must be one of"):
        rotator.create(json.dumps({"username": "admin", "password": "x"}), "y")
    with pytest.raises(rotation.RotationError, match="JSON with username"):
        rotator.create(json.dumps({"password": "x"}), "y")


def test_cache_test_step_requires_auth_over_tls():
    cache = FakeElastiCache()
    with pytest.raises(rotation.RotationError, match="AUTH over TLS"):
        rotation.RedisRotator(cache, "g", poll_seconds=0).test("token")


# --- Rotating secrets in the runtime ----------------------------------------------------


class Store:
    def __init__(self, value):
        self.value, self.reads = value, 0

    def get_secret_value(self, SecretId):
        self.reads += 1
        if isinstance(self.value, Exception):
            raise self.value
        return {"SecretString": self.value}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain-token", {"username": None, "password": "plain-token"}),
        ('{"username": "u", "password": "p"}', {"username": "u", "password": "p"}),
        ('{"password": "p", "engine": "postgres"}', {"username": None, "password": "p"}),
    ],
)
def test_secret_formats(text, expected):
    assert parse_secret(text) == expected


@pytest.mark.parametrize("text", ["", '{"username": "u"}', '{"password": ""}', "{broken"])
def test_unusable_secrets_are_rejected(text):
    with pytest.raises(SecretUnavailable):
        parse_secret(text)


def test_rotating_secret_caches_refreshes_and_rate_limits():
    clock, store = [0.0], Store("one")
    secret = RotatingSecret("id", client=store, ttl=60, clock=lambda: clock[0])
    assert secret.credentials("default") == ("default", "one")
    store.value = "two"
    assert secret.value()["password"] == "one" and store.reads == 1
    assert secret.refresh()["password"] == "one"  # Too soon after the last read.
    clock[0] = 3
    assert secret.refresh()["password"] == "two" and store.reads == 2
    store.value = "three"
    clock[0] = 63.5
    assert secret.value()["password"] == "three"
    assert "three" not in repr(secret)


def test_secret_outage_names_the_secret_but_not_the_cause_text():
    secret = RotatingSecret("p/dev/redis", client=Store(RuntimeError("token=leaked")))
    with pytest.raises(SecretUnavailable) as caught:
        secret.value()
    assert "p/dev/redis" in str(caught.value) and "leaked" not in str(caught.value)
    assert caught.value.__cause__ is None


BASE = {"EXECUTION_ENABLED": "false", "TRANSFER_ENABLED": "false"}


@pytest.mark.parametrize("service", ["RABBITMQ", "REDIS", "DATABASE"])
def test_secret_id_and_fixed_password_are_exclusive(service):
    environ = {
        **BASE,
        f"{service}_SECRET_ID": "id",
        f"{service}_PASSWORD": "pw",
        "DATABASE_URL": "postgresql://u@localhost/db",
    }
    with pytest.raises(ValueError, match="not both"):
        Settings.from_env("coordinator", environ)


def test_database_secret_needs_a_database():
    with pytest.raises(ValueError, match="requires DATABASE_URL"):
        Settings.from_env("coordinator", {**BASE, "DATABASE_SECRET_ID": "id"})


def _patch_secret(monkeypatch, value):
    secret = RotatingSecret("id", client=Store(value), ttl=0)
    monkeypatch.setattr(connections, "rotating_secret", lambda *_: secret)
    return secret


def test_broker_credentials_come_from_the_secret(monkeypatch):
    _patch_secret(monkeypatch, json.dumps({"username": "optimizer-alt", "password": "p1"}))
    settings = Settings.from_env("coordinator", {**BASE, "RABBITMQ_SECRET_ID": "id"})
    params = connections.rabbitmq_parameters(
        settings, socket_timeout=1, stack_timeout=1, blocked_connection_timeout=1, heartbeat=1
    )
    assert (params.credentials.username, params.credentials.password) == ("optimizer-alt", "p1")


def test_secret_outage_is_a_broker_connection_error(monkeypatch):
    _patch_secret(monkeypatch, RuntimeError("down"))
    settings = Settings.from_env("coordinator", {**BASE, "RABBITMQ_SECRET_ID": "id"})
    with pytest.raises(pika.exceptions.AMQPConnectionError):
        connections.rabbitmq_parameters(
            settings, socket_timeout=1, stack_timeout=1, blocked_connection_timeout=1, heartbeat=1
        )


def test_cache_reads_the_token_per_connection(monkeypatch):
    secret = _patch_secret(monkeypatch, "cache-token")
    settings = Settings.from_env("coordinator", {**BASE, "REDIS_SECRET_ID": "id"})
    provider = connections.redis_client(settings).connection_pool.connection_kwargs[
        "credential_provider"
    ]
    assert provider.get_credentials() == ("cache-token",)
    secret._client.value = "rotated-token"
    assert provider.get_credentials() == ("rotated-token",)
    secret._client.value = RuntimeError("down")
    with pytest.raises(redis.ConnectionError):
        provider.get_credentials()


def test_database_retries_once_with_a_refreshed_password(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    from compiler_optimizer.storage.postgres import PostgresDatabase

    clock, store = [0.0], Store('{"username": "optimizer_admin", "password": "old"}')
    secret = RotatingSecret("db", client=store, ttl=60, clock=lambda: clock[0])
    database = PostgresDatabase("postgresql://u@localhost/db", credentials=secret, pool_size=0)
    attempts = []

    class Connection:
        adapters = type("A", (), {"register_dumper": staticmethod(lambda *a: None)})()

    def connect(url, **options):
        attempts.append((options.get("user"), options["password"]))
        if options["password"] != "new":
            raise psycopg.OperationalError(
                'connection failed: FATAL:  password authentication failed for user "x"'
            )
        return Connection()

    monkeypatch.setattr(psycopg, "connect", connect)
    secret.value()
    store.value = '{"username": "optimizer_admin", "password": "new"}'  # RDS rotated it.
    clock[0] = 5
    database._connect()
    assert attempts == [("optimizer_admin", "old"), ("optimizer_admin", "new")]

    def unreachable(url, **options):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", unreachable)
    reads = store.reads
    with pytest.raises(psycopg.OperationalError):
        database._connect()
    assert store.reads == reads  # Only a rejected password triggers a re-read.
    with pytest.raises(ValueError, match="not both"):
        PostgresDatabase("postgresql://u@localhost/db", password="p", credentials=secret)


# --- Service check reporting -----------------------------------------------------------


def test_artifact_check_round_trips_under_its_own_prefix(monkeypatch):
    s3 = FakeS3()
    monkeypatch.setattr("compiler_optimizer.storage.s3_artifacts.s3_client", lambda **_: s3)
    settings = Settings.from_env(
        "coordinator",
        {
            **BASE,
            "ARTIFACT_BACKEND": "s3",
            "ARTIFACT_S3_BUCKET": BUCKET,
            "ARTIFACT_S3_PREFIX": "artifacts",
        },
    )
    report = service_check.run_checks(settings, ("artifacts", "database"))
    assert report["ok"], report
    assert all(key.startswith("artifacts/service-check/sha256/") for key in s3.objects)
    assert report["checks"][-1]["detail"] == "skipped: not configured"


def test_unreachable_services_are_reported_not_raised():
    settings = Settings.from_env(
        "coordinator",
        {
            **BASE,
            "RABBITMQ_HOST": "127.0.0.1",
            "RABBITMQ_PORT": "1",
            "REDIS_HOST": "127.0.0.1",
            "REDIS_PORT": "1",
        },
    )
    report = service_check.run_checks(settings, ("broker", "cache"))
    assert not report["ok"]
    assert [check["name"] for check in report["checks"]] == ["broker.connect", "cache.connect"]
    with pytest.raises(ValueError, match="unknown service"):
        service_check.run_checks(settings, ("queue",))
