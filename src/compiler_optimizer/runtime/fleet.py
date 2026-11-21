"""RPC boundary for worker registration, fenced execution and scheduler dispatch."""

import hashlib
import tempfile

from compiler_optimizer.runtime.transfer import transfer_errors
from compiler_optimizer.shared.fleet import MAX_RESULT_BYTES
from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.transfer import TRANSFER_CHUNK_BYTES, validate_reference
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc


class FleetService(rpc.FleetServiceServicer):
    def __init__(self, repository):
        self.repository = repository

    def Ping(self, request, context):
        with transfer_errors(context):
            self.repository.check_health()
            return pb.FleetAck(accepted=True)

    def Register(self, request, context):
        with transfer_errors(context):
            self.repository.register_fleet_worker(request.identity, request.profile_json)
            return pb.FleetAck(accepted=True)

    def Heartbeat(self, request, context):
        with transfer_errors(context):
            return self.repository.fleet_heartbeat(request)

    def Schedule(self, request, context):
        with transfer_errors(context):
            return pb.FleetDispatches(
                jobs=[
                    pb.FleetDispatch(
                        job_id=row["job_id"], worker_id=row["worker_id"], fence=row["fence"]
                    )
                    for row in self.repository.schedule_fleet()
                ]
            )

    def Claim(self, request, context):
        with transfer_errors(context):
            return self.repository.claim_fleet(request)

    def CheckLease(self, request, context):
        with transfer_errors(context):
            self.repository.check_fleet_authority(request.identity, request.lease)
            return pb.FleetAck(accepted=True)

    def ConfirmCleanup(self, request, context):
        with transfer_errors(context):
            self.repository.confirm_fleet_cleanup(request.identity, request.lease)
            return pb.FleetAck(accepted=True)

    def UploadResult(self, requests, context):
        with transfer_errors(context):
            remaining = context.time_remaining()
            require(
                remaining is not None and 0 < remaining <= 120, "bounded result deadline required"
            )
            header, size, digest = None, 0, hashlib.sha256()
            with tempfile.SpooledTemporaryFile(max_size=1024**2) as output:
                for index, frame in enumerate(requests):
                    require(index < 1024 and context.is_active(), "result frame/deadline limit")
                    if index == 0:
                        require(frame.WhichOneof("frame") == "header", "result header required")
                        header = frame.header
                        validate_reference(header.artifact, MAX_RESULT_BYTES)
                        self.repository.check_fleet_authority(header.identity, header.lease)
                        continue
                    require(
                        frame.WhichOneof("frame") == "data"
                        and 0 < len(frame.data) <= TRANSFER_CHUNK_BYTES,
                        "invalid result chunk",
                    )
                    size += len(frame.data)
                    require(size <= header.artifact.size_bytes, "result too large")
                    output.write(frame.data)
                    digest.update(frame.data)
                require(
                    header is not None
                    and size == header.artifact.size_bytes
                    and digest.hexdigest() == header.artifact.sha256,
                    "result digest mismatch",
                )
                output.seek(0)
                self.repository.commit_fleet_result(header.identity, header.lease, output.read())
            return pb.FleetAck(accepted=True)
