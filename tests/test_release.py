"""Release manifests, promotion gates and schema-safe rollback (action S)."""

import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"tools/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load("release")
infra = load("infra")

DIGEST = "sha256:" + "a" * 64
REGISTRY = "123456789012.dkr.ecr.us-west-2.amazonaws.com/cmpe273-optimizer"
REVISION = "0123456789abcdef0123456789abcdef01234567"


def ref(image, letter="a"):
    return f"{REGISTRY}/{image}@sha256:{letter * 64}"


def records(arm_mode="native", arm_validation="passed"):
    result = []
    for image in release.IMAGES:
        for platform, info in release.PLATFORMS.items():
            arm = info["arch"] == "arm64"
            result.append(
                release.platform_record(
                    image=image,
                    platform=platform,
                    reference=ref(image, "b" if arm else "c"),
                    tag=f"v0.1.0-{info['arch']}",
                    build_mode=arm_mode if arm else "native",
                    native_validation=arm_validation if arm else "passed",
                )
            )
    return result


def manifest(version=None, schema=None, scanned=True, **kwargs):
    value = release.assemble_manifest(
        version=version or release.project_version(),
        revision=REVISION,
        records=records(**kwargs),
        indexes={image: ref(image) for image in release.IMAGES},
    )
    if schema is not None:
        value["database_schema_version"] = schema
    if scanned:
        for name, platform, _ in release.all_platform_references(value):
            value["scans"][f"{name} {platform}"] = {"counts": {}, "blocking": [], "passed": True}
    return value


class NotFound(Exception):
    pass


