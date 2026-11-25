"""Typed LLVM proposals with immutable, content-addressed compiler-stage evidence."""

import hashlib
import json

from google.protobuf.json_format import MessageToDict

from compiler_optimizer.shared.manifest import require
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.storage.pipeline import reference
from compiler_optimizer.workers.baseline import metadata
from compiler_optimizer.workers.docker_runner import InfrastructureError
from compiler_optimizer.workers.instrumentation import BUILD_PROFILES
from compiler_optimizer.workers.llvm_policy import PIPELINES, TRIPLES
from compiler_optimizer.workers.package import package_from_files, unbundle


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def descriptor(ir):
    return encode(MessageToDict(ir, preserving_proto_field_name=True))


def validate_ir(ir, compiler, parent):
    require(ir.schema_version == 1, "unsupported LLVM proposal version")
    require(ir.pipeline_id in PIPELINES, "unsupported LLVM pass pipeline")
    require(compiler.target in TRIPLES, "LLVM proposals require a native CPU target")
    require(ir.target_triple == TRIPLES[compiler.target], "LLVM target triple mismatch")
    require(ir.llvm_version == compiler.version, "LLVM/Clang version mismatch")
    require(ir.baseline_source_sha256 == parent, "LLVM baseline source mismatch")
    profiles = [v.build_profile for v in ir.variants]
    require(
        2 <= len(profiles) <= 3
        and len(set(profiles)) == len(profiles)
        and "normal" in profiles
        and set(profiles) <= BUILD_PROFILES.keys(),
        "LLVM proposal needs distinct normal and sanitizer variants",
    )
    from compiler_optimizer.shared.contracts import _artifact

    for variant in ir.variants:
        for ref in (variant.baseline_bitcode, variant.optimized_bitcode, variant.stage_report):
            _artifact(ref, nonempty=True)
    normal = next(v for v in ir.variants if v.build_profile == "normal")
    require(
        normal.baseline_bitcode.sha256 != normal.optimized_bitcode.sha256,
        "LLVM pipeline made no IR change",
    )


def validate_artifacts(artifacts, proposal):
    """Recheck the entire stage chain whenever proposals enter verification or selection."""
    ir = proposal.ir
    validate_ir(ir, proposal.compiler, proposal.source.parent_source_sha256)
    require(artifacts.get(proposal.source.bundle) == descriptor(ir), "LLVM descriptor mismatch")
    refs = []
    for variant in ir.variants:
        artifacts.get(variant.baseline_bitcode)
        artifacts.get(variant.optimized_bitcode)
        refs.extend((variant.baseline_bitcode, variant.optimized_bitcode, variant.stage_report))
        report = json.loads(artifacts.get(variant.stage_report))
        require(
            report["schema_version"] == 1
            and report["compiler_image"] == proposal.compiler.image_digest
            and report["llvm_version"] == ir.llvm_version
            and report["pipeline_id"] == ir.pipeline_id
            and report["passes"] == PIPELINES[ir.pipeline_id]
            and report["baseline_source_sha256"] == ir.baseline_source_sha256
            and report["build_profile"] == variant.build_profile,
            "LLVM stage identity mismatch",
        )
        rows = report["stages"]
        stages = [row["stage"] for row in rows]
        count = stages.count("emit")
        require(
            count > 0 and stages == ["emit"] * count + ["link", "verify", "optimize", "verify"],
            "incomplete LLVM compiler stage chain",
        )
        outputs = []
        for index, row in enumerate(rows):
            ref = pb.ArtifactRef(**row["output"])
            artifacts.get(ref)
            refs.append(ref)
            expected = (
                [] if index < count else outputs[:count] if index == count else [outputs[index - 1]]
            )
            # The first verifier's output is deliberately not used as a mutable baseline.
            if index == count + 2:
                expected = [variant.baseline_bitcode.sha256]
            require(row["inputs"] == expected, "LLVM stage input chain mismatch")
            log = row["invocation"]
            require(
                log["failure"] is None
                and log["returncode"] == 0
                and log["build_profile"] == variant.build_profile
                and log["sandbox"]["policy_version"] == 1,
                "LLVM compiler stage did not succeed in pinned sandbox",
            )
            outputs.append(ref.sha256)
        require(
            outputs[count] == variant.baseline_bitcode.sha256
            and outputs[count + 2] == variant.optimized_bitcode.sha256,
            "LLVM bitcode identity mismatch",
        )
    return refs


class IRPassBackend:
    """Operator selects a recipe ID; no model-supplied compiler arguments are accepted."""

    def __init__(self, pipeline_id):
        require(pipeline_id in PIPELINES, "unsupported LLVM pass pipeline")
        self.pipeline_id = pipeline_id


