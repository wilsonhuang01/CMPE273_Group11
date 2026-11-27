"""Redis quarantine cache with exact identity validation and durable local fallback."""

import json

import redis

from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.recovery import canonical, digest, quarantine_key


class BlacklistUnavailable(RuntimeError):
    pass


class RedisBlacklist:
    def __init__(self, client):
        self.client = client

    @classmethod
    def from_settings(cls, settings):
        return cls(
            redis.Redis(
                host=settings.redis_host,
                port=settings.redis_port,
                password=settings.redis_password,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
        )

    @staticmethod
    def validate(record, key, now):
        require(
            isinstance(record, dict)
            and record.get("schema_version") == 1
            and record.get("key") == key
            and quarantine_key(record["transformation"], record["source_sha256"], record["context"])
            == key
            and record.get("context_sha256") == digest(record["context"])
            and type(record.get("expires_ms")) is int
            and record["expires_ms"] > now
            and isinstance(record.get("confirmations"), list)
            and len(record["confirmations"]) == 2,
            "invalid blacklist identity/evidence",
        )
        confirmations = record["confirmations"]
        require(
            len({item["worker_id"] for item in confirmations}) == 2
            and confirmations[0]["signature"] == confirmations[1]["signature"],
            "blacklisting requires matching crashes on two workers",
        )
        return record

    def get(self, key, now):
        try:
            raw = self.client.get(key)
            if raw is None:
                return None
            require(
                isinstance(raw, bytes | str) and len(raw) <= 65536, "blacklist record too large"
            )
            record = json.loads(raw)
            require(isinstance(record, dict), "blacklist record must be an object")
            if type(record.get("expires_ms")) is int and record["expires_ms"] <= now:
                return None
            return self.validate(record, key, now)
        except (redis.RedisError, ValueError, KeyError, TypeError) as error:
            raise BlacklistUnavailable("Redis blacklist could not be checked") from error

    def put(self, record, now):
        self.validate(record, record["key"], now)
        try:
            self.client.set(record["key"], canonical(record), nx=True, pxat=record["expires_ms"])
            existing = self.get(record["key"], now)
            require(existing is not None, "blacklist publication not visible")
        except (redis.RedisError, ValueError, KeyError, TypeError) as error:
            raise BlacklistUnavailable("Redis blacklist publication failed") from error
