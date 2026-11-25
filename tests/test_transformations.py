"""Rule applicability/proofs, source contracts and native target bindings, without model calls."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from test_agent_rpc import agents as agents

from compiler_optimizer.agents.rules import (
    ArchitectureRuleBackend,
    MemoryRuleBackend,
    PerformanceRuleBackend,
)
from compiler_optimizer.shared.c_syntax import TransformationAbstained
from compiler_optimizer.shared.manifest import ContractError
from compiler_optimizer.shared.patches import apply_patch
from compiler_optimizer.shared.v1 import contracts_pb2 as pb
from compiler_optimizer.workers.baseline import metadata
from compiler_optimizer.workers.package import bundle, read_package
from compiler_optimizer.workers.targets import compiler_flags

ROOT = Path(__file__).parents[1] / "examples"
FIXTURES = ROOT / "cpu-transformations"
CASES = [
    ("bounded-echo", PerformanceRuleBackend),
    ("buffered-total", PerformanceRuleBackend),
    ("buffered-total", MemoryRuleBackend),
    ("buffered-total-cpp", MemoryRuleBackend),
    ("unsigned-scale", PerformanceRuleBackend),
    ("unsigned-scale", ArchitectureRuleBackend),
    ("unsigned-xor", PerformanceRuleBackend),
    ("unsigned-xor", ArchitectureRuleBackend),
]


def assignment(package, role, target="linux-aarch64"):
    return pb.OptimizationRequest(
        meta=metadata(str(uuid4())),
        proposal_id=str(uuid4()),
        role=role,
        source=pb.SourceVersion(
            version=1,
            bundle=pb.ArtifactRef(
                key="source",
                sha256=hashlib.sha256(package.source_bundle).hexdigest(),
                size_bytes=len(package.source_bundle),
            ),
        ),
        manifest=package.manifest,
        compiler=pb.CompilerSettings(
            executable="clang" if package.manifest.language == "c17" else "clang++",
            version="18",
            language=package.manifest.language,
            target=target,
            image_digest="sha256:" + "a" * 64,
            flags=compiler_flags(target, package.manifest.language),
        ),
    )


@pytest.mark.parametrize("target", ["linux-aarch64", "linux-x86_64"])
@pytest.mark.parametrize("name,backend_type", CASES)
def test_unrelated_c_cpp_programs_generate_bound_portable_candidates(target, name, backend_type):
    package, backend = read_package(FIXTURES / name), backend_type()
    request = assignment(package, backend.role, target)
    before = request.compiler.SerializeToString()
    data = backend.generate(request, package.sources)
    patch, after = apply_patch(
        data, package.sources, request.source.bundle.sha256, compiler=request.compiler
    )
    assert patch["schema_version"] == 2 and patch["contract"]["mode"] == "portable"
    assert patch["contract"]["target"] == target and after != package.sources
    assert before == request.compiler.SerializeToString()
    assert set(after) == set(package.sources)


def test_rules_use_tokens_and_bounds_rather_than_filenames_hashes_or_spacing():
    package = read_package(ROOT / "sum-array")
    source = (
        package.sources["main.c"].decode().replace("count", "items").replace("value", "reading")
    )
    source = source.replace("100000", "200000").replace("sum", "answer")
    source = source.replace("sum", "answer").replace("int main(void)", "int main ( void )")
    source = source.replace("answer += reading;", "answer /* += fake; */ += reading ;")
    sources = {"renamed.c": source.encode()}
    request = assignment(package, pb.ARCHITECTURE)
    request.source.bundle.sha256 = hashlib.sha256(bundle(sources)).hexdigest()
    data = ArchitectureRuleBackend().generate(request, sources)
    _, changed = apply_patch(data, sources, request.source.bundle.sha256, compiler=request.compiler)
    assert b"answer + alternate" in changed["renamed.c"]
    assert b"items" in changed["renamed.c"]


@pytest.mark.parametrize(
    "change",
    [
        lambda text: "#define output input\n" + text,
        lambda text: text.replace("*input", "*restrict input"),
        lambda text: text.replace("*input", "*volatile input"),
        lambda text: text.replace("unsigned int", "int"),
        lambda text: text.replace("* 3U", "* 4294967295"),
        lambda text: text.replace("input[index]", "input[index + 1]"),
        lambda text: text.replace("length;", "length - 1;"),
        lambda text: "#pragma clang loop vectorize(enable)\n" + text,
    ],
)
def test_unsupported_aliasing_types_bounds_macros_and_recurrences_abstain(change):
    package = read_package(FIXTURES / "unsigned-scale")
    sources = {"main.c": change(package.sources["main.c"].decode()).encode()}
    with pytest.raises(TransformationAbstained, match="abstained"):
        ArchitectureRuleBackend().generate(assignment(package, pb.ARCHITECTURE), sources)


def test_streaming_retains_allocation_failure_and_checks_integer_bounds():
    package = read_package(FIXTURES / "buffered-total-cpp")
    request = assignment(package, pb.MEMORY)
    data = MemoryRuleBackend().generate(request, package.sources)
    _, result = apply_patch(
        data, package.sources, request.source.bundle.sha256, compiler=request.compiler
    )
    new = result["main.cpp"].decode()
    assert "(long long *)malloc" in new and "samples == NULL" in new
    assert new.index("free(samples)") < new.index("long long total")
    assert "samples[i]" not in new and "optimizer_value" in new
    original = package.sources["main.cpp"].replace(b"< -4096", b"< -9223372036854775807")
    original = original.replace(b"samples[i] > 4096", b"samples[i] > 9223372036854775807")
    with pytest.raises(TransformationAbstained, match="representable"):
        MemoryRuleBackend().generate(request, {"main.cpp": original})


def test_guarded_candidate_preserves_overlapping_array_fallback_and_scalar_tail():
    package = read_package(FIXTURES / "unsigned-scale")
    request = assignment(package, pb.ARCHITECTURE)
    _, result = apply_patch(
        ArchitectureRuleBackend().generate(request, package.sources),
        package.sources,
        request.source.bundle.sha256,
        compiler=request.compiler,
    )
    source = result["main.c"].decode()
    assert "if (input == output)" in source and "length - index >= 4U" in source
    assert "for (; index < length; ++index)" in source
    assert "else {" in source and "for (unsigned int index = 0; index < length; ++index)" in source
    assert "restrict" not in source and "#pragma" not in source


def patch_for(request, old, new, *, contract=None):
    return json.dumps(
        {
            "schema_version": 1 if contract is None else 2,
            **({"contract": contract} if contract else {}),
            "parent_source_sha256": request.source.bundle.sha256,
            "transformation": "operator-patch",
            "rationale": "Independently validate.",
            "edits": [{"path": "main.c", "old": old, "new": new}],
        }
    ).encode()


def contract(target="linux-aarch64", mode="portable", features=None):
    return {
        "schema_version": 1,
        "language": "c17",
        "target": target,
        "mode": mode,
        "required_features": features or [],
    }


def test_versioned_contract_rejects_wrong_targets_and_undeclared_isa():
    package = read_package(FIXTURES / "bounded-echo")
    request = assignment(package, pb.PERFORMANCE)
    old = package.sources["main.c"].decode()
    new = "#include <arm_neon.h>\n" + old
    with pytest.raises(ContractError, match="versioned explicit"):
        apply_patch(
            patch_for(request, old, new),
            package.sources,
            request.source.bundle.sha256,
            compiler=request.compiler,
        )
    data = patch_for(
        request, old, new, contract=contract(mode="baseline-intrinsics", features=["neon"])
    )
    apply_patch(data, package.sources, request.source.bundle.sha256, compiler=request.compiler)
    wrong = pb.CompilerSettings()
    wrong.CopyFrom(request.compiler)
    wrong.target = "linux-x86_64"
    with pytest.raises(ContractError, match="assigned compiler"):
        apply_patch(data, package.sources, request.source.bundle.sha256, compiler=wrong)
    with pytest.raises(ContractError, match="features exceed"):
        apply_patch(
            patch_for(
                request,
                old,
                new,
                contract=contract("linux-x86_64", "baseline-intrinsics", ["neon"]),
            ),
            package.sources,
            request.source.bundle.sha256,
            compiler=request.compiler,
        )


@pytest.mark.parametrize(
    "addition",
    [
        "#pragma clang loop vectorize(enable)\n",
        "#define restrict __restrict\n",
        "void f(int *restrict p);\n",
        '__attribute__((target("avx2"))) int f(void);\n',
    ],
)
def test_flags_pragmas_and_alias_assumptions_cannot_hide_in_source(addition):
    package = read_package(FIXTURES / "bounded-echo")
    request = assignment(package, pb.PERFORMANCE)
    old = package.sources["main.c"].decode()
    with pytest.raises(ContractError, match="assumptions"):
        apply_patch(
            patch_for(request, old, addition + old),
            package.sources,
            request.source.bundle.sha256,
            compiler=request.compiler,
        )


def test_comments_and_strings_do_not_trigger_source_contract_markers():
    package = read_package(FIXTURES / "bounded-echo")
    request = assignment(package, pb.PERFORMANCE)
    old = package.sources["main.c"].decode()
    new = "/* #pragma __restrict__ _mm256_loadu_si256 */\n" + old
    apply_patch(
        patch_for(request, old, new),
        package.sources,
        request.source.bundle.sha256,
        compiler=request.compiler,
    )


def test_unsupported_target_or_changed_flags_abstain():
    package = read_package(FIXTURES / "unsigned-scale")
    request = assignment(package, pb.ARCHITECTURE)
    request.compiler.target = "unsupported"
    with pytest.raises(TransformationAbstained, match="CPU target"):
        ArchitectureRuleBackend().generate(request, package.sources)
    request = assignment(package, pb.ARCHITECTURE)
    request.compiler.flags.append("-ffast-math")
    with pytest.raises(TransformationAbstained, match="fixed compiler"):
        ArchitectureRuleBackend().generate(request, package.sources)


@pytest.mark.parametrize(
    "name,backend_type",
    [
        ("bounded-echo", PerformanceRuleBackend),
        ("buffered-total", MemoryRuleBackend),
        ("unsigned-scale", ArchitectureRuleBackend),
    ],
)
def test_portable_riscv_candidates_remain_available_for_correctness_only(name, backend_type):
    from compiler_optimizer.workers.targets import target_profile

    package = read_package(FIXTURES / name)
    backend = backend_type()
    request = assignment(package, backend.role, "linux-riscv64")
    before = request.compiler.SerializeToString()
    data = backend.generate(request, package.sources)
    patch, changed = apply_patch(
        data, package.sources, request.source.bundle.sha256, compiler=request.compiler
    )
    assert patch["contract"] == {
        "schema_version": 1,
        "mode": "portable",
        "target": "linux-riscv64",
        "language": "c17",
        "required_features": [],
    }
    assert changed != package.sources and request.compiler.SerializeToString() == before
    assert not target_profile(request.compiler.target).performance_qualification


def test_model_contract_survives_generation_and_is_bound_to_the_assigned_profile():
    from test_model_adapter import Scripted, client

    from compiler_optimizer.agents.model_backends import ModelPatchBackend

    package = read_package(FIXTURES / "bounded-echo")
    request = assignment(package, pb.PERFORMANCE)
    old = package.sources["main.c"].decode()
    value = json.loads(patch_for(request, old, "/* portable */\n" + old, contract=contract()))
    data = ModelPatchBackend(client(Scripted(value)), pb.PERFORMANCE).generate(
        request, package.sources
    )
    assert json.loads(data)["contract"] == contract()
    value["contract"]["target"] = "linux-x86_64"
    with pytest.raises(ContractError, match="assigned compiler"):
        ModelPatchBackend(client(Scripted(value), max_repairs=0), pb.PERFORMANCE).generate(
            request, package.sources
        )


def test_multiline_comments_cannot_hide_pragmas_and_quoted_headers_need_contracts():
    package = read_package(FIXTURES / "bounded-echo")
    request = assignment(package, pb.PERFORMANCE)
    old = package.sources["main.c"].decode()
    new = 'int helper; /*\n ignored */ #pragma GCC target("avx2")\n' + old
    with pytest.raises(ContractError, match="assumptions"):
        apply_patch(
            patch_for(request, old, new),
            package.sources,
            request.source.bundle.sha256,
            compiler=request.compiler,
        )
    with pytest.raises(ContractError, match="versioned explicit"):
        apply_patch(
            patch_for(request, old, '#include "arm_neon.h"\n' + old),
            package.sources,
            request.source.bundle.sha256,
            compiler=request.compiler,
        )


def test_rule_abstention_survives_rpc_and_preserves_the_original(tmp_path, agents):  # noqa: F811
    from test_agents import make_workflow
    from test_selection import CohortBroker

    from compiler_optimizer.agents.rpc_client import AgentClient
    from compiler_optimizer.coordinator.round import CohortCoordinator

    repo, runner, task, _ = make_workflow(tmp_path, "sum-array")
    _, settings, _ = agents
    coordinator = CohortCoordinator(
        repo, runner, CohortBroker(repo), agent_client=AgentClient(repo, settings)
    )
    report = coordinator.run(task, agents=["memory"])
    assert report["status"] == "retained_baseline"
    assert not repo.list_proposals(task) and not repo.list_benchmarks(task)
    progress = repo.selection_progress(task)[0]
    with repo._transaction(write=False) as db:
        evidence = json.loads(repo.artifacts.get(repo._ref(db, progress["evidence_key"])))
    assert evidence["reason"] == "generation_abstained"