class IRPassAgent:
    def __init__(self, repository, runner, pipeline_id, sanitizer):
        self.repository, self.runner = repository, runner
        self.pipeline_id, self.sanitizer = pipeline_id, sanitizer

    def generate(self, request):
        repo, runner = self.repository, self.runner
        submission = repo.get_task(request.meta.task_id)["submission"]
        package = package_from_files(unbundle(repo.artifacts.get(submission.package)))
        require(
            request.source == submission.source,
            "LLVM proposals must derive from immutable baseline",
        )
        require(
            runner.compiler(package.manifest.language) == request.compiler,
            "LLVM generation requires the assigned pinned compiler",
        )
        require(
            getattr(runner, "native_execution", False), "LLVM generation requires native execution"
        )
        ir = pb.IRPassProposal(
            schema_version=1,
            pipeline_id=self.pipeline_id,
            baseline_source_sha256=request.source.bundle.sha256,
            llvm_version=request.compiler.version,
            target_triple=TRIPLES.get(request.compiler.target, ""),
        )
        require(bool(ir.target_triple), "unsupported LLVM CPU target")
        for profile in ("normal", self.sanitizer):
            rows = []

            def stage(name, modules=(), source=None, *, profile=profile, rows=rows):
                # Cache successful stages by all immutable inputs, independent of proposal ID.
                key = hashlib.sha256(
                    encode(
                        {
                            "stage": name,
                            "modules": [hashlib.sha256(x).hexdigest() for x in modules],
                            "source": source,
                            "parent": request.source.bundle.sha256,
                            "image": request.compiler.image_digest,
                            "profile": profile,
                            "pipeline": PIPELINES[self.pipeline_id] if name == "optimize" else None,
                        }
                    )
                ).hexdigest()
                cached = [
                    a
                    for a in repo.list_artifacts(request.meta.task_id)
                    if a["kind"] == "llvm-stage" and a["owner_id"] == key
                ]
                if cached:
                    record = json.loads(
                        repo.artifacts.get(
                            pb.ArtifactRef(
                                **{k: cached[0][k] for k in ("key", "sha256", "size_bytes")}
                            )
                        )
                    )
                else:
                    invocation = runner.llvm_stage(
                        package,
                        name,
                        pipeline_id=self.pipeline_id,
                        modules=modules,
                        source=source,
                        build_profile=profile,
                    )
                    if invocation.failure or not invocation.binary:
                        failure = repo.artifacts.put(
                            encode(
                                {
                                    "stage": name,
                                    "compiler_image": request.compiler.image_digest,
                                    "profile": profile,
                                    "pipeline_id": self.pipeline_id,
                                    "invocation": invocation.log(),
                                }
                            )
                        )
                        with repo._transaction() as db:
                            repo._task(db, request.meta.task_id, running=True)
                            repo._artifact(
                                db,
                                request.meta.task_id,
                                failure,
                                "llvm-failure",
                                owner=request.proposal_id,
                            )
                        raise InfrastructureError(
                            "LLVM " + name + " failed: " + str(invocation.failure)
                        )
                    output = repo.artifacts.put(invocation.binary)
                    record = {
                        "stage": name,
                        "inputs": [hashlib.sha256(x).hexdigest() for x in modules],
                        "output": reference(output),
                        "invocation": invocation.log(),
                    }
                    ref = repo.artifacts.put(encode(record))
                    with repo._transaction() as db:
                        repo._task(db, request.meta.task_id, running=True)
                        repo._artifact(
                            db,
                            request.meta.task_id,
                            output,
                            "llvm-bitcode",
                            owner=request.proposal_id,
                        )
                        repo._artifact(db, request.meta.task_id, ref, "llvm-stage", owner=key)
                rows.append(record)
                return repo.artifacts.get(pb.ArtifactRef(**record["output"]))

            modules = [stage("emit", source=name) for name in package.manifest.sources]
            baseline = stage("link", modules)
            stage("verify", [baseline])
            optimized = stage("optimize", [baseline])
            stage("verify", [optimized])
            report = repo.artifacts.put(
                encode(
                    {
                        "schema_version": 1,
                        "compiler_image": request.compiler.image_digest,
                        "llvm_version": ir.llvm_version,
                        "pipeline_id": ir.pipeline_id,
                        "passes": PIPELINES[ir.pipeline_id],
                        "baseline_source_sha256": ir.baseline_source_sha256,
                        "build_profile": profile,
                        "stages": rows,
                    }
                )
            )
            ir.variants.add(
                build_profile=profile,
                baseline_bitcode=repo.artifacts.put(baseline),
                optimized_bitcode=repo.artifacts.put(optimized),
                stage_report=report,
            )
        proposal = pb.Proposal(
            meta=metadata(request.meta.task_id),
            proposal_id=request.proposal_id,
            role=request.role,
            compiler=request.compiler,
            ir=ir,
            source=pb.SourceVersion(
                version=request.source.version + 1,
                bundle=repo.artifacts.put(descriptor(ir)),
                parent_source_sha256=request.source.bundle.sha256,
            ),
            rationale="Apply trusted LLVM pipeline "
            + ir.pipeline_id
            + "; native equivalence and measurements remain required.",
            transformation="llvm-" + ir.pipeline_id,
        )
        proposal.meta.created_at_unix_ms = max(
            proposal.meta.created_at_unix_ms, request.meta.created_at_unix_ms
        )
        validate_artifacts(repo.artifacts, proposal)
        return proposal, proposal.source.bundle
