"""Coordinator-side DockerRunner facade. All sandbox work is executed by fleet workers."""

import json
import time
from contextlib import contextmanager

from google.protobuf.json_format import MessageToDict, ParseDict

from compiler_optimizer.shared.fleet import decode_result, validate_profile
from compiler_optimizer.shared.lease_context import EXECUTION_LEASE, MEASUREMENT_WINDOW
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.workers.docker_runner import InfrastructureError
from compiler_optimizer.workers.package import bundle


def matching_workers(repository, compiler=None, target=""):
    rows = []
    for row in repository.fleet_profiles():
        profile = validate_profile(row["profile_json"])
        if target and profile["target"] != target:
            continue
        if compiler is not None:
            offered = ParseDict(profile["compilers"][compiler.language], pb.CompilerSettings())
            if offered != compiler:
                continue
        rows.append(row)
    return sorted(
        rows, key=lambda r: (not json.loads(r["profile_json"])["native_execution"], r["worker_id"])
    )


def fleet_compiler(repository, target, language):
    workers = matching_workers(repository, target=target)
    if not workers:
        raise InfrastructureError("no healthy registered worker for this target")
    return ParseDict(
        json.loads(workers[0]["profile_json"])["compilers"][language], pb.CompilerSettings()
    )


class RemoteRunner:
    def __init__(self, repository, task_id, worker):
        self.repository, self.task_id, self.worker = repository, task_id, worker
        self.profile = validate_profile(worker["profile_json"])
        self.worker_id = worker["worker_id"]
        for name in (
            "target",
            "image",
            "hardware",
            "platform",
            "native_execution",
            "native_amd64",
            "supports_sanitizers",
            "measurement_version",
        ):
            setattr(self, name, self.profile[name])

    def compiler(self, language):
        return ParseDict(self.profile["compilers"][language], pb.CompilerSettings())

    def check_health(self):
        self._available()
        environment = self.benchmark_environment()
        expected = self.profile["context"]["hardware"]
        if any(environment.get(key) != value for key, value in expected.items()):
            raise InfrastructureError("remote worker execution context changed")

    def _available(self):
        if not any(
            row["worker_id"] == self.worker_id
            and row["incarnation_id"] == self.worker["incarnation_id"]
            and row["profile_json"] == self.worker["profile_json"]
            for row in self.repository.fleet_profiles()
        ):
            raise InfrastructureError("remote worker is unavailable")

    def recovery_context(self):
        return self.profile["context"]

    def cancel_recovery_attempt(self, token):
        return self.repository.cancel_fleet_parent(token)

    def _call(self, payload, refs=()):
        self._available()
        payload["compiler"] = MessageToDict(
            self.compiler(payload.pop("language", "c17")), preserving_proto_field_name=True
        )
        payload["window_token"] = MEASUREMENT_WINDOW.get()
        parent = EXECUTION_LEASE.get()
        job = self.repository.enqueue_fleet_job(
            self.task_id,
            self.worker,
            payload,
            refs,
            parent_token=None if parent is None else parent["lease_token"],
        )
        deadline = time.monotonic() + payload["timeout_seconds"] + 10
        while time.monotonic() < deadline:
            row = self.repository.fleet_job(job)
            if row["state"] == "completed":
                with self.repository._transaction(write=False) as db:
                    ref = self.repository._ref(db, row["result_key"])
                data = self.repository.artifacts.get(ref)
                result = decode_result(data, payload["operation"])
                if isinstance(result, dict) and "error" in result:
                    raise InfrastructureError("remote sandbox infrastructure failure")
                return result["environment"] if payload["operation"] == "environment" else result
            if row["state"] == "failed":
                raise InfrastructureError("remote job failed: " + str(row["error"]))
            time.sleep(0.05)
        self.repository.cancel_fleet_job(job)
        raise InfrastructureError("remote work or sandbox cleanup did not complete before deadline")

    @contextmanager
    def measurement_window(self):
        parent = EXECUTION_LEASE.get()
        token = self.repository.request_measurement_window(
            self.task_id,
            self.worker["engine_id"],
            None if parent is None else parent["lease_token"],
        )
        context_token = None
        try:
            deadline = time.monotonic() + 60
            while not self.repository.enter_measurement_window(token):
                if parent is not None:
                    parent["assert_live"]()
                if time.monotonic() >= deadline:
                    raise InfrastructureError("remote measurement admission deadline")
                time.sleep(0.05)
            context_token = MEASUREMENT_WINDOW.set(token)
            yield
        finally:
            if context_token is not None:
                MEASUREMENT_WINDOW.reset(context_token)
            self.repository.release_measurement_window(token)

    def compile(self, package, *, build_profile="normal"):
        ref = self.repository.artifacts.put(bundle(package.files))
        return self._call(
            {
                "operation": "compile",
                "package": MessageToDict(ref, preserving_proto_field_name=True),
                "language": package.manifest.language,
                "build_profile": build_profile,
                "timeout_seconds": package.manifest.limits.compile_wall_seconds + 60,
                "reservation_mib": package.manifest.limits.compile_memory_mib,
                "output_bytes": package.manifest.limits.output_kib * 1024,
            },
            [ref],
        )

    def execute(self, binary, case, package, *, build_profile="normal", cpu_id=None):
        bref = self.repository.artifacts.put(binary)
        iref = self.repository.artifacts.put(package.files[case.stdin])
        return self._call(
            {
                "operation": "execute",
                "binary": MessageToDict(bref, preserving_proto_field_name=True),
                "stdin": MessageToDict(iref, preserving_proto_field_name=True),
                "manifest": MessageToDict(
                    package.manifest,
                    preserving_proto_field_name=True,
                    always_print_fields_with_no_presence=True,
                ),
                "case": MessageToDict(case, preserving_proto_field_name=True),
                "language": package.manifest.language,
                "build_profile": build_profile,
                "cpu_id": cpu_id,
                "timeout_seconds": package.manifest.limits.run_wall_seconds + 60,
                "reservation_mib": package.manifest.limits.run_memory_mib,
                "output_bytes": package.manifest.limits.output_kib * 1024,
            },
            [bref, iref],
        )

    def benchmark_environment(self, cpu_id=None):
        return self._call({"operation": "environment", "cpu_id": cpu_id, "timeout_seconds": 65})
