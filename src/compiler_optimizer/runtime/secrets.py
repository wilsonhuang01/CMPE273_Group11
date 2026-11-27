"""Credentials read from AWS Secrets Manager when connections open (action R, phase 4).

Values injected as environment variables at start-up go stale when a secret rotates.
With ``RABBITMQ_SECRET_ID``, ``REDIS_SECRET_ID`` or ``DATABASE_SECRET_ID`` set, each new
connection instead reads the current value through a short-lived per-process cache.

The rotation design keeps the previous broker and cache credential valid until the next
rotation, so refreshing within the cache lifetime is enough there. The RDS-managed
database secret changes the password in place, so the PostgreSQL backend also refreshes
immediately after an authentication failure.

A secret is either a JSON object with ``password`` (and optionally ``username``), as
written by RDS and the broker rotation, or a plain string holding only the password.
"""

import json
import threading
import time

DEFAULT_TTL_SECONDS = 60.0
MIN_REFRESH_SECONDS = 2.0


class SecretUnavailable(ConnectionError):
    """The secret store could not be read or returned an unusable value."""


def parse_secret(text: str) -> dict:
    if not isinstance(text, str) or not text:
        raise SecretUnavailable("secret has no string value")
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            value = json.loads(stripped)
        except ValueError as error:
            raise SecretUnavailable("secret is not valid JSON") from error
        password = value.get("password")
        username = value.get("username")
        if not isinstance(password, str) or not password:
            raise SecretUnavailable("secret JSON has no password")
        if username is not None and (not isinstance(username, str) or not username):
            raise SecretUnavailable("secret JSON has an invalid username")
        return {"username": username, "password": password}
    return {"username": None, "password": text}


class RotatingSecret:
    """Cached view of one secret's AWSCURRENT value. Values are never logged or shown."""

    def __init__(
        self,
        secret_id: str,
        *,
        region: str | None = None,
        ttl: float = DEFAULT_TTL_SECONDS,
        client=None,
        clock=time.monotonic,
    ):
        if not secret_id:
            raise ValueError("secret ID is required")
        self.secret_id, self.region, self.ttl = secret_id, region, ttl
        self._client, self._clock = client, clock
        self._value, self._fetched, self._lock = None, None, threading.Lock()

    def __repr__(self):
        return f"RotatingSecret({self.secret_id!r})"

    def _secrets_client(self):
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as error:
                raise SecretUnavailable(
                    "secret IDs require boto3: pip install -e '.[aws]'"
                ) from error
            self._client = boto3.client(
                "secretsmanager",
                region_name=self.region,
                config=Config(connect_timeout=3, read_timeout=5, retries={"mode": "standard"}),
            )
        return self._client

    def _fetch(self) -> dict:
        try:
            response = self._secrets_client().get_secret_value(SecretId=self.secret_id)
        except SecretUnavailable:
            raise
        except Exception as error:  # noqa: BLE001 - any client failure is an outage here
            raise SecretUnavailable(
                f"could not read secret {self.secret_id}: {type(error).__name__}"
            ) from None
        return parse_secret(response.get("SecretString", ""))

    def value(self) -> dict:
        with self._lock:
            now = self._clock()
            if self._value is None or now - self._fetched >= self.ttl:
                self._value, self._fetched = self._fetch(), now
            return dict(self._value)

    def refresh(self) -> dict:
        """Re-read after an authentication failure, at most once every few seconds."""
        with self._lock:
            now = self._clock()
            if self._value is None or now - self._fetched >= MIN_REFRESH_SECONDS:
                self._value, self._fetched = self._fetch(), now
            return dict(self._value)

    def credentials(self, default_username: str | None) -> tuple[str | None, str]:
        value = self.value()
        return value["username"] or default_username, value["password"]


_CACHE: dict[tuple[str, str | None], RotatingSecret] = {}
_CACHE_LOCK = threading.Lock()


def rotating_secret(secret_id: str, region: str | None = None) -> RotatingSecret:
    """Process-wide cache so every connection to a service shares one secret view."""
    with _CACHE_LOCK:
        key = (secret_id, region)
        if key not in _CACHE:
            _CACHE[key] = RotatingSecret(secret_id, region=region)
        return _CACHE[key]
