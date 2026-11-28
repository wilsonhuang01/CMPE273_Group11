"""Traceable image releases and staged promotion (action S): `./project release <command>`.

A release is one tagged source revision (vX.Y.Z matching pyproject.toml) built into an
`app` and a `compiler` image for linux/amd64 and linux/arm64, pushed to Amazon ECR
and referenced only by digest. The release manifest records the source revision,
dependency and contract hashes, base images, compiler versions, the database schema
version, scan results and, per platform, whether the build ran on native hardware
and passed native sandbox validation. Emulated builds are recorded as such and are
never promoted for an architecture they were not validated on.

Each environment has one deployed-release record, the SSM parameter
/<project>/<env>/release. Its parameter history is the rollback list. Schema
migrations run forward only at startup, so a rollback target must support the
highest schema any release in that environment has applied.
"""

import argparse
import ast
import datetime
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "infra" / "aws" / "config"
STORAGE = ROOT / "src" / "compiler_optimizer" / "storage"
ALLOWLIST = ROOT / "infra" / "aws" / "release" / "scan-allowlist.json"
PROJECT = "cmpe273-optimizer"
IMAGES = ("app", "compiler")
PLATFORMS = {
    "linux/amd64": {"arch": "amd64", "target": "linux-x86_64", "worker_architecture": "x86_64"},
    "linux/arm64": {"arch": "arm64", "target": "linux-aarch64", "worker_architecture": "arm64"},
}
MANIFEST_SCHEMA = 1
BLOCKING_SEVERITIES = ("CRITICAL",)
VERSION = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
DIGEST_REFERENCE = re.compile(r"^[a-z0-9.-]+(?::\d+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$")


class ReleaseError(Exception):
    pass


# --- Source facts --------------------------------------------------------------


def project_version(root=ROOT):
    match = re.search(r'^version\s*=\s*"([^"]+)"', (root / "pyproject.toml").read_text(), re.M)
    if not match:
        raise ReleaseError("pyproject.toml has no version")
    return match.group(1)


def release_version(ref, root=ROOT):
    """refs/tags/v1.2.3 (or v1.2.3) -> 1.2.3, which must equal pyproject's version."""
    tag = ref.removeprefix("refs/tags/")
    if not VERSION.match(tag):
        raise ReleaseError(f"{tag!r} is not a release tag; use vMAJOR.MINOR.PATCH")
    version = tag[1:]
    if version != project_version(root):
        raise ReleaseError(
            f"tag {tag} does not match pyproject.toml version {project_version(root)}; "
            "bump the version in the tagged commit"
        )
    return version


def _version_key(tag):
    return tuple(int(part) for part in VERSION.match(tag).groups())


def schema_version(storage=STORAGE):
    """The database schema a build migrates to, read from source without importing it.

    SQLite and PostgreSQL must agree, because both back the same repository code.
    """
    tree = ast.parse((storage / "postgres.py").read_text())
    migrations = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "MIGRATIONS" for t in node.targets
        ):
            migrations = ast.literal_eval(node.value)
    if not migrations:
        raise ReleaseError("could not read postgres MIGRATIONS")
    postgres = max(migrations)
    match = re.search(r"if version != (\d+):", (storage / "repository.py").read_text())
    if not match:
        raise ReleaseError("could not read the SQLite schema version")
    sqlite = int(match.group(1))
    if sqlite != postgres:
        raise ReleaseError(f"SQLite schema {sqlite} and PostgreSQL schema {postgres} differ")
    return postgres


def _sha256_files(paths, root=ROOT):
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def contracts_sha256(root=ROOT):
    paths = [root / "src/compiler_optimizer/shared/v1/contracts.proto"]
    paths += [p for p in (root / "contracts").rglob("*") if p.is_file()]
    return _sha256_files(paths, root)


def base_images(root=ROOT):
    return sorted(
        {
            line.split()[1]
            for name in ("Dockerfile", "docker/compiler.Dockerfile")
            for line in (root / name).read_text().splitlines()
            if line.startswith("FROM ")
        }
    )


