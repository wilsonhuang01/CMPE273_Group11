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
