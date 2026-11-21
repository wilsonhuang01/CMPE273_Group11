"""Authenticated worker/scheduler connections and bounded result framing."""

from compiler_optimizer.client import OptimizerClient, client_channel
from compiler_optimizer.shared.transfer import TRANSFER_CHUNK_BYTES, content_reference
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc


def fleet_connection(settings):
    channel = client_channel(
        settings.coordinator_endpoint,
        tls=settings.coordinator_tls,
        ca=settings.coordinator_ca,
        token=settings.api_token,
    )
    return (
        channel,
        rpc.FleetServiceStub(channel),
        OptimizerClient(channel, token=settings.api_token),
    )


def result_frames(identity, lease, data):
    yield pb.FleetResultFrame(
        header=pb.FleetResultHeader(
            identity=identity, lease=lease, artifact=content_reference(data)
        )
    )
    for offset in range(0, len(data), TRANSFER_CHUNK_BYTES):
        yield pb.FleetResultFrame(data=data[offset : offset + TRANSFER_CHUNK_BYTES])


def check_fleet(settings):
    channel, fleet, client = fleet_connection(settings)
    try:
        fleet.Ping(pb.FleetScheduleRequest(), timeout=3, metadata=client.metadata)
    finally:
        channel.close()
