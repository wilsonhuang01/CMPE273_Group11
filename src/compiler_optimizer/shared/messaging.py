"""Validated protobuf serialization and RabbitMQ routing, without a broker client."""

from dataclasses import dataclass

from google.protobuf.message import DecodeError, Message

from compiler_optimizer.shared.contracts import MAX_MESSAGE_BYTES, validate_message
from compiler_optimizer.shared.manifest import ContractError, require
from compiler_optimizer.shared.v1 import contracts_pb2 as pb

EXCHANGE = "compiler-optimizer.v1"
ROUTES = {
    "optimization.proposal.v1": pb.Proposal,
    "verification.job.v1": pb.VerificationJob,
    "verification.result.v1": pb.VerificationResult,
    "worker.heartbeat.v1": pb.WorkerHeartbeat,
}


@dataclass(frozen=True)
class OutgoingEvent:
    routing_key: str
    body: bytes
    message_type: str
    message_id: str
    correlation_id: str
    content_type: str = "application/x-protobuf"
    delivery_mode: int = 2


def encode_message(message: Message) -> bytes:
    validate_message(message)
    return message.SerializeToString(deterministic=True)


def decode_message(message_type: type[Message], body: bytes) -> Message:
    require(
        isinstance(body, bytes) and len(body) <= MAX_MESSAGE_BYTES,
        "payload must be bytes and at most 1 MiB",
    )
    try:
        message = message_type.FromString(body)
    except DecodeError as error:
        raise ContractError("malformed protobuf payload") from error
    validate_message(message)
    return message


def encode_event(message: Message) -> OutgoingEvent:
    route = next((route for route, kind in ROUTES.items() if isinstance(message, kind)), None)
    require(route is not None, "message has no RabbitMQ route")
    return OutgoingEvent(
        routing_key=route,
        body=encode_message(message),
        message_type=message.DESCRIPTOR.full_name,
        message_id=message.meta.message_id,
        correlation_id=message.meta.task_id,
    )


def decode_event(routing_key: str, body: bytes) -> Message:
    require(routing_key in ROUTES, "unknown RabbitMQ routing key")
    return decode_message(ROUTES[routing_key], body)