class FakeSSM:
    class exceptions:
        ParameterNotFound = NotFound

    def __init__(self):
        self.versions = []

    def put_parameter(self, **request):
        assert request["Overwrite"] and request["Type"] == "String"
        assert len(request["Value"]) <= 4096
        self.versions.append(request["Value"])

    def get_paginator(self, name):
        assert name == "get_parameter_history"
        outer = self

        class Paginator:
            def paginate(self, Name):
                if not outer.versions:
                    raise NotFound(Name)
                entries = [{"Version": i + 1, "Value": v} for i, v in enumerate(outer.versions)]
                # Real history arrives in pages; return them out of order to test sorting.
                yield {"Parameters": entries[len(entries) // 2 :]}
                yield {"Parameters": entries[: len(entries) // 2]}

        return Paginator()


class FakeECR:
    class exceptions:
        ImageNotFoundException = NotFound
        ScanNotFoundException = type("ScanNotFound", (Exception,), {})

    def __init__(self, missing=(), scans=None):
        self.missing = set(missing)
        self.scans = list(scans or [])

    def describe_images(self, repositoryName, imageIds):
        if imageIds[0]["imageDigest"] in self.missing:
            raise NotFound(repositoryName)
        return {"imageDetails": [{}]}

    def describe_image_scan_findings(self, **request):
        return self.scans.pop(0)


STAGING = {"worker_architecture": "arm64"}


# --- Source facts ------------------------------------------------------------------


def test_release_tag_must_match_the_project_version():
    version = release.project_version()
    assert release.release_version(f"refs/tags/v{version}") == version
    with pytest.raises(release.ReleaseError, match="does not match"):
        release.release_version("v99.0.0")
    for bad in ("1.0.0", "v1.0", "v1.0.0-rc1", "refs/heads/main"):
        with pytest.raises(release.ReleaseError, match="not a release tag"):
            release.release_version(bad)


def test_schema_version_is_read_from_both_backends_without_importing():
    from compiler_optimizer.storage import postgres

    assert release.schema_version() == postgres.SCHEMA_VERSION


def test_contract_and_base_image_provenance():
    assert re.fullmatch(r"[0-9a-f]{64}", release.contracts_sha256())
    images = release.base_images()
    assert images and all("@sha256:" in image for image in images)


@pytest.fixture
def repository(tmp_path):
    storage = tmp_path / "src/compiler_optimizer/storage"
    storage.mkdir(parents=True)
    for name in ("postgres.py", "repository.py"):
        (storage / name).write_text((release.STORAGE / name).read_text())
    (storage / "schema.sql").write_text("CREATE TABLE a (id INTEGER);\n")

    def run(*arguments):
        subprocess.run(["git", *arguments], cwd=tmp_path, check=True, capture_output=True)

    run("init", "-q")
    run("-c", "user.email=t@example.com", "-c", "user.name=t", "add", ".")
    run("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "one")
    for tag in ("v0.1.0", "v0.9.0", "v0.10.0"):
        run("tag", tag)
    return tmp_path, storage


def test_previous_release_is_the_highest_lower_version(repository):
    root, _ = repository
    assert release.previous_release_tag("0.11.0", root) == "v0.10.0"
    assert release.previous_release_tag("0.10.0", root) == "v0.9.0"
    assert release.previous_release_tag("0.1.0", root) is None


def test_released_migrations_are_append_only(repository):
    root, storage = repository
    assert release.migration_problems(None, root) == []
    (storage / "later.sql").write_text("ALTER TABLE a ADD COLUMN b INTEGER;\n")
    assert release.migration_problems("v0.10.0", root) == []
    (storage / "schema.sql").write_text("CREATE TABLE a (id BIGINT);\n")
    assert "changed since v0.10.0" in release.migration_problems("v0.10.0", root)[0]
    (storage / "schema.sql").unlink()
    assert "removed" in release.migration_problems("v0.10.0", root)[0]


def test_schema_version_may_not_decrease(repository):
    root, storage = repository
    text = (storage / "repository.py").read_text()
    current = release.schema_version(storage)
    (storage / "repository.py").write_text(
        text.replace(f"if version != {current}:", f"if version != {current - 1}:")
    )
    postgres = (storage / "postgres.py").read_text()
    (storage / "postgres.py").write_text(
        re.sub(r"MIGRATIONS = \{.*\}", f"MIGRATIONS = {{{current - 1}: 'x.sql'}}", postgres)
    )
    assert any("went down" in p for p in release.migration_problems("v0.10.0", root))


# --- Records and manifest ------------------------------------------------------------


def test_emulated_builds_cannot_claim_native_validation():
    with pytest.raises(release.ReleaseError, match="emulated"):
        release.platform_record(
            image="app",
            platform="linux/arm64",
            reference=ref("app"),
            tag="t",
            build_mode="emulated",
            native_validation="passed",
        )


def test_records_require_digest_references():
    with pytest.raises(release.ReleaseError, match="digest reference"):
        release.platform_record(
            image="app",
            platform="linux/amd64",
            reference=f"{REGISTRY}/app:v0.1.0",
            tag="t",
            build_mode="native",
            native_validation="passed",
        )


def test_manifest_records_traceable_provenance():
    value = manifest(scanned=False)
    assert value["source_revision"] == REVISION
    assert value["tag"] == "v" + value["version"]
    assert value["database_schema_version"] == release.schema_version()
    assert set(value["images"]) == set(release.IMAGES)
    for image in value["images"].values():
        assert set(image["platforms"]) == set(release.PLATFORMS)
        assert "@sha256:" in image["index"]
    assert re.fullmatch(r"[0-9a-f]{64}", value["constraints_sha256"])


def test_manifest_needs_every_platform_and_index():
    with pytest.raises(release.ReleaseError, match="exactly one build"):
        release.assemble_manifest(
            version="0.1.0",
            revision=REVISION,
            records=records()[:3],
            indexes={image: ref(image) for image in release.IMAGES},
        )
    with pytest.raises(release.ReleaseError, match="index"):
        release.assemble_manifest(
            version="0.1.0", revision=REVISION, records=records(), indexes={"app": ref("app")}
        )


# --- Scanning ----------------------------------------------------------------------


def test_critical_findings_block_unless_accepted(tmp_path):
    findings = [{"name": "CVE-1", "severity": "CRITICAL"}, {"name": "CVE-2", "severity": "HIGH"}]
    assert release.scan_summary(findings, set())["blocking"] == ["CVE-1"]
    summary = release.scan_summary(findings, {"CVE-1"})
    assert summary["passed"] and summary["counts"] == {"CRITICAL": 1, "HIGH": 1}


def test_allowlist_entries_need_reasons_and_expire(tmp_path):
    path = tmp_path / "allow.json"
    path.write_text(
        json.dumps(
            {
                "findings": [
                    {"id": "CVE-1", "reason": "not reachable", "expires": "2026-12-01"},
                    {"id": "CVE-2", "reason": "old", "expires": "2026-01-01"},
                ]
            }
        )
    )
    import datetime

    assert release.load_allowlist(path, today=datetime.date(2025, 12, 1)) == {"CVE-1", "CVE-2"}
    assert release.load_allowlist(path, today=datetime.date(2026, 11, 1)) == {"CVE-1"}
    path.write_text(json.dumps({"findings": [{"id": "CVE-3"}]}))
    with pytest.raises(release.ReleaseError, match="reason and expiry"):
        release.load_allowlist(path)
    assert release.load_allowlist(ROOT / "infra/aws/release/scan-allowlist.json") == set()


def test_scan_waits_for_completion_and_reads_every_page():
    ecr = FakeECR(
        scans=[
            {"imageScanStatus": {"status": "IN_PROGRESS"}},
            {
                "imageScanStatus": {"status": "COMPLETE"},
                "imageScanFindings": {"findings": [{"name": "CVE-1", "severity": "LOW"}]},
                "nextToken": "next",
            },
            {"imageScanFindings": {"findings": [{"name": "CVE-2", "severity": "CRITICAL"}]}},
        ]
    )
    sleeps = []
    findings = release.scan_findings(ecr, "repo", DIGEST, sleep=sleeps.append)
    assert [f["name"] for f in findings] == ["CVE-1", "CVE-2"] and len(sleeps) == 1


def test_failed_scan_is_an_error():
    ecr = FakeECR(scans=[{"imageScanStatus": {"status": "FAILED", "description": "x"}}])
    with pytest.raises(release.ReleaseError, match="FAILED"):
        release.scan_findings(ecr, "repo", DIGEST, sleep=lambda _: None)


# --- Promotion -----------------------------------------------------------------------


def test_complete_release_is_promotable():
    assert release.promotion_problems(manifest(), STAGING) == []


def test_emulated_or_unvalidated_builds_are_not_promoted_for_their_architecture():
    emulated = manifest(arm_mode="emulated", arm_validation="not-run")
    problems = release.promotion_problems(emulated, STAGING)
    assert len(problems) == 2 and all("native arm64" in p for p in problems)
    # The same release may still serve x86-64 workers, whose builds were native.
    assert release.promotion_problems(emulated, {"worker_architecture": "x86_64"}) == []


def test_unscanned_or_failing_scans_block_promotion():
    assert "not scanned" in release.promotion_problems(manifest(scanned=False), STAGING)[0]
    failing = manifest()
    failing["scans"]["app linux/arm64"] = {"counts": {}, "blocking": ["CVE-9"], "passed": False}
    assert "CVE-9" in release.promotion_problems(failing, STAGING)[0]


def test_promotion_writes_a_compact_pointer_and_needs_published_images():
    ssm = FakeSSM()
    pointer = release.promote(ssm, FakeECR(), "staging", manifest(), STAGING, actor="ci")
    assert pointer["images"] == {image: ref(image) for image in release.IMAGES}
    assert release.history(ssm, "staging")[-1]["version"] == pointer["version"]
    with pytest.raises(release.ReleaseError, match="not in ECR"):
        release.promote(ssm, FakeECR(missing={DIGEST}), "staging", manifest(), STAGING)


def test_promotion_refuses_a_build_older_than_the_database_schema():
    ssm = FakeSSM()
    release.promote(ssm, FakeECR(), "staging", manifest(schema=14), STAGING)
    with pytest.raises(release.ReleaseError, match="at schema 14"):
        release.promote(ssm, FakeECR(), "staging", manifest(schema=13), STAGING)


def test_blocked_promotion_leaves_the_environment_unchanged():
    ssm = FakeSSM()
    with pytest.raises(release.ReleaseError, match="promotion blocked"):
        release.promote(ssm, FakeECR(), "staging", manifest(scanned=False), STAGING)
    assert ssm.versions == []


def deploy(ssm, *entries):
    for version, schema in entries:
        value = manifest(schema=schema)
        value["version"] = version
        release.put_pointer(ssm, "staging", release.release_pointer(value, environment="staging"))


def test_rollback_restores_the_previous_compatible_release():
    ssm = FakeSSM()
    deploy(ssm, ("0.1.0", 13), ("0.2.0", 13))
    pointer = release.rollback(ssm, FakeECR(), "staging", actor="ci")
    assert pointer["version"] == "0.1.0" and pointer["rolled_back_from"] == "0.2.0"
    assert [e["version"] for e in release.history(ssm, "staging")] == ["0.1.0", "0.2.0", "0.1.0"]


def test_rollback_refuses_a_release_that_cannot_read_the_migrated_schema():
    ssm = FakeSSM()
    deploy(ssm, ("0.1.0", 13), ("0.2.0", 14))
    with pytest.raises(release.ReleaseError, match="cannot be restored"):
        release.rollback(ssm, FakeECR(), "staging")
    assert len(ssm.versions) == 2


def test_rollback_skips_repeats_and_can_target_a_version():
    ssm = FakeSSM()
    deploy(ssm, ("0.1.0", 13), ("0.2.0", 13), ("0.3.0", 13), ("0.3.0", 13))
    assert release.rollback_target(release.history(ssm, "staging"))["version"] == "0.2.0"
    assert release.rollback_target(release.history(ssm, "staging"), "0.1.0")["version"] == "0.1.0"
    with pytest.raises(release.ReleaseError, match="no earlier release 9.9.9"):
        release.rollback_target(release.history(ssm, "staging"), "9.9.9")
    with pytest.raises(release.ReleaseError, match="nothing has been deployed"):
        release.rollback_target([])


def test_verify_detects_a_different_deployed_release():
    ssm = FakeSSM()
    value = manifest()
    release.promote(ssm, FakeECR(), "staging", value, STAGING)
    assert release.verify_deployed(ssm, "staging", value)["version"] == value["version"]
    other = manifest()
    other["images"]["app"]["index"] = ref("app", "d")
    with pytest.raises(release.ReleaseError, match="runs"):
        release.verify_deployed(ssm, "staging", other)


# --- Workflow and identity ---------------------------------------------------------


WORKFLOW = (ROOT / ".github/workflows/release.yml").read_text()


def job(name):
    """The text of one top-level job in the release workflow."""
    match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [a-z-]+:\n|\Z)", WORKFLOW, re.M | re.S)
    assert match, name
    return match.group(1)


def test_release_workflow_gates_publishing_and_uses_oidc_only():
    assert "secrets.AWS" not in WORKFLOW and "aws-access-key-id" not in WORKFLOW
    assert re.search(r"^permissions:\n  contents: read\n", WORKFLOW, re.M)
    assert "uses: ./.github/workflows/ci.yml" in job("checks")
    assert "needs: [checks, metadata]" in job("build")
    assert "needs: [metadata, build]" in job("publish")
    assert "needs: publish" in job("deploy-staging")
    assert "environment: staging" in job("deploy-staging")
    for name in ("checks", "metadata"):
        assert "id-token" not in job(name)
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert re.search(r"^on:\n(?:  .*\n)*?  workflow_call:", ci, re.M)


def test_release_builds_refuse_emulation_and_validate_before_push():
    build = job("build")
    assert "refusing an emulated release build" in build
    assert build.index("Validate the sandbox natively") < build.index("docker push")


def test_staging_rolls_back_only_a_release_it_promoted():
    assert "if: failure() && steps.promote.outcome == 'success'" in job("deploy-staging")


def test_github_roles_trust_only_this_repository_tags_and_environments():
    text = (ROOT / "infra/aws/release/main.tf").read_text()
    assert 'values   = ["repo:${var.github_repository}:ref:refs/tags/v*"]' in text
    assert 'values   = ["repo:${var.github_repository}:environment:${each.key}"]' in text
    assert 'image_tag_mutability = "IMMUTABLE"' in text and "scan_on_push = true" in text
    settings, _ = infra.release_settings(require_owner=False)
    assert "*" not in settings["github_repository"]
    assert (
        settings["region"]
        == json.loads((ROOT / "infra/aws/config/staging.tfvars.json").read_text())["region"]
    )


def test_release_settings_reject_wildcards_and_need_an_owner(tmp_path):
    (tmp_path / "release.tfvars.json").write_text(
        json.dumps({"region": "us-west-2", "github_repository": "owner/*"})
    )
    with pytest.raises(infra.InfraError, match="wildcards"):
        infra.release_settings(tmp_path)
    (tmp_path / "release.tfvars.json").write_text(
        json.dumps({"region": "us-west-2", "github_repository": "owner/repo"})
    )
    with pytest.raises(infra.InfraError, match="cost_owner"):
        infra.release_settings(tmp_path)
    (tmp_path / "local.tfvars.json").write_text(json.dumps({"cost_owner": "group11"}))
    assert infra.release_settings(tmp_path)[1] == "group11"


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "docker/compiler.Dockerfile"])
def test_images_carry_release_provenance_labels(dockerfile):
    text = (ROOT / dockerfile).read_text()
    for label in ("org.opencontainers.image.revision", "compiler-optimizer.build-mode"):
        assert label in text
