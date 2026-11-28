"""Confirmed durable RabbitMQ delivery with acknowledgement after coordinator commit."""

import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pika

from compiler_optimizer.monitoring.events import emit
from compiler_optimizer.runtime.connections import rabbitmq_parameters
from compiler_optimizer.runtime.dependencies import DEAD_EXCHANGE, declare_topology
from compiler_optimizer.shared.agent_roles import role_spec
from compiler_optimizer.shared.manifest import ContractError, require
from compiler_optimizer.shared.messaging import EXCHANGE, decode_event, encode_event, encode_message
from compiler_optimizer.shared.recovery import DeadLetterResult, RetryDelivery


class DeliveryError(RuntimeError):
    """A proposal has not completed its confirmed delivery; durable state can be resumed."""


class RabbitProposalQueue:
    def __init__(self, settings, *, delivery_timeout=15):
        if not 1 <= delivery_timeout <= 120:
            raise ValueError("delivery timeout must be 1–120 seconds")
        self.settings, self.delivery_timeout = settings, delivery_timeout

    def _connect(self):
        return pika.BlockingConnection(
            rabbitmq_parameters(
                self.settings,
                socket_timeout=3,
                stack_timeout=5,
                blocked_connection_timeout=5,
                heartbeat=30,
            )
        )

    @staticmethod
    def _publish_recovery(channel, proposal, kind, event_id, *, delay_ms=1, reason="retry"):
        event = encode_event(proposal)
        route = event.routing_key + "." + proposal.proposal_id
        queue = role_spec(proposal.role).name + "." + proposal.proposal_id + ".v1"
        properties = pika.BasicProperties(
            content_type=event.content_type,
            delivery_mode=2,
            type=event.message_type,
            message_id=event.message_id,
            correlation_id=event.correlation_id,
            headers={
                "recovery-event-id": event_id,
                "recovery-reason": reason,
                "source-sha256": proposal.source.bundle.sha256,
                "transformation": proposal.transformation,
            },
        )
        if kind == "retry":
            channel.queue_declare(
                queue=queue, durable=True, arguments={"x-dead-letter-exchange": DEAD_EXCHANGE}
            )
            channel.queue_bind(queue=queue, exchange=EXCHANGE, routing_key=route)
            retry_queue = "recovery.retry." + proposal.proposal_id + ".v1"
            channel.queue_declare(
                queue=retry_queue,
                durable=True,
                arguments={"x-dead-letter-exchange": EXCHANGE, "x-dead-letter-routing-key": route},
            )
            properties.expiration = str(max(1, int(delay_ms)))
            accepted = channel.basic_publish(
                exchange="",
                routing_key=retry_queue,
                body=event.body,
                properties=properties,
                mandatory=True,
            )
        else:
            if kind != "deadletter":
                raise ValueError("unknown recovery notification")
            accepted = channel.basic_publish(
                exchange=DEAD_EXCHANGE,
                routing_key="recovery." + reason,
                body=event.body,
                properties=properties,
                mandatory=True,
            )
        if accepted is False:
            raise DeliveryError("broker did not confirm recovery notification")

    def publish_recovery_event(self, proposal, kind, event_id, *, delay_ms=1, reason="retry"):
        connection = None
        try:
            connection = self._connect()
            channel = connection.channel()
            declare_topology(channel)
            channel.confirm_delivery()
            self._publish_recovery(
                channel, proposal, kind, event_id, delay_ms=delay_ms, reason=reason
            )
        except pika.exceptions.AMQPError as error:
            raise DeliveryError("recovery outbox publication interrupted") from error
        finally:
            if connection is not None and connection.is_open:
                try:
                    connection.close()
                except pika.exceptions.AMQPError:
                    pass

    def exchange(self, proposal, process, on_published):
        """Use a scoped durable queue so the milestone does not consume another scheduler's jobs.

        The main thread owns all Pika calls and services heartbeats while the worker runs.
        A reconnect/resume may redeliver; process must recognize committed results.
        """
        event = encode_event(proposal)
        route = event.routing_key + "." + proposal.proposal_id
        queue = role_spec(proposal.role).name + "." + proposal.proposal_id + ".v1"
        connection = None
        try:
            connection = self._connect()
            channel = connection.channel()
            declare_topology(channel)
            channel.queue_declare(
                queue=queue, durable=True, arguments={"x-dead-letter-exchange": DEAD_EXCHANGE}
            )
            channel.queue_bind(queue=queue, exchange=EXCHANGE, routing_key=route)
            channel.basic_qos(prefetch_count=1)
            channel.confirm_delivery()
            accepted = channel.basic_publish(
                exchange=EXCHANGE,
                routing_key=route,
                body=event.body,
                mandatory=True,
                properties=pika.BasicProperties(
                    content_type=event.content_type,
                    delivery_mode=2,
                    type=event.message_type,
                    message_id=event.message_id,
                    correlation_id=event.correlation_id,
                ),
            )
            if accepted is False:
                raise DeliveryError("broker did not confirm proposal")
            on_published()
            deadline = time.monotonic() + self.delivery_timeout
            while time.monotonic() < deadline:
                method, properties, body = channel.basic_get(queue=queue, auto_ack=False)
                if method is None:
                    connection.process_data_events(time_limit=0.1)
                    continue
                try:
                    incoming = decode_event(event.routing_key, body)
                    require(
                        encode_message(incoming) == event.body
                        and properties.content_type == event.content_type
                        and properties.delivery_mode == 2
                        and properties.message_id == event.message_id
                        and properties.correlation_id == event.correlation_id
                        and properties.type == event.message_type,
                        "delivery differs from the durable coordinator proposal",
                    )
                except ContractError:
                    emit(
                        "delivery_rejected",
                        task_id=proposal.meta.task_id,
                        proposal_id=proposal.proposal_id,
                        reason="invalid_envelope",
                    )
                    channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                    continue
                try:
                    emit(
                        "delivery_received",
                        task_id=proposal.meta.task_id,
                        proposal_id=proposal.proposal_id,
                        redelivered=bool(getattr(method, "redelivered", False)),
                    )
                    remaining = getattr(method, "message_count", None)
                    if type(remaining) is int:
                        emit(
                            "queue_depth",
                            task_id=proposal.meta.task_id,
                            proposal_id=proposal.proposal_id,
                            measurements={"remaining_ready": remaining},
                        )
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        # Keep the caller's dispatch claim, so result commits stay fenced.
                        future = executor.submit(copy_context().run, process, incoming)
                        while not future.done():
                            connection.process_data_events(time_limit=0.2)
                        result = future.result()
                except RetryDelivery as retry:
                    # The retry intent already exists in SQLite. Confirm its durable broker copy
                    # before acknowledging this delivery; duplicate copies remain safe on reconnect.
                    self._publish_recovery(
                        channel, proposal, "retry", retry.event_id, delay_ms=retry.delay_ms
                    )
                    retry.confirmed()
                    emit(
                        "retry_published",
                        task_id=proposal.meta.task_id,
                        proposal_id=proposal.proposal_id,
                        event_id=retry.event_id,
                        delay_ms=retry.delay_ms,
                    )
                    channel.basic_ack(delivery_tag=method.delivery_tag)
                    deadline = time.monotonic() + retry.delay_ms / 1000 + self.delivery_timeout
                    continue
                except BaseException:
                    if channel.is_open:
                        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                    raise
                if isinstance(result, DeadLetterResult):
                    self._publish_recovery(
                        channel, proposal, "deadletter", result.event_id, reason=result.reason
                    )
                    result.confirmed()
                    result = result.result
                channel.basic_ack(delivery_tag=method.delivery_tag)
                emit(
                    "delivery_acknowledged",
                    task_id=proposal.meta.task_id,
                    proposal_id=proposal.proposal_id,
                )
                # Only duplicate copies of this immutable proposal can remain in this scoped queue.
                channel.queue_delete(queue=queue, if_unused=True)
                retry_queue = "recovery.retry." + proposal.proposal_id + ".v1"
                channel.queue_declare(
                    queue=retry_queue,
                    durable=True,
                    arguments={
                        "x-dead-letter-exchange": EXCHANGE,
                        "x-dead-letter-routing-key": route,
                    },
                )
                channel.queue_delete(queue=retry_queue, if_unused=True)
                return result
            raise DeliveryError("proposal delivery timed out; resume using the stored task ID")
        except pika.exceptions.AMQPError as error:
            emit(
                "broker_interrupted",
                task_id=proposal.meta.task_id,
                proposal_id=proposal.proposal_id,
                reason=type(error).__name__,
            )
            raise DeliveryError(
                "RabbitMQ delivery interrupted; resume using the stored task ID"
            ) from error
        finally:
            if connection is not None and connection.is_open:
                try:
                    connection.close()
                except pika.exceptions.AMQPError:
                    pass
