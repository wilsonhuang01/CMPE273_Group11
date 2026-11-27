"""Secrets Manager rotation for the broker and cache credentials (action R, phase 4).

Secrets Manager calls this function four times per rotation (createSecret, setSecret,
testSecret, finishSecret). Both strategies keep the previous credential working until
the next rotation, so services that re-read the secret within minutes never see an
authentication failure:

- ``ROTATION_KIND=rabbitmq`` (runs in the VPC): alternating users. The secret is JSON
  ``{"username", "password"}``. Each rotation sets a new password on whichever of
  ``<user>`` and ``<user>-alt`` is *not* current, through the broker's RabbitMQ management
  API, authenticated as the current user. The current user's password is untouched.
- ``ROTATION_KIND=redis`` (runs outside the VPC): ElastiCache AUTH tokens. ``ROTATE``
  adds the new token while the current one stays valid; ElastiCache keeps the two most
  recent tokens, so the token from two rotations ago is the one removed.

Only the AWS SDK and the standard library are used, so the code ships as one file.
Values are never logged.
"""

import base64
import ipaddress
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

APPLIED = "APPLIED"  # Version label: the pending token was sent to ElastiCache.
PASSWORD_LENGTH = 48


class RotationError(Exception):
    pass


def _clients():
    import boto3

    return boto3.client("secretsmanager"), boto3.client("elasticache")


# --- Secrets Manager steps ---------------------------------------------------------------


def handler(event, _context, *, secrets=None, elasticache=None, opener=None, environ=None):
    environ = os.environ if environ is None else environ
    if secrets is None:
        secrets, default_elasticache = _clients()
        elasticache = elasticache or default_elasticache
    arn, token, step = event["SecretId"], event["ClientRequestToken"], event["Step"]
    versions = secrets.describe_secret(SecretId=arn).get("VersionIdsToStages", {})
    if token not in versions:
        if step == "testSecret":
            # Secrets Manager checks a new rotation configuration with a token it never
            # created; there is nothing to test yet.
            return {"step": step, "result": "configuration-check"}
        raise RotationError(f"version {token} is not part of secret {arn}")
    if "AWSCURRENT" in versions[token]:
        return {"step": step, "result": "already-current"}
    if "AWSPENDING" not in versions[token]:
        raise RotationError(f"version {token} is not pending for secret {arn}")
    kind = environ.get("ROTATION_KIND")
    if kind == "rabbitmq":
        rotator = RabbitMQRotator(environ["RABBITMQ_API_URL"], environ["RABBITMQ_USER"], opener)
    elif kind == "redis":
        rotator = RedisRotator(elasticache, environ["REPLICATION_GROUP_ID"])
    else:
        raise RotationError("ROTATION_KIND must be rabbitmq or redis")

    if step == "createSecret":
        return {"step": step, "result": create_secret(secrets, arn, token, rotator)}
    pending = _value(secrets, arn, VersionId=token, VersionStage="AWSPENDING")
    if step == "setSecret":
        current = _value(secrets, arn, VersionStage="AWSCURRENT")
        rotator.apply(secrets, arn, token, current, pending, versions)
        return {"step": step, "result": "applied"}
    if step == "testSecret":
        rotator.test(pending)
        return {"step": step, "result": "tested"}
    if step == "finishSecret":
        finish_secret(secrets, arn, token, versions)
        return {"step": step, "result": "finished"}
    raise RotationError(f"unknown step {step}")


def _value(secrets, arn, **selector):
    return secrets.get_secret_value(SecretId=arn, **selector)["SecretString"]


def _random_password(secrets):
    return secrets.get_random_password(
        PasswordLength=PASSWORD_LENGTH, ExcludePunctuation=True, RequireEachIncludedType=True
    )["RandomPassword"]


def create_secret(secrets, arn, token, rotator):
    try:
        secrets.get_secret_value(SecretId=arn, VersionId=token, VersionStage="AWSPENDING")
        return "pending-exists"
    except secrets.exceptions.ResourceNotFoundException:
        pass
    current = _value(secrets, arn, VersionStage="AWSCURRENT")
    secrets.put_secret_value(
        SecretId=arn,
        ClientRequestToken=token,
        SecretString=rotator.create(current, _random_password(secrets)),
        VersionStages=["AWSPENDING"],
    )
    return "created"


def finish_secret(secrets, arn, token, versions):
    current = next((v for v, stages in versions.items() if "AWSCURRENT" in stages), None)
    secrets.update_secret_version_stage(
        SecretId=arn,
        VersionStage="AWSCURRENT",
        MoveToVersionId=token,
        **({"RemoveFromVersionId": current} if current and current != token else {}),
    )


