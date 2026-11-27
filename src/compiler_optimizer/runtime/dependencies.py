"""Bounded dependency probes and idempotent RabbitMQ topology setup."""

import json
from urllib.request import urlopen

import pika

from compiler_optimizer.runtime.connections import rabbitmq_parameters, redis_client
from compiler_optimizer.shared.messaging import EXCHANGE

QUEUES = {
    "optimization.proposals.v1": "optimization.proposal.v1",
    "verification.jobs.v1": "verification.job.v1",
    "verification.results.v1": "verification.result.v1",
    "worker.heartbeats.v1": "worker.heartbeat.v1",
}
DEAD_EXCHANGE = EXCHANGE + ".dead"
DEAD_QUEUE = "deadletters.v1"


def declare_topology(channel) -> None:
    channel.exchange_declare(exchange=EXCHANGE, exchange_type="topic", durable=True)
    channel.exchange_declare(exchange=DEAD_EXCHANGE, exchange_type="topic", durable=True)
    channel.queue_declare(queue=DEAD_QUEUE, durable=True)
    channel.queue_bind(queue=DEAD_QUEUE, exchange=DEAD_EXCHANGE, routing_key="#")
    for queue, route in QUEUES.items():
        channel.queue_declare(
            queue=queue, durable=True, arguments={"x-dead-letter-exchange": DEAD_EXCHANGE}
        )
        channel.queue_bind(queue=queue, exchange=EXCHANGE, routing_key=route)


def check_rabbitmq(settings, *, setup=False) -> None:
    params = rabbitmq_parameters(
        settings, socket_timeout=2, stack_timeout=4, blocked_connection_timeout=2, heartbeat=10
    )
    connection = pika.BlockingConnection(params)
    try:
        channel = connection.channel()
        if setup:
            declare_topology(channel)
    finally:
        if connection.is_open:
            connection.close()


def check_redis(settings) -> None:
    with redis_client(settings) as client:
        if not client.ping():
            raise RuntimeError("Redis PING failed")


def check_coordinator(settings) -> None:
    with urlopen(settings.coordinator_health_url, timeout=2) as response:
        payload = json.loads(response.read(16384))
    if payload.get("service") != "coordinator" or not payload.get("infrastructure_ready"):
        raise RuntimeError("coordinator is not ready")
