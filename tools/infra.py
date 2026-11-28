"""AWS environment provisioning (actions Q, R and S): `./project infra <command> --env <name>`.

Terraform owns resources; this wrapper adds the rules Terraform cannot express:
committed settings never contain credentials, plans are saved for review before
apply, secrets are seeded in memory after apply, broker and cache credentials are
put into service and rotated, and protected environments need an explicit
confirmation before teardown. It also builds the Lambda packages reproducibly and
runs the in-VPC service check (`validate`). The account-level release stack (ECR
repositories and GitHub OIDC roles, action S) uses the `release-*` commands.
"""

import argparse
import datetime
import hashlib
import json
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra" / "aws"
STACK = INFRA / "environment"
BOOTSTRAP = INFRA / "bootstrap"
RELEASE_STACK = INFRA / "release"
CONFIG = INFRA / "config"
PLANS = ROOT / "var" / "infra"
PACKAGES = PLANS / "lambda"
ROTATION_SOURCE = INFRA / "lambda" / "rotation.py"
RDS_CA_URL = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
# Lambda python3.12 on arm64 (Amazon Linux 2023, glibc 2.34), as rotation.tf declares.
LAMBDA_PLATFORMS = ("manylinux_2_28_aarch64", "manylinux2014_aarch64")
LAMBDA_PYTHON = "3.12"
ZIP_DATE = (2020, 1, 1, 0, 0, 0)
AUTH_ENFORCED_TAG = "AuthEnforced"
ENVIRONMENTS = ("dev", "staging", "production")
PROJECT = "cmpe273-optimizer"
LOCAL_KEYS = {"cost_owner", "budget_alert_emails"}
SECRET_WORDS = ("password", "secret", "token", "access_key", "private_key", "credential")


class InfraError(Exception):
    pass