def git(*arguments, root=ROOT, check=True):
    result = subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True)
    if check and result.returncode:
        raise ReleaseError(f"git {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result


def previous_release_tag(version, root=ROOT):
    """The highest release tag below ``version`` (None for the first release)."""
    tags = [t for t in git("tag", "--list", "v*", root=root).stdout.split() if VERSION.match(t)]
    older = [t for t in tags if _version_key(t) < _version_key("v" + version)]
    return max(older, key=_version_key) if older else None


def migration_problems(previous, root=ROOT):
    """Released migrations are immutable: every SQL file a previous release shipped must
    be byte-identical now, and the schema version may only grow."""
    if previous is None:
        return []
    problems = []
    relative = STORAGE.relative_to(ROOT).as_posix()
    listed = git("ls-tree", "--name-only", f"{previous}:{relative}", root=root).stdout.split()
    for name in sorted(n for n in listed if n.endswith(".sql")):
        before = git("show", f"{previous}:{relative}/{name}", root=root).stdout
        path = root / relative / name
        if not path.exists():
            problems.append(f"{name} was released in {previous} and has been removed")
        elif path.read_text() != before:
            problems.append(f"{name} changed since {previous}; add a new migration instead")
    old_storage = {}
    for name in ("postgres.py", "repository.py"):
        old_storage[name] = git("show", f"{previous}:{relative}/{name}", root=root).stdout
    with _snapshot(old_storage) as old:
        old_schema = schema_version(old)
    if schema_version(root / relative) < old_schema:
        problems.append(f"schema version went down from {old_schema} ({previous})")
    return problems


class _snapshot:
    def __init__(self, files):
        self.files = files

    def __enter__(self):
        import tempfile

        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name)
        for name, text in self.files.items():
            (path / name).write_text(text)
        return path

    def __exit__(self, *exc):
        self.directory.cleanup()


# --- Build records and the manifest ---------------------------------------------


def platform_record(
    *,
    image,
    platform,
    reference,
    tag,
    build_mode,
    native_validation,
    compiler_version=None,
):
    """What one build job produced for one image on one platform."""
    if image not in IMAGES:
        raise ReleaseError(f"unknown image {image!r}")
    if platform not in PLATFORMS:
        raise ReleaseError(f"unsupported platform {platform!r}")
    if not DIGEST_REFERENCE.match(reference):
        raise ReleaseError(f"{reference!r} is not a repository@sha256 digest reference")
    if build_mode not in ("native", "emulated"):
        raise ReleaseError("build_mode must be native or emulated")
    if native_validation not in ("passed", "not-run"):
        raise ReleaseError("native_validation must be passed or not-run")
    if build_mode == "emulated" and native_validation == "passed":
        raise ReleaseError("an emulated build cannot claim native validation")
    return {
        "image": image,
        "platform": platform,
        "target": PLATFORMS[platform]["target"],
        "reference": reference,
        "tag": tag,
        "build_mode": build_mode,
        "native_validation": native_validation,
        "compiler_version": compiler_version,
    }


def assemble_manifest(*, version, revision, records, indexes, run_url=None, root=ROOT):
    """Combine per-platform records and the multi-arch indexes into one manifest."""
    images = {}
    for name in IMAGES:
        if not DIGEST_REFERENCE.match(indexes.get(name, "")):
            raise ReleaseError(f"missing digest reference for the {name} index")
        platforms = {r["platform"]: r for r in records if r["image"] == name}
        if set(platforms) != set(PLATFORMS):
            raise ReleaseError(f"{name} needs exactly one build for each of {sorted(PLATFORMS)}")
        images[name] = {
            "index": indexes[name],
            "platforms": {
                platform: {key: value for key, value in record.items() if key != "image"}
                for platform, record in platforms.items()
            },
        }
    if not re.fullmatch(r"[0-9a-f]{40}", revision or ""):
        raise ReleaseError("revision must be a full git commit hash")
    return {
        "manifest_schema": MANIFEST_SCHEMA,
        "version": version,
        "tag": "v" + version,
        "source_revision": revision,
        "created_at": datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat(),
        "workflow_run": run_url,
        "database_schema_version": schema_version(root / STORAGE.relative_to(ROOT)),
        "contracts_sha256": contracts_sha256(root),
        "constraints_sha256": hashlib.sha256((root / "constraints.txt").read_bytes()).hexdigest(),
        "pyproject_sha256": hashlib.sha256((root / "pyproject.toml").read_bytes()).hexdigest(),
        "base_images": base_images(root),
        "images": images,
        "scans": {},
    }


def all_platform_references(manifest):
    for name, image in manifest["images"].items():
        for platform, record in image["platforms"].items():
            yield name, platform, record["reference"]


def split_reference(reference):
    repository, digest = reference.split("@", 1)
    return repository.split("/", 1)[1], digest


# --- Image scanning --------------------------------------------------------------


