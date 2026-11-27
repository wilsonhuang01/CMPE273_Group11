"""Broker, cache and artifact clients built from Settings (action R).

Local Compose keeps plaintext AMQP/Redis on the private bridge network. Managed AWS
services (Amazon MQ for RabbitMQ, ElastiCache) require TLS: ``RABBITMQ_TLS`` and
``REDIS_TLS`` verify the server certificate and hostname against the system trust
store, or against ``RABBITMQ_CA``/``REDIS_CA`` when a private CA is used.

With ``*_SECRET_ID`` set, credentials come from Secrets Manager each time a connection
opens (see ``runtime/secrets.py``), so rotation takes effect without a restart.
"""

import ssl

import pika
import redis

from compiler_optimizer.runtime.secrets import SecretUnavailable, rotating_secret


def _tls_context(ca) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=None if ca is None else str(ca))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def rabbitmq_credentials(settings) -> pika.PlainCredentials:
    if not settings.rabbitmq_secret_id:
        return pika.PlainCredentials(settings.rabbitmq_user, settings.rabbitmq_password)
    try:
        user, password = rotating_secret(
            settings.rabbitmq_secret_id, settings.aws_region
        ).credentials(settings.rabbitmq_user)
    except SecretUnavailable as error:
        # Callers already treat AMQP connection errors as a retryable broker outage.
        raise pika.exceptions.AMQPConnectionError(str(error)) from error
    return pika.PlainCredentials(user, password)


class SecretCredentialProvider(redis.CredentialProvider):
    """Reads the current cache token whenever redis-py opens a connection."""

    def __init__(self, secret, username=None):
        self.secret, self.username = secret, username

    def get_credentials(self):
        try:
            username, password = self.secret.credentials(self.username)
        except SecretUnavailable as error:
            raise redis.ConnectionError(str(error)) from error
        return (username, password) if username else (password,)


def rabbitmq_parameters(
    settings,
    *,
    socket_timeout: float,
    stack_timeout: float,
    blocked_connection_timeout: float,
    heartbeat: int,
) -> pika.ConnectionParameters:
    return pika.ConnectionParameters(
        host=settings.rabbitmq_host,
        port=settings.rabbitmq_port,
        virtual_host=settings.rabbitmq_vhost,
        credentials=rabbitmq_credentials(settings),
        ssl_options=pika.SSLOptions(_tls_context(settings.rabbitmq_ca), settings.rabbitmq_host)
        if settings.rabbitmq_tls
        else None,
        socket_timeout=socket_timeout,
        stack_timeout=stack_timeout,
        blocked_connection_timeout=blocked_connection_timeout,
        connection_attempts=1,
        heartbeat=heartbeat,
    )


def redis_client(settings, *, timeout: float = 2) -> redis.Redis:
    options = {}
    if settings.redis_tls:
        options = {
            "ssl": True,
            "ssl_cert_reqs": "required",
            "ssl_check_hostname": True,
            "ssl_ca_certs": None if settings.redis_ca is None else str(settings.redis_ca),
        }
    if settings.redis_secret_id:
        options["credential_provider"] = SecretCredentialProvider(
            rotating_secret(settings.redis_secret_id, settings.aws_region),
            settings.redis_username,
        )
    else:
        options["username"] = settings.redis_username
        options["password"] = settings.redis_password
    return redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
        **options,
    )


def artifact_store(settings):
    """Return the configured coordinator artifact store, or None for the local default."""
    if settings.artifact_backend == "local":
        return None
    from compiler_optimizer.storage.s3_artifacts import S3ArtifactStore, s3_client

    return S3ArtifactStore(
        s3_client(region=settings.aws_region, endpoint_url=settings.s3_endpoint_url),
        settings.artifact_bucket,
        prefix=settings.artifact_prefix,
        kms_key_id=settings.artifact_kms_key_id or None,
    )


def repository_database(settings):
    """Return the configured shared PostgreSQL database, or None for local SQLite."""
    if not settings.database_url:
        return None
    from compiler_optimizer.storage.postgres import PostgresDatabase

    return PostgresDatabase(
        settings.database_url,
        schema=settings.database_schema,
        password=settings.database_password or None,
        credentials=rotating_secret(settings.database_secret_id, settings.aws_region)
        if settings.database_secret_id
        else None,
    )
