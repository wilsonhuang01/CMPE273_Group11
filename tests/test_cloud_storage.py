"""Action R foundations: S3 artifact backend and TLS-aware broker/cache clients.

S3 behavior is exercised against an in-memory client that models the conditional-put,
checksum and error semantics the store relies on, so these tests run offline.
"""

import base64
import hashlib
import io
import ssl
from pathlib import Path
from uuid import uuid4

import pytest

from compiler_optimizer.runtime.config import Settings
from compiler_optimizer.runtime.connections import (
    artifact_store,
    rabbitmq_parameters,
    redis_client,
)
from compiler_optimizer.shared.contracts import BASE_FLAGS
from compiler_optimizer.shared.manifest import parse_manifest
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage import Repository, S3ArtifactStore
from compiler_optimizer.storage.artifacts import artifact_key
from compiler_optimizer.storage.errors import ArtifactError, NotFoundError, UnavailableError

BUCKET = "optimizer-artifacts-test"


class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeBody(io.BytesIO):
    def __init__(self, data, fail_after=None):
        super().__init__(data)
        self.fail_after = fail_after

    def read(self, size=-1):
        if self.fail_after is not None and self.tell() >= self.fail_after:
            raise TimeoutError("read timed out")
        return super().read(size)


class FakeS3:
    """Enough of the S3 API to model If-None-Match puts and checksum validation."""

    def __init__(self):
        self.objects, self.puts = {}, []
        self.conflicts = 0
        self.outage = False
        self.fail_read_after = None

    def head_bucket(self, Bucket):
        if self.outage or Bucket != BUCKET:
            raise FakeClientError("503")

    def put_object(self, *, Bucket, Key, Body, **arguments):
        assert Bucket == BUCKET
        if self.outage:
            raise ConnectionResetError("connection reset")
        self.puts.append((Key, arguments))
        data = Body.read()
        assert arguments["ContentLength"] == len(data)
        if base64.b64encode(hashlib.sha256(data).digest()).decode() != arguments["ChecksumSHA256"]:
            raise FakeClientError("BadDigest")
        if self.conflicts:
            self.conflicts -= 1
            raise FakeClientError("ConditionalRequestConflict")
        if arguments.get("IfNoneMatch") == "*" and Key in self.objects:
            raise FakeClientError("PreconditionFailed")
        self.objects[Key] = data

    def get_object(self, *, Bucket, Key):
        if self.outage:
            raise FakeClientError("SlowDown")
        if Key not in self.objects:
            raise FakeClientError("NoSuchKey")
        data = self.objects[Key]
        return {"Body": FakeBody(data, self.fail_read_after), "ContentLength": len(data)}


@pytest.fixture
def s3():
    return FakeS3()


def ref_for(data):
    digest = hashlib.sha256(data).hexdigest()
    return pb.ArtifactRef(key=artifact_key(digest), sha256=digest, size_bytes=len(data))


def test_s3_round_trip_uses_portable_keys_checksums_and_no_overwrite(s3):
    store = S3ArtifactStore(s3, BUCKET, prefix="staging/artifacts", kms_key_id="alias/data")
    ref = store.put(b"hello")
    assert ref == ref_for(b"hello")
    assert ref.key.startswith("sha256/")  # Same reference format as the local backend.
    key, arguments = s3.puts[0]
    assert key == "staging/artifacts/" + ref.key
    assert arguments["IfNoneMatch"] == "*"
    assert arguments["ServerSideEncryption"] == "aws:kms"
    assert arguments["SSEKMSKeyId"] == "alias/data"
    assert store.get(ref) == b"hello"
    assert store.put(b"hello") == ref  # Precondition failure deduplicates after verifying.
    assert store.get(store.put(b"")) == b""
    store.check_health()


def test_s3_without_kms_key_uses_bucket_default_encryption(s3):
    S3ArtifactStore(s3, BUCKET).put(b"x")
    assert "ServerSideEncryption" not in s3.puts[0][1]


