"""Check the broker, cache, database and artifact store against the deployed engines.

Action R, phase 4. The checks run the same client code and message patterns that the
coordinator uses, against whatever ``Settings.from_env("coordinator")`` points to:

- **Broker:** TLS/credentials, the real topology, publisher confirms (routed and
  unroutable mandatory publishes), consumer acknowledgements and requeue, rejection to
  the dead-letter exchange, and the per-message TTL retry queue used by the proposal
  queue (expiry dead-letters back to the main exchange).
- **Cache:** TLS/credentials, refusal of unauthenticated clients, and the quarantine
  commands (``SET NX PXAT``/``GET``) through ``RedisBlacklist``, including no-overwrite
  and expiry.
- **Database:** TLS, the full schema migration in a throwaway schema, read/write
  transactions, and refusal of unencrypted connections when TLS is expected.
- **Artifacts:** an ``S3ArtifactStore`` round trip and its no-overwrite check.

Everything a check creates is scoped by a random run ID and removed afterwards, except
small S3 check objects, which cannot be deleted where Object Lock applies.

Locally: ``python -m compiler_optimizer.runtime.service_check`` with the usual variables.
In AWS: ``./project infra validate --env <name>`` invokes the in-VPC Lambda function that
calls ``lambda_handler``.
"""

import json
import os
import ssl
import sys
import time
import uuid

CHECK_PREFIX = "service-check"
SERVICES = ("broker", "cache", "database", "artifacts")


class CheckFailed(AssertionError):
    pass


def expect(condition, message):
    if not condition:
        raise CheckFailed(message)


class Report:
    def __init__(self):
        self.checks, self.versions = [], {}

    def run(self, name, function):
        started = time.monotonic()
        try:
            detail = function()
            self.checks.append({"name": name, "ok": True, "detail": detail or ""})
        except Exception as error:  # noqa: BLE001 - every failure is reported, not raised
            self.checks.append(
                {"name": name, "ok": False, "detail": f"{type(error).__name__}: {error}"[:500]}
            )
        self.checks[-1]["seconds"] = round(time.monotonic() - started, 3)

    @property
    def ok(self):
        return bool(self.checks) and all(check["ok"] for check in self.checks)

    def as_dict(self):
        return {"ok": self.ok, "versions": self.versions, "checks": self.checks}


# --- Broker ------------------------------------------------------------------------


