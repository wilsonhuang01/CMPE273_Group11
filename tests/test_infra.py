"""AWS environment configuration rules enforced outside Terraform (action Q)."""

import datetime
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("infra", ROOT / "tools/infra.py")
infra = importlib.util.module_from_spec(spec)
spec.loader.exec_module(infra)

LOCAL = {"cost_owner": "group11", "budget_alert_emails": ["alerts@example.com"]}


def declared_variables():
    text = "".join(p.read_text() for p in (ROOT / "infra/aws/environment").glob("*.tf"))
    return set(re.findall(r'^variable "([a-z_]+)"', text, re.M))


@pytest.fixture
def config(tmp_path):
    for environment in infra.ENVIRONMENTS:
        source = ROOT / f"infra/aws/config/{environment}.tfvars.json"
        (tmp_path / source.name).write_text(source.read_text())
    (tmp_path / "local.tfvars.json").write_text(json.dumps(LOCAL))
    return tmp_path


@pytest.mark.parametrize("environment", infra.ENVIRONMENTS)
def test_committed_settings_use_declared_variables_and_no_credentials(environment):
    settings = json.loads((ROOT / f"infra/aws/config/{environment}.tfvars.json").read_text())
    assert settings["environment"] == environment
    assert set(settings) <= declared_variables()
    assert not infra.LOCAL_KEYS & settings.keys()
    assert list(infra._secret_like_keys(settings)) == []


def test_committed_settings_respect_capacity_and_protection_rules():
    for environment in infra.ENVIRONMENTS:
        settings = json.loads((ROOT / f"infra/aws/config/{environment}.tfvars.json").read_text())
        capacity = settings["worker_capacity"]
        assert 0 <= capacity["min"] <= capacity["desired"] <= capacity["max"]
        assert capacity["max"] <= settings["max_worker_ceiling"]
        if environment == "production":
            assert settings["data_protection"] == "retain"
        else:
            assert settings["data_protection"] == "destroy"
            datetime.date.fromisoformat(settings["expires_on"])


def test_load_settings_merges_local_owner_and_recipients(config):
    settings, files = infra.load_settings("staging", config)
    assert settings["cost_owner"] == "group11"
    assert [f.name for f in files] == ["staging.tfvars.json", "local.tfvars.json"]


def test_environment_local_file_overrides_shared_local_file(config):
    (config / "dev.local.tfvars.json").write_text(json.dumps({"cost_owner": "dev-owner"}))
    settings, files = infra.load_settings("dev", config)
    assert settings["cost_owner"] == "dev-owner"
    assert files[-1].name == "dev.local.tfvars.json"


def test_missing_local_owner_is_rejected(config):
    (config / "local.tfvars.json").unlink()
    with pytest.raises(infra.InfraError, match="cost_owner"):
        infra.load_settings("staging", config)


@pytest.mark.parametrize(
    "local",
    [
        {**LOCAL, "rabbitmq_password": "hunter2"},
        {**LOCAL, "region": "us-east-1"},
    ],
)
def test_local_file_may_only_set_owner_and_recipients(config, local):
    (config / "local.tfvars.json").write_text(json.dumps(local))
    with pytest.raises(infra.InfraError, match="may only set"):
        infra.load_settings("staging", config)


def test_credential_like_keys_are_rejected_anywhere(config):
    settings = json.loads((config / "staging.tfvars.json").read_text())
    settings["service_capacity"]["agent_token"] = "abc"
    (config / "staging.tfvars.json").write_text(json.dumps(settings))
    with pytest.raises(infra.InfraError, match="service_capacity.agent_token"):
        infra.load_settings("staging", config)


def test_committed_file_cannot_hold_personal_settings(config):
    settings = json.loads((config / "dev.tfvars.json").read_text())
    settings["budget_alert_emails"] = ["someone@example.com"]
    (config / "dev.tfvars.json").write_text(json.dumps(settings))
    with pytest.raises(infra.InfraError, match="must not set"):
        infra.load_settings("dev", config)


def test_unknown_environment_is_rejected(config):
    with pytest.raises(infra.InfraError, match="unknown environment"):
        infra.load_settings("qa", config)


def test_expired_environment_warns_with_teardown_command():
    settings = {"environment": "staging", "expires_on": "2026-01-31"}
    warning = infra.expiry_warning(settings, today=datetime.date(2026, 2, 1))
    assert "./project infra down --env staging" in warning
    assert infra.expiry_warning(settings, today=datetime.date(2026, 1, 31)) is None
    assert infra.expiry_warning({"environment": "production"}) is None


