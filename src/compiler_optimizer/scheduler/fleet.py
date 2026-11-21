"""Independent confirmed publisher for the coordinator's durable worker-job outbox."""

import logging

import pika

from compiler_optimizer.runtime.dependencies import DEAD_EXCHANGE, declare_topology
from compiler_optimizer.runtime.fleet_client import fleet_connection
from compiler_optimizer.runtime.proposal_queue import RabbitProposalQueue
from compiler_optimizer.shared.v1 import contracts_pb2 as pb

FLEET_EXCHANGE = "compiler-optimizer.work.v1"


def worker_queue(channel, worker_id):
    queue = "sandbox.worker." + worker_id + ".v1"
    channel.exchange_declare(exchange=FLEET_EXCHANGE, exchange_type="direct", durable=True)
    channel.queue_declare(
        queue=queue, durable=True, arguments={"x-dead-letter-exchange": DEAD_EXCHANGE}
    )
    channel.queue_bind(queue=queue, exchange=FLEET_EXCHANGE, routing_key=worker_id)
    return queue


def run_scheduler(settings, stop, ready=None):
    channel, fleet, client = fleet_connection(settings)
    connection = None
    try:
        while not stop.is_set():
            try:
                if connection is None or not connection.is_open:
                    connection = RabbitProposalQueue(settings)._connect()
                    broker = connection.channel()
                    declare_topology(broker)
                    broker.confirm_delivery()
                result = fleet.Schedule(
                    pb.FleetScheduleRequest(), timeout=5, metadata=client.metadata
                )
                if ready is not None:
                    ready.set()
                for job in result.jobs:
                    worker_queue(broker, job.worker_id)
                    confirmed = broker.basic_publish(
                        exchange=FLEET_EXCHANGE,
                        routing_key=job.worker_id,
                        body=job.SerializeToString(deterministic=True),
                        mandatory=True,
                        properties=pika.BasicProperties(
                            content_type="application/x-protobuf",
                            delivery_mode=2,
                            message_id=job.job_id,
                            type=job.DESCRIPTOR.full_name,
                        ),
                    )
                    if confirmed is False:
                        raise RuntimeError("worker dispatch not confirmed")
                connection.process_data_events(time_limit=0.05)
                stop.wait(0.05)
            except Exception as error:
                if ready is not None:
                    ready.clear()
                logging.warning("worker scheduler interrupted: %s", type(error).__name__)
                if connection is not None:
                    try:
                        connection.close()
                    except pika.exceptions.AMQPError:
                        pass
                connection = None
                stop.wait(1)
    finally:
        if ready is not None:
            ready.clear()
        if connection is not None and connection.is_open:
            connection.close()
        channel.close()