# --- RabbitMQ: alternating users through the management API ------------------------------


def _check_api_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "https" and parsed.hostname:
        return url.rstrip("/")
    if parsed.scheme == "http" and parsed.hostname:
        try:
            if ipaddress.ip_address(parsed.hostname).is_loopback:
                return url.rstrip("/")  # Local tests only.
        except ValueError:
            if parsed.hostname == "localhost":
                return url.rstrip("/")
    raise RotationError("RABBITMQ_API_URL must be an https:// URL")


class RabbitMQRotator:
    def __init__(self, api_url, base_user, opener=None):
        self.api = _check_api_url(api_url)
        self.users = (base_user, base_user + "-alt")
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.open = opener or (
            lambda request: urllib.request.urlopen(  # noqa: S310
                request, timeout=10, context=context
            )
        )

    @staticmethod
    def _parse(text):
        value = json.loads(text)
        if not isinstance(value, dict) or not value.get("username") or not value.get("password"):
            raise RotationError("broker secret must be JSON with username and password")
        return value

    def create(self, current_text, password):
        current = self._parse(current_text)
        if current["username"] not in self.users:
            raise RotationError(f"current broker user must be one of {self.users}")
        other = self.users[1] if current["username"] == self.users[0] else self.users[0]
        return json.dumps({"username": other, "password": password})

    def _call(self, method, path, login, body=None):
        credentials = f"{login['username']}:{login['password']}".encode()
        request = urllib.request.Request(
            self.api + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": "Basic " + base64.b64encode(credentials).decode(),
                "Content-Type": "application/json",
            },
        )
        try:
            with self.open(request) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            raise RotationError(f"{method} {path.split('/')[2]} returned {error.code}") from None

    def apply(self, _secrets, _arn, _token, current_text, pending_text, _versions):
        current, pending = self._parse(current_text), self._parse(pending_text)
        if pending["username"] == current["username"]:
            raise RotationError("pending and current broker users must differ")
        user = urllib.parse.quote(pending["username"], safe="")
        # Administrator lets the new user rotate its partner next time; the application
        # needs configure/write/read on the virtual host for its topology.
        self._call(
            "PUT",
            f"/api/users/{user}",
            current,
            {"password": pending["password"], "tags": "administrator"},
        )
        self._call(
            "PUT",
            f"/api/permissions/%2F/{user}",
            current,
            {"configure": ".*", "write": ".*", "read": ".*"},
        )

    def test(self, pending_text):
        pending = self._parse(pending_text)
        _, body = self._call("GET", "/api/whoami", pending)
        if json.loads(body).get("name") != pending["username"]:
            raise RotationError("broker authenticated a different user")


# --- ElastiCache: two-token AUTH rotation ------------------------------------------------


class RedisRotator:
    def __init__(self, elasticache, group_id, *, poll_seconds=15, timeout_seconds=780):
        self.elasticache, self.group_id = elasticache, group_id
        self.poll_seconds, self.timeout_seconds = poll_seconds, timeout_seconds

    def create(self, _current_text, password):
        return password

    def _group(self):
        groups = self.elasticache.describe_replication_groups(ReplicationGroupId=self.group_id)
        return groups["ReplicationGroups"][0]

    def wait_available(self):
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            group = self._group()
            if group["Status"] == "available":
                return group
            if time.monotonic() > deadline:
                raise RotationError(f"{self.group_id} still {group['Status']}")
            time.sleep(self.poll_seconds)

    def apply(self, secrets, arn, token, _current_text, pending_text, versions):
        # A retried setSecret must not ROTATE twice: the second call would drop the
        # current token, which clients are still using.
        if APPLIED in versions.get(token, []):
            self.wait_available()
            return
        self.wait_available()
        self.elasticache.modify_replication_group(
            ReplicationGroupId=self.group_id,
            AuthToken=pending_text,
            AuthTokenUpdateStrategy="ROTATE",
            ApplyImmediately=True,
        )
        previous = next((v for v, stages in versions.items() if APPLIED in stages), None)
        secrets.update_secret_version_stage(
            SecretId=arn,
            VersionStage=APPLIED,
            MoveToVersionId=token,
            **({"RemoveFromVersionId": previous} if previous else {}),
        )
        self.wait_available()

    def test(self, _pending_text):
        # This function has no VPC access; `./project infra validate` checks connections.
        group = self.wait_available()
        if not group.get("AuthTokenEnabled") or not group.get("TransitEncryptionEnabled"):
            raise RotationError(f"{self.group_id} does not enforce AUTH over TLS")


def lambda_handler(event, context):
    return handler(event, context)