def _secret_like_keys(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            where = f"{path}.{key}" if path else key
            if any(word in key.lower() for word in SECRET_WORDS):
                yield where
            yield from _secret_like_keys(item, where)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _secret_like_keys(item, f"{path}[{index}]")


def _read_json(path):
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise InfraError(f"{path.relative_to(ROOT)} is not valid JSON: {error}") from error


def load_settings(environment, config=CONFIG):
    """Return (merged settings, var files) for an environment, rejecting credentials."""
    if environment not in ENVIRONMENTS:
        raise InfraError(f"unknown environment {environment!r}; use one of {ENVIRONMENTS}")
    committed = config / f"{environment}.tfvars.json"
    if not committed.exists():
        raise InfraError(f"missing {committed}")
    files = [committed]
    settings = _read_json(committed)
    if settings.get("environment") != environment:
        raise InfraError(f"{committed.name} must set environment to {environment!r}")
    if LOCAL_KEYS & settings.keys():
        raise InfraError(f"{committed.name} must not set {sorted(LOCAL_KEYS & settings.keys())}")
    for name in ("local.tfvars.json", f"{environment}.local.tfvars.json"):
        path = config / name
        if path.exists():
            local = _read_json(path)
            unexpected = set(local) - LOCAL_KEYS
            if unexpected:
                raise InfraError(
                    f"{name} may only set {sorted(LOCAL_KEYS)}; found {sorted(unexpected)}"
                )
            settings.update(local)
            files.append(path)
    found = sorted(_secret_like_keys(settings))
    if found:
        raise InfraError(f"credentials do not belong in tfvars ({found}); use Secrets Manager")
    missing = sorted(key for key in LOCAL_KEYS if not settings.get(key))
    if missing:
        raise InfraError(
            f"set {missing} in infra/aws/config/local.tfvars.json "
            "(copy local.tfvars.json.example); these are kept out of git"
        )
    return settings, files


def expiry_warning(settings, today=None):
    expires = settings.get("expires_on")
    today = today or datetime.date.today()
    if expires and datetime.date.fromisoformat(expires) < today:
        return (
            f"{settings['environment']} expired on {expires}; tear it down with "
            f"`./project infra down --env {settings['environment']}` or extend expires_on"
        )
    return None


def destroy_allowed(settings, confirmation):
    """Retained environments are torn down only when the operator repeats the name."""
    if settings.get("data_protection") != "retain":
        return True
    return confirmation == settings["environment"]


def load_backend(config=CONFIG):
    path = config / "backend.json"
    if not path.exists():
        raise InfraError("no remote state configured; run `./project infra bootstrap` first")
    backend = _read_json(path)
    for key in ("bucket", "dynamodb_table", "region"):
        if not backend.get(key):
            raise InfraError(f"backend.json is missing {key}")
    return backend


def backend_arguments(backend, environment):
    return [
        f"-backend-config=bucket={backend['bucket']}",
        f"-backend-config=key={PROJECT}/{environment}/terraform.tfstate",
        f"-backend-config=region={backend['region']}",
        f"-backend-config=dynamodb_table={backend['dynamodb_table']}",
        "-backend-config=encrypt=true",
    ]


def check_account(backend, account_id):
    """The state bucket name embeds its account; refuse to plan against another one."""
    if f"-{account_id}-" not in backend["bucket"]:
        raise InfraError(
            f"current AWS credentials are for account {account_id}, but the state bucket "
            f"{backend['bucket']} belongs to another account"
        )


def var_arguments(files):
    return [item if isinstance(item, str) else f"-var-file={item}" for item in files]


def seed_secrets(client, names, log=print):
    """Give every empty secret a random value. Values exist only in memory here."""
    seeded = []
    for name in names:
        description = client.describe_secret(SecretId=name)
        if description.get("VersionIdsToStages"):
            log(f"secret {name}: already set")
            continue
        value = client.get_random_password(
            PasswordLength=48, ExcludePunctuation=True, RequireEachIncludedType=True
        )["RandomPassword"]
        client.put_secret_value(
            SecretId=name, SecretString=value, ClientRequestToken=str(uuid.uuid4())
        )
        seeded.append(name)
        log(f"secret {name}: seeded")
    return seeded


def seed_broker_secret(client, name, username, bootstrap, log=print):
    """Store the broker's first credentials. Returns True while they are still in use."""
    description = client.describe_secret(SecretId=name)
    if not description.get("VersionIdsToStages"):
        client.put_secret_value(
            SecretId=name,
            SecretString=json.dumps({"username": username, "password": bootstrap}),
            ClientRequestToken=str(uuid.uuid4()),
        )
        log(f"secret {name}: seeded with the broker's bootstrap credentials")
        return True
    current = json.loads(client.get_secret_value(SecretId=name)["SecretString"])
    return current == {"username": username, "password": bootstrap}


def rotate_and_wait(client, name, *, timeout=900, poll=5, sleep=time.sleep, log=print):
    """Start a rotation and wait until its new version is AWSCURRENT."""
    version = client.rotate_secret(SecretId=name, RotateImmediately=True)["VersionId"]
    deadline = time.monotonic() + timeout
    while True:
        stages = client.describe_secret(SecretId=name).get("VersionIdsToStages", {})
        if "AWSCURRENT" in stages.get(version, []):
            log(f"secret {name}: rotated")
            return version
        if time.monotonic() > deadline:
            raise InfraError(
                f"rotation of {name} did not finish in {timeout}s; check the rotation "
                "function's log group under /<project>/<env>/lambda/"
            )
        sleep(poll)


def wait_cache_available(elasticache, group_id, *, timeout=1200, poll=15, sleep=time.sleep):
    deadline = time.monotonic() + timeout
    while True:
        group = elasticache.describe_replication_groups(ReplicationGroupId=group_id)
        group = group["ReplicationGroups"][0]
        if group["Status"] == "available":
            return group
        if time.monotonic() > deadline:
            raise InfraError(f"cache {group_id} is still {group['Status']}")
        sleep(poll)


def enforce_cache_auth(secrets, elasticache, name, group_id, *, sleep=time.sleep, log=print):
    """Require the seeded AUTH token on a cache created without one (ROTATE, then SET).

    ElastiCache accepts unauthenticated clients between the two steps; the secret is
    tagged only once SET has completed, so an interrupted run repeats both steps.
    """
    description = secrets.describe_secret(SecretId=name)
    if {"Key": AUTH_ENFORCED_TAG, "Value": "true"} in description.get("Tags", []):
        log(f"cache {group_id}: AUTH already required")
        return False
    token = secrets.get_secret_value(SecretId=name)["SecretString"]
    for strategy in ("ROTATE", "SET"):
        wait_cache_available(elasticache, group_id, sleep=sleep)
        elasticache.modify_replication_group(
            ReplicationGroupId=group_id,
            AuthToken=token,
            AuthTokenUpdateStrategy=strategy,
            ApplyImmediately=True,
        )
    wait_cache_available(elasticache, group_id, sleep=sleep)
    secrets.tag_resource(SecretId=name, Tags=[{"Key": AUTH_ENFORCED_TAG, "Value": "true"}])
    log(f"cache {group_id}: AUTH token required")
    return True


# --- Lambda packages -----------------------------------------------------------


def write_zip(destination, entries):
    """Write {archive name: bytes} with fixed timestamps and order, so equal inputs
    give byte-identical archives and Terraform sees no change."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(".partial")
    with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, entries[name])
    partial.replace(destination)
    return destination


def _tree_digest(paths):
    digest = hashlib.sha256()
    for path in paths:
        files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        for file in files:
            if "__pycache__" in file.parts or file.suffix == ".pyc":
                continue
            digest.update(str(file.relative_to(ROOT)).encode() + b"\0" + file.read_bytes())
    return digest.hexdigest()[:16]


def build_rotation_package(output=PACKAGES):
    return write_zip(output / "rotation.zip", {"rotation.py": ROTATION_SOURCE.read_bytes()})


def _https_context():
    """Verify downloads even on Pythons without a configured system CA store."""
    try:
        import certifi
    except ImportError:
        try:
            from pip._vendor import certifi
        except ImportError:
            return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _rds_ca_bundle(cache):
    if not cache.exists():
        with urllib.request.urlopen(  # noqa: S310 - fixed https URL
            RDS_CA_URL, timeout=30, context=_https_context()
        ) as response:
            bundle = response.read(2_000_000)
        if b"-----BEGIN CERTIFICATE-----" not in bundle:
            raise InfraError(f"{RDS_CA_URL} did not return a PEM bundle")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(bundle)
    return cache.read_bytes()


def build_service_check_package(output=PACKAGES, python=sys.executable):
    """The optimizer wheel plus linux/arm64 drivers, for the in-VPC service check."""
    inputs = [ROOT / "pyproject.toml", ROOT / "constraints.txt", ROOT / "src"]
    destination = output / f"service-check-{_tree_digest(inputs)}.zip"
    if destination.exists():
        return destination
    with tempfile.TemporaryDirectory() as scratch:
        scratch = Path(scratch)
        quiet = ["--quiet", "--disable-pip-version-check"]
        # Build from a copy: setuptools writes build/ and *.egg-info next to its sources.
        source = scratch / "source"
        shutil.copytree(
            ROOT / "src",
            source / "src",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
        )
        for name in ("pyproject.toml", "README.md"):
            shutil.copy2(ROOT / name, source / name)
        subprocess.run(
            [python, "-m", "pip", "wheel", *quiet, "--no-deps", "-w", scratch / "wheel", source],
            check=True,
        )
        wheel = next((scratch / "wheel").glob("*.whl"))
        subprocess.run(
            [
                python,
                "-m",
                "pip",
                "install",
                *quiet,
                "--no-compile",
                "--target",
                scratch / "site",
                *(item for platform in LAMBDA_PLATFORMS for item in ("--platform", platform)),
                "--implementation",
                "cp",
                "--python-version",
                LAMBDA_PYTHON,
                "--only-binary=:all:",
                "-c",
                ROOT / "constraints.txt",
                f"{wheel}[postgres]",
            ],
            check=True,
        )
        site = scratch / "site"
        entries = {
            str(path.relative_to(site)): path.read_bytes()
            for path in site.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.name not in ("direct_url.json", "RECORD", "INSTALLER")
            and not path.relative_to(site).parts[0] == "bin"
        }
    entries["rds-global-bundle.pem"] = _rds_ca_bundle(output / "rds-global-bundle.pem")
    for stale in output.glob("service-check-*.zip"):
        stale.unlink()
    return write_zip(destination, entries)


def package_arguments(packages):
    return [f"-var={name}={path}" for name, path in sorted(packages.items())]


# --- Process helpers ---------------------------------------------------------


def terraform(arguments, directory, capture=False):
    if not shutil.which("terraform"):
        raise InfraError("Terraform >= 1.5 is required: brew install terraform")
    command = ["terraform", f"-chdir={directory}", *arguments]
    if capture:
        return subprocess.run(command, check=True, capture_output=True, text=True).stdout
    subprocess.run(command, check=True)
    return None


def aws_session(region):
    try:
        import boto3
    except ImportError as error:
        raise InfraError("boto3 is required: .venv/bin/pip install -e '.[aws]'") from error
    return boto3.session.Session(region_name=region)


def caller_account(session):
    try:
        return session.client("sts").get_caller_identity()["Account"]
    except Exception as error:  # noqa: BLE001 - any credential failure has the same remedy
        raise InfraError(
            "no usable AWS credentials; run `aws configure sso` or `aws sso login` "
            "and export AWS_PROFILE"
        ) from error


def confirm(prompt, assume_yes):
    if assume_yes:
        return True
    return input(f"{prompt} [type 'yes'] ").strip() == "yes"


# --- Commands ----------------------------------------------------------------


def cmd_check(_args):
    for directory in (BOOTSTRAP, STACK, RELEASE_STACK):
        terraform(["fmt", "-check", "-recursive", "-diff"], directory)
        terraform(["init", "-backend=false", "-input=false"], directory)
        terraform(["validate"], directory)
    for environment in ENVIRONMENTS:
        settings = _read_json(CONFIG / f"{environment}.tfvars.json")
        if settings.get("environment") != environment:
            raise InfraError(f"{environment}.tfvars.json has the wrong environment")
        if found := sorted(_secret_like_keys(settings)):
            raise InfraError(f"{environment}.tfvars.json contains credential-like keys {found}")
    release_settings(CONFIG, require_owner=False)
    print("CHECKED: Terraform formatting, validation and committed environment settings")


def cmd_bootstrap(args):
    if not args.region or not args.cost_owner:
        raise InfraError("bootstrap needs --region and --cost-owner")
    session = aws_session(args.region)
    account = caller_account(session)
    print(f"Bootstrapping Terraform state in account {account}, region {args.region}")
    variables = [f"-var=region={args.region}", f"-var=cost_owner={args.cost_owner}"]
    terraform(["init", "-input=false"], BOOTSTRAP)
    if args.destroy:
        print("The state bucket must already be empty; versions are not deleted automatically.")
        if confirm("Destroy the shared Terraform state bucket and lock table?", False):
            terraform(["destroy", "-input=false", *variables], BOOTSTRAP)
        return
    terraform(
        ["apply", "-input=false", *(["-auto-approve"] if args.yes else []), *variables], BOOTSTRAP
    )
    outputs = json.loads(terraform(["output", "-json"], BOOTSTRAP, capture=True))
    backend = {
        "bucket": outputs["state_bucket"]["value"],
        "dynamodb_table": outputs["lock_table"]["value"],
        "region": outputs["region"]["value"],
    }
    (CONFIG / "backend.json").write_text(json.dumps(backend, indent=2) + "\n")
    print(f"WROTE: {(CONFIG / 'backend.json').relative_to(ROOT)} (git-ignored)")


def prepare(environment, packages=True):
    settings, files = load_settings(environment)
    if packages:
        print("Building Lambda packages (rotation and service check)")
        files = [
            *files,
            *package_arguments(
                {
                    "rotation_package": build_rotation_package(),
                    "service_check_package": build_service_check_package(),
                }
            ),
        ]
    if warning := expiry_warning(settings):
        print(f"WARNING: {warning}", file=sys.stderr)
    backend = load_backend()
    session = aws_session(settings["region"])
    account = caller_account(session)
    check_account(backend, account)
    print(f"{environment}: account {account}, region {settings['region']}")
    terraform(
        ["init", "-input=false", "-reconfigure", *backend_arguments(backend, environment)], STACK
    )
    return settings, files, session


def write_plan(environment, files, destroy=False):
    PLANS.mkdir(parents=True, exist_ok=True)
    suffix = "destroy" if destroy else "apply"
    plan = PLANS / f"{environment}-{suffix}.tfplan"
    terraform(
        [
            "plan",
            "-input=false",
            *(["-destroy"] if destroy else []),
            *var_arguments(files),
            f"-out={plan}",
        ],
        STACK,
    )
    review = plan.with_suffix(".txt")
    review.write_text(terraform(["show", "-no-color", str(plan)], STACK, capture=True))
    print(f"PLAN: {review.relative_to(ROOT)} (review before applying)")
    return plan


def cmd_plan(args):
    _, files, _ = prepare(args.env)
    write_plan(args.env, files)


def cmd_up(args):
    _, files, session = prepare(args.env)
    plan = write_plan(args.env, files)
    if not confirm(f"Apply this plan to {args.env}?", args.yes):
        print("Not applied.")
        return
    terraform(["apply", "-input=false", str(plan)], STACK)
    plan.unlink(missing_ok=True)
    activate_credentials(session)
    print(f"READY: {args.env} infrastructure. References: ./project infra output --env {args.env}")
    print(f"Check the managed services with: ./project infra validate --env {args.env}")


def admin_outputs():
    return json.loads(terraform(["output", "-json", "data_services_admin"], STACK, capture=True))


def activate_credentials(session):
    """Seed empty secrets, then put the broker and cache credentials into service."""
    secrets = session.client("secretsmanager")
    admin = admin_outputs()
    names = json.loads(terraform(["output", "-json", "secret_names"], STACK, capture=True))
    seed_secrets(secrets, [name for name in names if name != admin["broker_secret"]])
    bootstrap = terraform(["output", "-raw", "broker_bootstrap_password"], STACK, capture=True)
    if seed_broker_secret(secrets, admin["broker_secret"], admin["broker_user"], bootstrap):
        # Two rotations: the first moves clients to the alternate user, the second
        # replaces the bootstrap password that Terraform state still holds.
        for _ in range(2):
            rotate_and_wait(secrets, admin["broker_secret"])
    del bootstrap
    enforce_cache_auth(
        secrets, session.client("elasticache"), admin["cache_secret"], admin["cache_group_id"]
    )


def cmd_rotate(args):
    _, _, session = prepare(args.env, packages=False)
    admin = admin_outputs()
    secrets = session.client("secretsmanager")
    chosen = {
        "broker": admin["broker_secret"],
        "cache": admin["cache_secret"],
        "database": admin["database_secret"],
    }
    targets = chosen if args.secret == "all" else {args.secret: chosen[args.secret]}
    for name in targets.values():
        rotate_and_wait(secrets, name)


def cmd_validate(args):
    _, _, session = prepare(args.env, packages=False)
    function = admin_outputs()["service_check_lambda"]
    print(f"Running {function} inside the VPC")
    response = session.client("lambda").invoke(
        FunctionName=function, Payload=json.dumps({"services": args.services or None}).encode()
    )
    payload = json.loads(response["Payload"].read())
    if response.get("FunctionError"):
        raise InfraError(f"service check failed to run: {payload.get('errorMessage', payload)}")
    print(json.dumps(payload, indent=2))
    if not payload.get("ok"):
        raise InfraError("one or more service checks failed")
    print("VALIDATED: broker, cache, database and artifact store")


def cmd_down(args):
    settings, files, _ = prepare(args.env)
    if not destroy_allowed(settings, args.confirm_protected):
        raise InfraError(
            f"{args.env} uses data_protection=retain; rerun with "
            f"--confirm-protected {args.env} after taking any required backups"
        )
    plan = write_plan(args.env, files, destroy=True)
    retained = (
        "Secrets enter a 30-day recovery window and KMS keys a 30-day deletion window."
        if settings.get("data_protection") == "retain"
        else "Secrets are deleted immediately; KMS keys enter a 7-day deletion window."
    )
    print(retained)
    if not confirm(f"Destroy {args.env}?", args.yes):
        print("Not destroyed.")
        return
    terraform(["apply", "-input=false", str(plan)], STACK)
    plan.unlink(missing_ok=True)
    print(f"REMOVED: {args.env} infrastructure. Remote state history stays in the state bucket.")


def cmd_output(args):
    prepare(args.env, packages=False)
    print(terraform(["output", "-json", "environment_config"], STACK, capture=True))


# --- Release stack (action S) ----------------------------------------------------


def release_settings(config=CONFIG, require_owner=True):
    """Committed release.tfvars.json plus the git-ignored cost owner."""
    settings = _read_json(config / "release.tfvars.json")
    if LOCAL_KEYS & settings.keys():
        raise InfraError("release.tfvars.json must not set personal settings")
    if found := sorted(_secret_like_keys(settings)):
        raise InfraError(f"release.tfvars.json contains credential-like keys {found}")
    if any(ch in settings.get("github_repository", "*") for ch in "*?"):
        raise InfraError("github_repository must name one repository, without wildcards")
    owner = None
    local = config / "local.tfvars.json"
    if local.exists():
        owner = _read_json(local).get("cost_owner")
    if require_owner and not owner:
        raise InfraError("set cost_owner in infra/aws/config/local.tfvars.json")
    return settings, owner


def prepare_release():
    settings, owner = release_settings()
    backend = load_backend()
    session = aws_session(settings["region"])
    check_account(backend, caller_account(session))
    terraform(
        ["init", "-input=false", "-reconfigure", *backend_arguments(backend, "release")],
        RELEASE_STACK,
    )
    return [f"-var-file={CONFIG / 'release.tfvars.json'}", f"-var=cost_owner={owner}"]


def release_plan(variables, destroy=False):
    PLANS.mkdir(parents=True, exist_ok=True)
    plan = PLANS / f"release-{'destroy' if destroy else 'apply'}.tfplan"
    terraform(
        ["plan", "-input=false", *(["-destroy"] if destroy else []), *variables, f"-out={plan}"],
        RELEASE_STACK,
    )
    review = plan.with_suffix(".txt")
    review.write_text(terraform(["show", "-no-color", str(plan)], RELEASE_STACK, capture=True))
    print(f"PLAN: {review.relative_to(ROOT)} (review before applying)")
    return plan


def cmd_release_plan(_args):
    release_plan(prepare_release())


def cmd_release_up(args):
    plan = release_plan(prepare_release())
    if not confirm("Apply the release stack (ECR repositories and GitHub roles)?", args.yes):
        print("Not applied.")
        return
    terraform(["apply", "-input=false", str(plan)], RELEASE_STACK)
    plan.unlink(missing_ok=True)
    print("READY: release stack. Set the GitHub variables from `./project infra release-output`.")


def cmd_release_output(_args):
    prepare_release()
    print(terraform(["output", "-json"], RELEASE_STACK, capture=True))


def cmd_release_down(args):
    plan = release_plan(prepare_release(), destroy=True)
    print("Repositories that still hold images are not deleted (force_delete = false).")
    if not confirm("Destroy the release stack?", args.yes):
        print("Not destroyed.")
        return
    terraform(["apply", "-input=false", str(plan)], RELEASE_STACK)
    plan.unlink(missing_ok=True)


COMMANDS = {
    "check": cmd_check,
    "bootstrap": cmd_bootstrap,
    "plan": cmd_plan,
    "up": cmd_up,
    "down": cmd_down,
    "output": cmd_output,
    "rotate": cmd_rotate,
    "validate": cmd_validate,
    "release-plan": cmd_release_plan,
    "release-up": cmd_release_up,
    "release-output": cmd_release_output,
    "release-down": cmd_release_down,
}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="./project infra", description=__doc__)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--env", choices=ENVIRONMENTS, default="staging")
    parser.add_argument("--region", help="bootstrap: state region")
    parser.add_argument("--cost-owner", help="bootstrap: cost owner tag")
    parser.add_argument("--destroy", action="store_true", help="bootstrap: remove state storage")
    parser.add_argument(
        "--yes", action="store_true", help="apply a reviewed plan without prompting"
    )
    parser.add_argument(
        "--confirm-protected", metavar="ENV", help="down: confirm a retained environment"
    )
    parser.add_argument(
        "--secret",
        choices=("broker", "cache", "database", "all"),
        default="all",
        help="rotate: which credential to rotate now",
    )
    parser.add_argument(
        "--service",
        dest="services",
        action="append",
        choices=("broker", "cache", "database", "artifacts"),
        help="validate: limit the check to these services (repeatable)",
    )
    args = parser.parse_args(argv)
    try:
        COMMANDS[args.command](args)
    except InfraError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        print(f"ERROR: {' '.join(error.cmd[:3])} exited with {error.returncode}", file=sys.stderr)
        return error.returncode or 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