def _wait_for_message(channel, queue, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        method, properties, body = channel.basic_get(queue=queue, auto_ack=False)
        if method is not None:
            return method, properties, body
        time.sleep(0.05)
    return None, None, None


def check_broker(settings, report, run_id):
    import pika

    from compiler_optimizer.runtime.connections import rabbitmq_parameters
    from compiler_optimizer.runtime.dependencies import declare_topology

    parameters = rabbitmq_parameters(
        settings, socket_timeout=5, stack_timeout=10, blocked_connection_timeout=10, heartbeat=30
    )
    connection = pika.BlockingConnection(parameters)
    queues, exchanges = [], []
    # Private exchanges keep check messages away from the real queues, including the
    # catch-all dead-letter queue. They use the same types and arguments as production.
    EXCHANGE, DEAD_EXCHANGE = f"{CHECK_PREFIX}.{run_id}", f"{CHECK_PREFIX}.{run_id}.dead"
    try:
        server = connection._impl.server_properties  # noqa: SLF001 - pika exposes no accessor
        version = server.get("version", b"")
        report.versions["rabbitmq"] = version.decode() if isinstance(version, bytes) else version
        channel = connection.channel()
        channel.confirm_delivery()
        route = f"service.check.{run_id}"
        for exchange in (EXCHANGE, DEAD_EXCHANGE):
            channel.exchange_declare(exchange=exchange, exchange_type="topic", durable=True)
            exchanges.append(exchange)

        def declare(name, **arguments):
            channel.queue_declare(queue=name, durable=True, arguments=arguments or None)
            queues.append(name)

        def connected():
            expect(connection.is_open, "connection closed")
            return (
                f"{'AMQPS' if settings.rabbitmq_tls else 'AMQP'} "
                f"{settings.rabbitmq_host}:{settings.rabbitmq_port}"
            )

        def topology():
            declare_topology(channel)
            declare_topology(channel)  # Redeclaring with identical arguments must succeed.

        main, dead = f"{CHECK_PREFIX}.{run_id}.v1", f"{CHECK_PREFIX}.{run_id}.dead.v1"

        def confirms():
            declare(main, **{"x-dead-letter-exchange": DEAD_EXCHANGE})
            channel.queue_bind(queue=main, exchange=EXCHANGE, routing_key=route)
            channel.basic_publish(
                exchange=EXCHANGE,
                routing_key=route,
                body=b"confirmed",
                properties=pika.BasicProperties(delivery_mode=2, message_id=run_id),
                mandatory=True,
            )
            try:
                channel.basic_publish(
                    exchange=EXCHANGE,
                    routing_key=f"{route}.unroutable",
                    body=b"unroutable",
                    mandatory=True,
                )
            except pika.exceptions.UnroutableError:
                return "routed publish confirmed; unroutable mandatory publish returned"
            raise CheckFailed("unroutable mandatory publish was not returned")

        def acknowledgements():
            method, _, body = _wait_for_message(channel, main, 5)
            expect(body == b"confirmed", "confirmed message not delivered")
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            method, _, body = _wait_for_message(channel, main, 5)
            expect(body == b"confirmed" and method.redelivered, "requeued message not redelivered")
            channel.basic_ack(delivery_tag=method.delivery_tag)
            expect(
                channel.queue_declare(queue=main, passive=True).method.message_count == 0,
                "acknowledged message still queued",
            )
            return "nack requeued with redelivered flag; ack removed the message"

        def dead_letters():
            declare(dead)
            channel.queue_bind(queue=dead, exchange=DEAD_EXCHANGE, routing_key=route)
            channel.basic_publish(
                exchange=EXCHANGE, routing_key=route, body=b"rejected", mandatory=True
            )
            method, _, _ = _wait_for_message(channel, main, 5)
            expect(method is not None, "message to reject not delivered")
            channel.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
            method, properties, body = _wait_for_message(channel, dead, 5)
            expect(body == b"rejected", "rejected message not dead-lettered")
            deaths = (properties.headers or {}).get("x-death") or []
            expect(deaths and deaths[0].get("reason") == "rejected", "x-death reason missing")
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return "reject(requeue=false) reached the dead-letter exchange with x-death"

        def retry_ttl():
            # The proposal queue's retry pattern: an unconsumed queue whose messages expire
            # and dead-letter back to the main exchange with the original route.
            retry = f"{CHECK_PREFIX}.retry.{run_id}.v1"
            declare(
                retry,
                **{"x-dead-letter-exchange": EXCHANGE, "x-dead-letter-routing-key": route},
            )
            started = time.monotonic()
            channel.basic_publish(
                exchange="",
                routing_key=retry,
                body=b"delayed",
                properties=pika.BasicProperties(delivery_mode=2, expiration="400"),
                mandatory=True,
            )
            method, _, body = _wait_for_message(channel, main, 10)
            elapsed = time.monotonic() - started
            expect(body == b"delayed", "expired retry message did not return to the main queue")
            expect(elapsed >= 0.35, f"retry delivered after {elapsed:.2f}s, before its TTL")
            channel.basic_ack(delivery_tag=method.delivery_tag)
            channel.queue_delete(queue=retry, if_unused=True)
            queues.remove(retry)
            return f"400 ms TTL retry delivered after {elapsed:.2f}s"

        report.run("broker.connect", connected)
        report.run("broker.topology", topology)
        report.run("broker.publisher_confirms", confirms)
        report.run("broker.acknowledgements", acknowledgements)
        report.run("broker.dead_letter", dead_letters)
        report.run("broker.retry_ttl", retry_ttl)
    finally:
        try:
            if connection.is_open:
                cleanup = connection.channel()
                for queue in queues:
                    cleanup.queue_delete(queue=queue)
                for exchange in exchanges:
                    cleanup.exchange_delete(exchange=exchange)
                connection.close()
        except Exception:  # noqa: BLE001 - cleanup must not mask the report
            pass


# --- Cache ---------------------------------------------------------------------------


def check_cache(settings, report, run_id):
    import redis

    from compiler_optimizer.runtime.connections import redis_client
    from compiler_optimizer.scheduler.blacklist import RedisBlacklist
    from compiler_optimizer.shared.recovery import digest, quarantine_key

    client = redis_client(settings, timeout=5)

    def connected():
        expect(client.ping(), "PING failed")
        info = client.info("server")
        report.versions["cache"] = (
            f"valkey {info['valkey_version']}"
            if "valkey_version" in info
            else f"redis {info.get('redis_version', 'unknown')}"
        )
        return f"{'TLS' if settings.redis_tls else 'plaintext'} {settings.redis_host}"

    def unauthenticated():
        options = {"ssl": True, "ssl_cert_reqs": "required"} if settings.redis_tls else {}
        if settings.redis_ca:
            options["ssl_ca_certs"] = str(settings.redis_ca)
        anonymous = redis.Redis(
            host=settings.redis_host, port=settings.redis_port, socket_timeout=5, **options
        )
        try:
            anonymous.get(f"{CHECK_PREFIX}:{run_id}")
        except redis.AuthenticationError:
            return "unauthenticated GET refused"
        except redis.ResponseError as error:
            expect("NOAUTH" in str(error) or "NOPERM" in str(error), str(error))
            return "unauthenticated GET refused"
        finally:
            anonymous.close()
        raise CheckFailed("an unauthenticated client could read the cache")

    def quarantine():
        blacklist = RedisBlacklist(client)
        context = {"service_check": run_id}
        source = "0" * 64
        key = quarantine_key("service-check", source, context)
        now = int(time.time() * 1000)

        def record(signature, expires_ms):
            return {
                "schema_version": 1,
                "key": key,
                "transformation": "service-check",
                "source_sha256": source,
                "context": context,
                "context_sha256": digest(context),
                "expires_ms": expires_ms,
                "confirmations": [
                    {"worker_id": "check-a", "signature": signature},
                    {"worker_id": "check-b", "signature": signature},
                ],
            }

        try:
            first = record("first", now + 1500)
            blacklist.put(first, now)
            expect(
                blacklist.get(key, now)["confirmations"] == first["confirmations"],
                "published quarantine not readable",
            )
            blacklist.put(record("second", now + 60_000), now)
            expect(
                blacklist.get(key, now)["confirmations"][0]["signature"] == "first",
                "SET NX overwrote an existing quarantine",
            )
            remaining = client.pttl(key)
            expect(0 < remaining <= 1500, f"PXAT expiry not applied (pttl {remaining})")
            deadline = time.monotonic() + 5
            while client.exists(key) and time.monotonic() < deadline:
                time.sleep(0.1)
            expect(not client.exists(key), "quarantine did not expire at its PXAT deadline")
            return "SET NX PXAT publish, no overwrite and expiry verified"
        finally:
            client.delete(key)

    try:
        report.run("cache.connect", connected)
        if report.checks[-1]["ok"]:  # Later checks only repeat a connection failure.
            report.run("cache.requires_auth", unauthenticated)
            report.run("cache.quarantine_commands", quarantine)
    finally:
        client.close()


# --- Database ------------------------------------------------------------------------


def check_database(settings, report, run_id):
    from compiler_optimizer.runtime.connections import repository_database
    from compiler_optimizer.storage.postgres import SCHEMA_VERSION, PostgresDatabase

    configured = repository_database(settings)
    schema = f"service_check_{run_id[:12]}"
    database = PostgresDatabase(
        configured.url,
        schema=schema,
        password=configured._password,  # noqa: SLF001 - reuse the configured login
        credentials=configured._credentials,  # noqa: SLF001
        pool_size=1,
    )
    remote_tls = "sslmode=verify-full" in configured.url

    def connected():
        with database.transaction(write=False) as db:
            version = db.execute("SHOW server_version").fetchone()[0]
            ssl_on = db.execute(
                "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
            ).fetchone()[0]
        report.versions["postgresql"] = version
        expect(ssl_on or not remote_tls, "connection is not encrypted")
        return f"{'TLS' if ssl_on else 'plaintext'}, server {version}"

    def migrate():
        expect(database.migrate() == SCHEMA_VERSION, "migration did not reach the current version")
        database.check()
        with database.transaction() as db:
            db.execute("INSERT INTO schema_version VALUES (?, ?)", (-1, int(time.time() * 1000)))
        with database.transaction(write=False) as db:
            expect(
                db.execute("SELECT COUNT(*) FROM schema_version WHERE version = -1").fetchone()[0]
                == 1,
                "committed write not visible",
            )
        return f"schema migrated to version {SCHEMA_VERSION} in {schema}"

    def refuses_plaintext():
        import psycopg

        if not remote_tls:
            return "skipped: TLS not expected for this database"
        secret = configured._credentials.value() if configured._credentials else {}  # noqa: SLF001
        login = {"password": secret.get("password") or configured._password}  # noqa: SLF001
        if secret.get("username"):
            login["user"] = secret["username"]
        plain = configured.url.replace("sslmode=verify-full", "sslmode=disable")
        try:
            psycopg.connect(plain, connect_timeout=5, **login).close()
        except psycopg.OperationalError:
            return "unencrypted connection refused"
        raise CheckFailed("the database accepted an unencrypted connection")

    try:
        report.run("database.connect", connected)
        if report.checks[-1]["ok"]:
            report.run("database.migrate", migrate)
            report.run("database.requires_tls", refuses_plaintext)
    finally:
        try:
            with database.transaction() as db:
                db.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        except Exception:  # noqa: BLE001 - cleanup must not mask the report
            pass
        database.close()
        configured.close()


# --- Artifacts -----------------------------------------------------------------------


def check_artifacts(settings, report, run_id):
    from compiler_optimizer.storage.s3_artifacts import S3ArtifactStore, s3_client

    prefix = "/".join(part for part in (settings.artifact_prefix, CHECK_PREFIX) if part)
    store = S3ArtifactStore(
        s3_client(region=settings.aws_region, endpoint_url=settings.s3_endpoint_url),
        settings.artifact_bucket,
        prefix=prefix,
        kms_key_id=settings.artifact_kms_key_id or None,
    )

    def round_trip():
        store.check_health()
        data = f"service check {run_id}".encode()
        ref = store.put(data)
        expect(store.get(ref) == data, "artifact read back differently")
        expect(store.put(data) == ref, "identical bytes did not deduplicate")
        return f"s3://{settings.artifact_bucket}/{prefix}/{ref.key}"

    report.run("artifacts.round_trip", round_trip)


# --- Entry points -------------------------------------------------------------------


def run_checks(settings, services=SERVICES) -> dict:
    report = Report()
    run_id = uuid.uuid4().hex
    report.versions["tls"] = ssl.OPENSSL_VERSION
    plan = {
        "broker": (check_broker, True),
        "cache": (check_cache, True),
        "database": (check_database, bool(settings.database_url)),
        "artifacts": (check_artifacts, settings.artifact_backend == "s3"),
    }
    for service in services:
        if service not in plan:
            raise ValueError(f"unknown service {service!r}; choose from {SERVICES}")
        function, configured = plan[service]
        if not configured:
            report.checks.append(
                {"name": f"{service}.configured", "ok": True, "detail": "skipped: not configured"}
            )
            continue
        try:
            function(settings, report, run_id)
        except Exception as error:  # noqa: BLE001 - a connection failure fails that service
            report.checks.append(
                {
                    "name": f"{service}.connect",
                    "ok": False,
                    "detail": f"{type(error).__name__}: {error}"[:500],
                }
            )
    return report.as_dict()


def lambda_handler(event, _context):
    """In-VPC entry point. Settings come from the function's environment."""
    from compiler_optimizer.runtime.config import Settings

    services = (event or {}).get("services") or SERVICES
    return run_checks(Settings.from_env("coordinator", os.environ), tuple(services))


def main(argv=None):
    from compiler_optimizer.runtime.config import Settings

    services = tuple(argv if argv is not None else sys.argv[1:]) or SERVICES
    result = run_checks(Settings.from_env("coordinator"), services)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