def test_retained_environment_requires_repeating_its_name():
    production = {"environment": "production", "data_protection": "retain"}
    assert not infra.destroy_allowed(production, None)
    assert not infra.destroy_allowed(production, "staging")
    assert infra.destroy_allowed(production, "production")
    assert infra.destroy_allowed({"environment": "dev", "data_protection": "destroy"}, None)


def test_backend_state_is_isolated_per_environment():
    backend = {"bucket": "b-123456789012-us-west-2", "dynamodb_table": "t", "region": "us-west-2"}
    arguments = infra.backend_arguments(backend, "staging")
    assert "-backend-config=key=cmpe273-optimizer/staging/terraform.tfstate" in arguments
    assert "-backend-config=encrypt=true" in arguments
    assert arguments != infra.backend_arguments(backend, "dev")


def test_state_bucket_from_another_account_is_refused():
    backend = {"bucket": "cmpe273-optimizer-tfstate-123456789012-us-west-2"}
    infra.check_account(backend, "123456789012")
    with pytest.raises(infra.InfraError, match="another account"):
        infra.check_account(backend, "999999999999")


def test_missing_backend_points_to_bootstrap(tmp_path):
    with pytest.raises(infra.InfraError, match="bootstrap"):
        infra.load_backend(tmp_path)


class FakeSecrets:
    def __init__(self, existing):
        self.existing = set(existing)
        self.values = {}

    def describe_secret(self, SecretId):
        return {"VersionIdsToStages": {"v1": ["AWSCURRENT"]}} if SecretId in self.existing else {}

    def get_random_password(self, **options):
        assert options["PasswordLength"] >= 32 and options["ExcludePunctuation"]
        return {"RandomPassword": "r" * options["PasswordLength"]}

    def put_secret_value(self, SecretId, SecretString, ClientRequestToken):
        assert ClientRequestToken
        self.values[SecretId] = SecretString


def test_seeding_fills_only_empty_secrets_and_never_logs_values():
    client = FakeSecrets(existing={"p/staging/agent-api-token"})
    lines = []
    seeded = infra.seed_secrets(
        client, ["p/staging/agent-api-token", "p/staging/redis-auth-token"], log=lines.append
    )
    assert seeded == ["p/staging/redis-auth-token"]
    assert set(client.values) == {"p/staging/redis-auth-token"}
    assert not any(client.values["p/staging/redis-auth-token"] in line for line in lines)


def test_seeding_is_idempotent():
    client = FakeSecrets(existing=set())
    names = ["p/dev/a", "p/dev/b"]
    assert infra.seed_secrets(client, names, log=lambda _: None) == names
    client.existing |= set(names)
    assert infra.seed_secrets(client, names, log=lambda _: None) == []


def test_every_terraform_secret_is_consumed_and_every_consumer_secret_exists():
    main = (ROOT / "infra/aws/environment/main.tf").read_text()
    block = main[main.index("secrets = {") : main.index("# Least privilege")]
    declared = set(re.findall(r"^\s+([a-z-]+)\s+=", block, re.M))
    consumers = main[main.index("workload_secrets = {") : main.index("ecs_workloads")]
    used = set(re.findall(r'"([a-z-]+)"', consumers))
    assert declared == used


# --- Managed data services (action R, phase 4) -----------------------------------------


class RotatingSecrets(FakeSecrets):
    def __init__(self, existing=(), finish_after=2):
        super().__init__(existing)
        self.stages, self.tags, self.rotations = {}, [], []
        self.finish_after, self.polls = finish_after, 0

    def describe_secret(self, SecretId):
        self.polls += 1
        description = super().describe_secret(SecretId)
        if self.rotations and self.polls >= self.finish_after:
            description["VersionIdsToStages"] = {self.rotations[-1]: ["AWSCURRENT"]}
        description["Tags"] = list(self.tags)
        return description

    def get_secret_value(self, SecretId):
        return {"SecretString": self.values[SecretId]}

    def rotate_secret(self, SecretId, RotateImmediately):
        assert RotateImmediately
        self.rotations.append(f"v{len(self.rotations) + 1}")
        self.polls = 0
        return {"VersionId": self.rotations[-1]}

    def tag_resource(self, SecretId, Tags):
        self.tags.extend(Tags)