def load_allowlist(path=ALLOWLIST, today=None):
    """Accepted findings: {"id", "reason", "expires"}; expired entries no longer apply."""
    if not path.exists():
        return set()
    today = today or datetime.date.today()
    accepted = set()
    for entry in json.loads(path.read_text()).get("findings", []):
        if not entry.get("reason") or not entry.get("expires"):
            raise ReleaseError(f"allowlist entry {entry.get('id')} needs a reason and expiry")
        if datetime.date.fromisoformat(entry["expires"]) >= today:
            accepted.add(entry["id"])
    return accepted


def scan_findings(ecr, repository, digest, *, timeout=900, poll=10, sleep=time.sleep):
    """Wait for the push-time scan, then return its findings."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            response = ecr.describe_image_scan_findings(
                repositoryName=repository, imageId={"imageDigest": digest}
            )
        except ecr.exceptions.ScanNotFoundException:
            response = {"imageScanStatus": {"status": "PENDING"}}
        status = response["imageScanStatus"]["status"]
        if status in ("COMPLETE", "ACTIVE"):
            break
        if status not in ("IN_PROGRESS", "PENDING"):
            description = response["imageScanStatus"].get("description", "")
            raise ReleaseError(f"scan of {repository}@{digest} is {status}: {description}")
        if time.monotonic() > deadline:
            raise ReleaseError(f"scan of {repository}@{digest} did not finish in {timeout}s")
        sleep(poll)
    findings = list(response.get("imageScanFindings", {}).get("findings", []))
    token = response.get("nextToken")
    while token:
        response = ecr.describe_image_scan_findings(
            repositoryName=repository, imageId={"imageDigest": digest}, nextToken=token
        )
        findings += response.get("imageScanFindings", {}).get("findings", [])
        token = response.get("nextToken")
    return findings


def scan_summary(findings, accepted, blocking=BLOCKING_SEVERITIES):
    counts = {}
    for finding in findings:
        counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
    blocked = sorted(
        {f["name"] for f in findings if f["severity"] in blocking and f["name"] not in accepted}
    )
    return {"counts": counts, "blocking": blocked, "passed": not blocked}


def scan_manifest(manifest, ecr, accepted, **wait):
    for name, platform, reference in all_platform_references(manifest):
        repository, digest = split_reference(reference)
        summary = scan_summary(scan_findings(ecr, repository, digest, **wait), accepted)
        manifest["scans"][f"{name} {platform}"] = summary
    return manifest


# --- Promotion and rollback --------------------------------------------------------


def environment_settings(environment, config=CONFIG):
    path = config / f"{environment}.tfvars.json"
    if not path.exists():
        raise ReleaseError(f"unknown environment {environment!r}")
    return json.loads(path.read_text())


def promotion_problems(manifest, settings):
    """Reasons a release may not run in an environment; empty means promotable."""
    problems = []
    if manifest.get("manifest_schema") != MANIFEST_SCHEMA:
        problems.append("unsupported manifest schema")
    architecture = settings["worker_architecture"]
    platform = next(p for p, v in PLATFORMS.items() if v["worker_architecture"] == architecture)
    for name, image in manifest["images"].items():
        record = image["platforms"].get(platform)
        if record is None:
            problems.append(f"{name} has no {platform} image for {architecture} workers")
            continue
        if record["build_mode"] != "native" or record["native_validation"] != "passed":
            problems.append(
                f"{name} {platform} was not built and validated on native {architecture} "
                f"hardware ({record['build_mode']}, validation {record['native_validation']})"
            )
    expected = {f"{n} {p}" for n, p, _ in all_platform_references(manifest)}
    missing = sorted(expected - set(manifest.get("scans", {})))
    if missing:
        problems.append(f"images not scanned: {missing}")
    for key, summary in sorted(manifest.get("scans", {}).items()):
        if not summary["passed"]:
            problems.append(f"{key} has unaccepted findings {summary['blocking']}")
    return problems


def release_pointer(manifest, *, environment, actor=None, rolled_back_from=None):
    """The compact deployed-release record (fits a 4 KB standard SSM parameter)."""
    pointer = {
        "manifest_schema": manifest["manifest_schema"],
        "environment": environment,
        "version": manifest["version"],
        "source_revision": manifest["source_revision"],
        "database_schema_version": manifest["database_schema_version"],
        "contracts_sha256": manifest["contracts_sha256"],
        "images": {name: image["index"] for name, image in manifest["images"].items()},
        "promoted_at": datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat(),
        "promoted_by": actor,
    }
    if rolled_back_from:
        pointer["rolled_back_from"] = rolled_back_from
    if len(json.dumps(pointer, separators=(",", ":"))) > 4096:
        raise ReleaseError("release pointer exceeds the SSM standard parameter size")
    return pointer


def parameter_name(environment):
    return f"/{PROJECT}/{environment}/release"


def history(ssm, environment):
    """Deployed releases in this environment, oldest first."""
    entries = []
    paginator = ssm.get_paginator("get_parameter_history")
    try:
        for page in paginator.paginate(Name=parameter_name(environment)):
            entries += page["Parameters"]
    except ssm.exceptions.ParameterNotFound:
        return []
    entries.sort(key=lambda entry: entry["Version"])
    return [json.loads(entry["Value"]) for entry in entries]


def rollback_target(deployed, version=None):
    """The newest earlier release that can run on the environment's database schema.

    Migrations apply forward at startup, so the database is at the highest schema any
    deployed release used; an older build refuses a schema it does not know.
    """
    if not deployed:
        raise ReleaseError("nothing has been deployed to this environment")
    current = deployed[-1]
    database_schema = max(entry["database_schema_version"] for entry in deployed)
    seen = set()
    for entry in reversed(deployed[:-1]):
        if entry["version"] == current["version"] or entry["version"] in seen:
            continue
        seen.add(entry["version"])
        if version is not None and entry["version"] != version:
            continue
        if entry["database_schema_version"] < database_schema:
            raise ReleaseError(
                f"{entry['version']} supports schema {entry['database_schema_version']}, "
                f"but the database is at schema {database_schema}; it cannot be restored. "
                "Fix forward with a new release instead."
            )
        return entry
    wanted = f" {version}" if version else ""
    raise ReleaseError(f"no earlier release{wanted} to roll back to")


def put_pointer(ssm, environment, pointer):
    ssm.put_parameter(
        Name=parameter_name(environment),
        Value=json.dumps(pointer, separators=(",", ":"), sort_keys=True),
        Type="String",
        Tier="Standard",
        Overwrite=True,
        Description=f"Deployed release for {environment} (action S); history is the rollback list",
    )


def check_images_exist(ecr, references):
    for reference in references:
        repository, digest = split_reference(reference)
        try:
            ecr.describe_images(repositoryName=repository, imageIds=[{"imageDigest": digest}])
        except ecr.exceptions.ImageNotFoundException as error:
            raise ReleaseError(f"{reference} is not in ECR") from error


def promote(ssm, ecr, environment, manifest, settings, actor=None):
    if problems := promotion_problems(manifest, settings):
        raise ReleaseError("promotion blocked:\n  " + "\n  ".join(problems))
    check_images_exist(ecr, [image["index"] for image in manifest["images"].values()])
    deployed = history(ssm, environment)
    if deployed:
        database_schema = max(entry["database_schema_version"] for entry in deployed)
        if manifest["database_schema_version"] < database_schema:
            raise ReleaseError(
                f"{manifest['version']} supports schema {manifest['database_schema_version']} "
                f"but {environment} is at schema {database_schema}"
            )
    pointer = release_pointer(manifest, environment=environment, actor=actor)
    put_pointer(ssm, environment, pointer)
    return pointer


def rollback(ssm, ecr, environment, version=None, actor=None):
    deployed = history(ssm, environment)
    target = rollback_target(deployed, version)
    check_images_exist(ecr, target["images"].values())
    pointer = {
        **target,
        "promoted_at": datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat(),
        "promoted_by": actor,
        "rolled_back_from": deployed[-1]["version"],
    }
    put_pointer(ssm, environment, pointer)
    return pointer


def verify_deployed(ssm, environment, manifest):
    deployed = history(ssm, environment)
    if not deployed:
        raise ReleaseError(f"{environment} has no deployed release")
    current = deployed[-1]
    expected = {name: image["index"] for name, image in manifest["images"].items()}
    if current["version"] != manifest["version"] or current["images"] != expected:
        raise ReleaseError(
            f"{environment} runs {current['version']} {current['images']}, not "
            f"{manifest['version']} {expected}"
        )
    return current


# --- Command line ----------------------------------------------------------------


def _region():
    return json.loads((CONFIG / "release.tfvars.json").read_text())["region"]


def _clients(names):
    try:
        import boto3
    except ImportError as error:
        raise ReleaseError("boto3 is required: pip install -e '.[aws]'") from error
    session = boto3.session.Session(region_name=_region())
    return [session.client(name) for name in names]


def _write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="./project release", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    tag = sub.add_parser("check-tag", help="tag matches pyproject version and migrations")
    tag.add_argument("--ref", required=True)

    record = sub.add_parser("record", help="write one image/platform build record")
    record.add_argument("--image", choices=IMAGES, required=True)
    record.add_argument("--platform", choices=sorted(PLATFORMS), required=True)
    record.add_argument("--reference", required=True)
    record.add_argument("--tag", required=True)
    record.add_argument("--build-mode", choices=("native", "emulated"), required=True)
    record.add_argument("--native-validation", choices=("passed", "not-run"), required=True)
    record.add_argument("--compiler-version")
    record.add_argument("--output", required=True)

    assemble = sub.add_parser("assemble", help="combine build records into a manifest")
    assemble.add_argument("--ref", required=True)
    assemble.add_argument("--revision", required=True)
    assemble.add_argument("--records", type=Path, required=True)
    assemble.add_argument("--index", action="append", default=[], metavar="IMAGE=REF")
    assemble.add_argument("--run-url")
    assemble.add_argument("--output", required=True)

    scan = sub.add_parser("scan", help="wait for ECR scans and record them in the manifest")
    scan.add_argument("--manifest", required=True)

    for name, text in (
        ("promote", "deploy a scanned, validated release to an environment"),
        ("verify", "confirm an environment runs this manifest's release"),
    ):
        command = sub.add_parser(name, help=text)
        command.add_argument("--env", required=True)
        command.add_argument("--manifest", required=True)
        command.add_argument("--actor")

    back = sub.add_parser("rollback", help="restore the previous schema-compatible release")
    back.add_argument("--env", required=True)
    back.add_argument("--to", metavar="VERSION")
    back.add_argument("--actor")

    status = sub.add_parser("status", help="show an environment's release history")
    status.add_argument("--env", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "check-tag":
            version = release_version(args.ref)
            previous = previous_release_tag(version)
            if problems := migration_problems(previous):
                raise ReleaseError("migration check failed:\n  " + "\n  ".join(problems))
            print(f"RELEASE {version}: previous {previous or 'none'}, schema {schema_version()}")
        elif args.command == "record":
            _write(
                args.output,
                platform_record(
                    image=args.image,
                    platform=args.platform,
                    reference=args.reference,
                    tag=args.tag,
                    build_mode=args.build_mode,
                    native_validation=args.native_validation,
                    compiler_version=args.compiler_version,
                ),
            )
        elif args.command == "assemble":
            records = [json.loads(p.read_text()) for p in sorted(args.records.rglob("*.json"))]
            indexes = dict(item.split("=", 1) for item in args.index)
            manifest = assemble_manifest(
                version=release_version(args.ref),
                revision=args.revision,
                records=records,
                indexes=indexes,
                run_url=args.run_url,
            )
            _write(args.output, manifest)
            print(f"MANIFEST: {args.output}")
        elif args.command == "scan":
            (ecr,) = _clients(["ecr"])
            manifest = json.loads(Path(args.manifest).read_text())
            scan_manifest(manifest, ecr, load_allowlist())
            _write(args.manifest, manifest)
            print(json.dumps(manifest["scans"], indent=2, sort_keys=True))
            failed = [k for k, v in manifest["scans"].items() if not v["passed"]]
            if failed:
                raise ReleaseError(f"blocking findings in {failed}; see scan-allowlist.json")
        elif args.command == "promote":
            ssm, ecr = _clients(["ssm", "ecr"])
            manifest = json.loads(Path(args.manifest).read_text())
            pointer = promote(
                ssm, ecr, args.env, manifest, environment_settings(args.env), args.actor
            )
            print(f"PROMOTED: {pointer['version']} to {args.env}")
        elif args.command == "verify":
            (ssm,) = _clients(["ssm"])
            current = verify_deployed(ssm, args.env, json.loads(Path(args.manifest).read_text()))
            print(f"VERIFIED: {args.env} runs {current['version']}")
        elif args.command == "rollback":
            ssm, ecr = _clients(["ssm", "ecr"])
            pointer = rollback(ssm, ecr, args.env, args.to, args.actor)
            print(f"ROLLED BACK: {args.env} to {pointer['version']}")
        elif args.command == "status":
            (ssm,) = _clients(["ssm"])
            for entry in history(ssm, args.env):
                source = entry.get("rolled_back_from")
                note = f" (rollback from {source})" if source else ""
                print(
                    f"{entry['promoted_at']}  {entry['version']}  schema "
                    f"{entry['database_schema_version']}  {entry['source_revision'][:12]}{note}"
                )
    except ReleaseError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
