"""Durable coordinator intake, automatic execution scheduling and result RPCs."""

import json
import logging
import sqlite3
import time
from uuid import UUID, uuid4

import grpc

from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.shared.v1 import contracts_pb2_grpc as rpc
from compiler_optimizer.shared.workflow import default_workflow, validate_workflow
from compiler_optimizer.storage.errors import (
    ArtifactError,
    ConflictError,
    NotFoundError,
    StateError,
)


class CoordinatorService(rpc.CoordinatorServiceServicer):
    def __init__(self, repository, *, execution_enabled=True, workflow_configuration=None):
        self.repository = repository
        self.workflow_configuration = None
        if execution_enabled:
            config = (
                default_workflow() if workflow_configuration is None else workflow_configuration
            )
            self.workflow_configuration = json.loads(validate_workflow(config))

    def _accept(self, request, context, save):
        try:
            save(request)
        except ContractError:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid v1 message")
        except ConflictError:
            context.abort(grpc.StatusCode.ALREADY_EXISTS, "message or entity identity conflict")
        except NotFoundError:
            context.abort(
                grpc.StatusCode.NOT_FOUND, "required task, assignment, or artifact is missing"
            )
        except (ArtifactError, StateError):
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION, "artifact integrity or task state check failed"
            )
        except (sqlite3.Error, OSError):
            context.abort(grpc.StatusCode.UNAVAILABLE, "durable storage is unavailable")
        except Exception as error:
            logging.error("intake failed: %s", type(error).__name__)
            context.abort(grpc.StatusCode.INTERNAL, "intake failed")
        return pb.Receipt(
            meta=pb.Metadata(
                schema_version=1,
                message_id=str(uuid4()),
                task_id=request.meta.task_id,
                created_at_unix_ms=time.time_ns() // 1_000_000,
            ),
            accepted_message_id=request.meta.message_id,
        )

    def SubmitSubmission(self, request, context):
        return self._accept(
            request,
            context,
            lambda message: self.repository.save_submission(
                message, execution=self.workflow_configuration
            ),
        )

    def SubmitProposal(self, request, context):
        return self._accept(request, context, self.repository.save_proposal)

    def GetOptimizationResult(self, request, context):
        try:
            parsed = UUID(request.task_id)
            if str(parsed) != request.task_id or parsed.int == 0:
                raise ValueError("invalid task ID")
        except ValueError:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "task_id must be a canonical UUID")
        try:
            task = self.repository.get_task(request.task_id)
            execution = self.repository.get_execution(request.task_id)
            pipeline = self.repository.get_pipeline(request.task_id)
            cached = self.repository.pipeline_report(request.task_id)
            selection = self.repository.get_selection_session(request.task_id)
            if cached is None:
                cached = self.repository.selection_report(request.task_id)
            compile_report = self.repository.cuda_task_report(request.task_id)
            if compile_report is not None:
                cached = compile_report
            result = pb.OptimizationResult(
                task_id=request.task_id,
                status=task["status"],
                stage="baseline" if pipeline is None else pipeline["phase"],
            )
            if selection is not None and pipeline is None:
                result.stage = (
                    "completed" if selection["outcome_key"] is not None else "collecting_candidates"
                )
            if execution is not None:
                result.stage = execution["stage"]
            if cached is not None:
                result.report.CopyFrom(cached[0])
                result.report_json = json.dumps(cached[1], sort_keys=True)
            decision = self.repository.get_decision(request.task_id)
            if decision is not None:
                selected = next(
                    row
                    for row in self.repository.list_results(request.task_id)
                    if row.job_id == decision["job_id"] and row.attempt == decision["attempt"]
                )
                self.repository.artifacts.get(selected.source.bundle)
                result.selected_source.CopyFrom(selected.source)
            if not result.report_json:
                summary = {
                    "task_id": request.task_id,
                    "status": task["status"],
                    "stage": result.stage,
                }
                if execution is not None:
                    summary["execution"] = {
                        key: execution[key]
                        for key in ("attempts", "available_at_ms", "error_code", "finished")
                    }
                if pipeline is not None:
                    summary["proposal_id"] = pipeline["proposal_id"]
                else:
                    failures = [
                        row
                        for row in self.repository.list_results(request.task_id)
                        if row.WhichOneof("subject") == "baseline"
                        and row.WhichOneof("outcome") == "failure"
                    ]
                    if failures:
                        summary.update(
                            reason="baseline_rejected",
                            failure=pb.FailureReason.Name(failures[-1].failure.reason),
                        )
                result.report_json = json.dumps(summary)
            return result
        except NotFoundError:
            context.abort(grpc.StatusCode.NOT_FOUND, "task or artifact not found")
        except (ArtifactError, StateError, ContractError):
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, "result integrity check failed")
        except (sqlite3.Error, OSError):
            context.abort(grpc.StatusCode.UNAVAILABLE, "durable storage is unavailable")
