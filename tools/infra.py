"""AWS environment provisioning for action Q: `./project infra <command> --env <name>`.

Terraform owns resources; this wrapper adds the rules Terraform cannot express:
committed settings never contain credentials, plans are saved for review before
apply, secrets are seeded in memory after apply, and protected environments
need an explicit confirmation before teardown.
"""

import argparse
import datetime
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra" / "aws"
STACK = INFRA / "environment"
BOOTSTRAP = INFRA / "bootstrap"
CONFIG = INFRA / "config"
PLANS = ROOT / "var" / "infra"
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
    return [f"-var-file={path}" for path in files]


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
    for directory in (BOOTSTRAP, STACK):
        terraform(["fmt", "-check", "-recursive", "-diff"], directory)
        terraform(["init", "-backend=false", "-input=false"], directory)
        terraform(["validate"], directory)
    for environment in ENVIRONMENTS:
        settings = _read_json(CONFIG / f"{environment}.tfvars.json")
        if settings.get("environment") != environment:
            raise InfraError(f"{environment}.tfvars.json has the wrong environment")
        if found := sorted(_secret_like_keys(settings)):
            raise InfraError(f"{environment}.tfvars.json contains credential-like keys {found}")
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


def prepare(environment):
    settings, files = load_settings(environment)
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
    names = json.loads(terraform(["output", "-json", "secret_names"], STACK, capture=True))
    seed_secrets(session.client("secretsmanager"), names)
    print(f"READY: {args.env} infrastructure. References: ./project infra output --env {args.env}")


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
    prepare(args.env)
    print(terraform(["output", "-json", "environment_config"], STACK, capture=True))


COMMANDS = {
    "check": cmd_check,
    "bootstrap": cmd_bootstrap,
    "plan": cmd_plan,
    "up": cmd_up,
    "down": cmd_down,
    "output": cmd_output,
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
