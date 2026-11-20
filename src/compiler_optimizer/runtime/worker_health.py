"""Heartbeat RPC for coordinator-registered worker identities; clients cannot renew task leases."""

import sqlite3

import grpc

from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc
from compiler_optimizer.storage.errors import ConflictError, NotFoundError, StateError
from compiler_optimizer.workers.baseline import metadata


class WorkerHealthService(rpc.WorkerHealthServiceServicer):
    def __init__(self, repository):
        self.repository = repository

    def ReportHeartbeat(self, request, context):
        try:
            self.repository.report_worker_heartbeat(request)
        except ContractError:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid heartbeat/worker incarnation")
        except NotFoundError:
            context.abort(grpc.StatusCode.NOT_FOUND, "worker is not registered")
        except ConflictError:
            context.abort(grpc.StatusCode.ALREADY_EXISTS, "stale/conflicting heartbeat sequence")
        except StateError:
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION, "worker is quarantined or unassigned"
            )
        except (sqlite3.Error, OSError):
            context.abort(grpc.StatusCode.UNAVAILABLE, "worker registry unavailable")
        return pb.Receipt(
            meta=metadata(request.meta.task_id), accepted_message_id=request.meta.message_id
        )