@pytest.mark.parametrize("corruption", [b"HELLO", b"shorter or longer"])
def test_s3_corruption_is_detected_and_never_overwritten(s3, corruption):
    store = S3ArtifactStore(s3, BUCKET)
    ref = store.put(b"hello")
    s3.objects[ref.key] = corruption
    with pytest.raises(ArtifactError):
        store.get(ref)
    with pytest.raises(ArtifactError):
        store.put(b"hello")
    assert s3.objects[ref.key] == corruption


def test_s3_retries_a_racing_conditional_put(s3):
    s3.conflicts = 2
    store = S3ArtifactStore(s3, BUCKET)
    assert store.get(store.put(b"racing")) == b"racing"
    s3.conflicts = 3
    with pytest.raises(UnavailableError):
        store.put(b"still racing")


def test_s3_enforces_size_limit_before_upload(s3):
    store = S3ArtifactStore(s3, BUCKET, max_bytes=4)
    with pytest.raises(ArtifactError, match="size limit"):
        store.put(b"12345")
    assert s3.puts == []
    with pytest.raises(ArtifactError, match="size limit"):
        store.get(ref_for(b"12345"))


def test_s3_spools_large_streams_without_reading_them_into_memory(s3, monkeypatch):
    monkeypatch.setattr("compiler_optimizer.storage.s3_artifacts.SPOOL_BYTES", 1024)
    store = S3ArtifactStore(s3, BUCKET)
    data = bytes(range(256)) * 64
    assert store.get(store.put_stream(io.BytesIO(data))) == data


def test_s3_missing_invalid_and_unavailable_objects_are_distinguished(s3):
    store = S3ArtifactStore(s3, BUCKET)
    with pytest.raises(NotFoundError):
        store.get(ref_for(b"absent"))
    bad = ref_for(b"x")
    bad.key = "../../etc/passwd"
    with pytest.raises(ArtifactError, match="key"):
        store.get(bad)
    ref = store.put(b"partial read")
    s3.fail_read_after = 0
    with pytest.raises(UnavailableError):
        store.get(ref)
    s3.fail_read_after = None
    s3.outage = True
    for operation in (lambda: store.get(ref), lambda: store.put(b"new"), store.check_health):
        with pytest.raises(UnavailableError) as raised:
            operation()
        assert isinstance(raised.value, OSError)  # Existing handlers map it to UNAVAILABLE.


@pytest.mark.parametrize(
    ("bucket", "prefix"),
    [("", ""), ("Upper", ""), ("ok-bucket", "../escape"), ("ok-bucket", "a//b")],
)
def test_s3_rejects_invalid_bucket_or_prefix(s3, bucket, prefix):
    with pytest.raises(ValueError):
        S3ArtifactStore(s3, bucket, prefix=prefix)


def test_repository_commits_task_state_against_s3_artifacts(tmp_path, s3):
    repo = Repository(tmp_path / "state", artifacts=S3ArtifactStore(s3, BUCKET, prefix="dev"))
    task_id = str(uuid4())
    manifest = Path(__file__).resolve().parents[1] / "examples/sum-array/submission.json"
    submission = pb.Submission(
        meta=pb.Metadata(
            schema_version=1,
            message_id=str(uuid4()),
            task_id=task_id,
            created_at_unix_ms=1_800_000_000_000,
        ),
        manifest=parse_manifest(manifest.read_bytes()),
        package=repo.artifacts.put(b"package"),
        source=pb.SourceVersion(version=1, bundle=repo.artifacts.put(b"source")),
        compiler=pb.CompilerSettings(
            executable="clang",
            version="18.1.8",
            image_digest="sha256:" + "a" * 64,
            language="c17",
            target="linux-x86_64",
            flags=["-std=c17", *BASE_FLAGS],
        ),
    )
    assert repo.save_submission(submission)
    assert {item["kind"] for item in repo.list_artifacts(task_id)} >= {"package", "source"}
    assert not (tmp_path / "state" / "artifacts").exists()
    assert set(s3.objects) == {
        "dev/" + submission.package.key,
        "dev/" + submission.source.bundle.key,
    }
    repo.check_health()
    s3.objects["dev/" + submission.package.key] = b"tampered"
    with pytest.raises(ArtifactError):
        repo.artifacts.get(submission.package)