def test_broker_secret_starts_with_the_bootstrap_login_until_rotated():
    client = RotatingSecrets()
    assert infra.seed_broker_secret(
        client, "p/dev/rabbitmq-password", "optimizer", "boot", log=lambda _: None
    )
    assert json.loads(client.values["p/dev/rabbitmq-password"]) == {
        "username": "optimizer",
        "password": "boot",
    }
    client.existing.add("p/dev/rabbitmq-password")
    assert infra.seed_broker_secret(client, "p/dev/rabbitmq-password", "optimizer", "boot")
    client.values["p/dev/rabbitmq-password"] = json.dumps(
        {"username": "optimizer-alt", "password": "rotated"}
    )
    assert not infra.seed_broker_secret(client, "p/dev/rabbitmq-password", "optimizer", "boot")


def test_rotation_waits_for_the_new_version_and_times_out_with_a_hint():
    client = RotatingSecrets(finish_after=3)
    sleeps = []
    assert infra.rotate_and_wait(client, "s", sleep=sleeps.append, log=lambda _: None) == "v1"
    assert len(sleeps) == 2
    stuck = RotatingSecrets(finish_after=10**9)
    with pytest.raises(infra.InfraError, match="log group"):
        infra.rotate_and_wait(stuck, "s", timeout=0, sleep=lambda _: None)


class FakeCache:
    def __init__(self):
        self.modifications, self.status = [], ["modifying", "available"]

    def describe_replication_groups(self, ReplicationGroupId):
        status = self.status.pop(0) if len(self.status) > 1 else self.status[0]
        return {"ReplicationGroups": [{"Status": status}]}

    def modify_replication_group(self, **request):
        self.modifications.append((request["AuthTokenUpdateStrategy"], request["AuthToken"]))


def test_cache_auth_is_enforced_once_with_rotate_then_set():
    secrets, cache = RotatingSecrets(), FakeCache()
    secrets.values["p/dev/redis-auth-token"] = "t" * 48
    assert infra.enforce_cache_auth(
        secrets, cache, "p/dev/redis-auth-token", "g", sleep=lambda _: None, log=lambda _: None
    )
    assert cache.modifications == [("ROTATE", "t" * 48), ("SET", "t" * 48)]
    assert secrets.tags == [{"Key": "AuthEnforced", "Value": "true"}]
    assert not infra.enforce_cache_auth(
        secrets, cache, "p/dev/redis-auth-token", "g", log=lambda _: None
    )
    assert len(cache.modifications) == 2


def test_lambda_packages_are_byte_reproducible(tmp_path):
    first = infra.write_zip(tmp_path / "a.zip", {"b.py": b"2", "a.py": b"1"})
    second = infra.write_zip(tmp_path / "b.zip", {"a.py": b"1", "b.py": b"2"})
    assert first.read_bytes() == second.read_bytes()
    rotation = infra.build_rotation_package(tmp_path)
    import zipfile

    assert zipfile.ZipFile(rotation).namelist() == ["rotation.py"]
    assert infra.var_arguments([Path("x.json"), "-var=a=b"]) == ["-var-file=x.json", "-var=a=b"]


def test_terraform_never_receives_service_credentials():
    """RDS and rotation own every credential; only the broker's bootstrap password, which
    `infra up` rotates out of use, passes through Terraform state."""
    stack = ROOT / "infra/aws/environment"
    text = {path.name: path.read_text() for path in stack.glob("*.tf")}
    assigned = [
        (name, line.strip())
        for name, body in text.items()
        for line in body.splitlines()
        if re.match(r"\s*(password|auth_token|master_password)\s*=", line)
    ]
    assert assigned == [("messaging.tf", "password = random_password.broker_bootstrap.result")]
    assert "manage_master_user_password   = true" in text["database.tf"]
    assert "ignore_changes = [auth_token, auth_token_update_strategy]" in text["messaging.tf"]


def test_production_data_services_are_redundant_and_retained():
    production = json.loads((ROOT / "infra/aws/config/production.tfvars.json").read_text())
    assert production["database"]["multi_az"]
    assert production["database"]["backup_retention_days"] >= 7
    assert production["cache"]["nodes"] >= 2
    for environment in infra.ENVIRONMENTS:
        settings = json.loads((ROOT / f"infra/aws/config/{environment}.tfvars.json").read_text())
        assert 1 <= settings["rotation_days"] <= 365
        assert settings["database"]["rotation_days"] <= 90