def test_settings_select_s3_artifacts_only_for_the_coordinator():
    env = {"ARTIFACT_BACKEND": "s3", "ARTIFACT_S3_BUCKET": BUCKET, "AWS_REGION": "us-west-2"}
    settings = Settings.from_env("coordinator", env | {"ARTIFACT_S3_PREFIX": "staging"})
    assert (settings.artifact_backend, settings.artifact_bucket) == ("s3", BUCKET)
    assert (settings.artifact_prefix, settings.aws_region) == ("staging", "us-west-2")
    assert artifact_store(Settings.from_env("coordinator", {})) is None
    with pytest.raises(ValueError, match="coordinator"):
        Settings.from_env("worker", env | {"EXECUTION_ENABLED": "false"})
    with pytest.raises(ValueError, match="BUCKET"):
        Settings.from_env("coordinator", {"ARTIFACT_BACKEND": "s3"})
    with pytest.raises(ValueError, match="ARTIFACT_BACKEND"):
        Settings.from_env("coordinator", {"ARTIFACT_BACKEND": "nfs"})


@pytest.mark.parametrize(
    ("url", "valid"),
    [
        ("https://s3.us-west-2.amazonaws.com", True),
        ("http://127.0.0.1:9000", True),
        ("http://minio.internal:9000", False),
        ("https://user:secret@s3.example", False),
        ("ftp://s3.example", False),
    ],
)
def test_settings_restrict_custom_s3_endpoints(url, valid):
    env = {"ARTIFACT_BACKEND": "s3", "ARTIFACT_S3_BUCKET": BUCKET, "S3_ENDPOINT_URL": url}
    if valid:
        assert Settings.from_env("coordinator", env).s3_endpoint_url == url
    else:
        with pytest.raises(ValueError, match="S3_ENDPOINT_URL"):
            Settings.from_env("coordinator", env)


def test_plaintext_dependencies_remain_the_local_default():
    settings = Settings.from_env("coordinator", {})
    assert (settings.rabbitmq_port, settings.rabbitmq_tls, settings.redis_tls) == (
        5672,
        False,
        False,
    )
    params = rabbitmq_parameters(
        settings, socket_timeout=2, stack_timeout=4, blocked_connection_timeout=2, heartbeat=10
    )
    assert params.ssl_options is None and params.port == 5672
    assert redis_client(settings).connection_pool.connection_kwargs.get("ssl") is None


def test_managed_dependencies_verify_tls_certificates_and_hostnames():
    settings = Settings.from_env(
        "coordinator",
        {
            "RABBITMQ_HOST": "b-1234.mq.us-west-2.on.aws",
            "RABBITMQ_TLS": "true",
            "REDIS_HOST": "master.cache.usw2.cache.amazonaws.com",
            "REDIS_TLS": "true",
            "REDIS_USERNAME": "optimizer",
        },
    )
    assert settings.rabbitmq_port == 5671  # AMQPS default for Amazon MQ.
    params = rabbitmq_parameters(
        settings, socket_timeout=2, stack_timeout=4, blocked_connection_timeout=2, heartbeat=10
    )
    context = params.ssl_options.context
    assert params.ssl_options.server_hostname == "b-1234.mq.us-west-2.on.aws"
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    kwargs = redis_client(settings).connection_pool.connection_kwargs
    assert kwargs["ssl_cert_reqs"] == "required" and kwargs["ssl_check_hostname"] is True
    assert kwargs["username"] == "optimizer"


@pytest.mark.parametrize("service", ["RABBITMQ", "REDIS"])
def test_private_ca_requires_tls(service):
    with pytest.raises(ValueError, match=service + "_CA"):
        Settings.from_env("coordinator", {service + "_CA": "/etc/ssl/private-ca.pem"})


def test_settings_repr_omits_new_credentials():
    settings = Settings.from_env("coordinator", {"REDIS_PASSWORD": "cache-secret"})
    assert "cache-secret" not in repr(settings)
