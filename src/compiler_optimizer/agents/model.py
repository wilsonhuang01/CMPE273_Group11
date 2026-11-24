"""Provider-neutral model calls with deadlines, bounded retries and per-task budgets.

Prompts carry submitted source as untrusted data. Replies are parsed as data only: they
never select commands, files outside the submission, or acceptance outcomes. Every call is
recorded (model identity, prompt/response hashes, usage, failures) before its reply is used.
"""

import argparse
import contextvars
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from compiler_optimizer.shared.manifest import ContractError, require

PROVIDERS = ("none", "bedrock", "replay")
MODEL_ID = re.compile(r"[A-Za-z0-9._:/-]{1,200}")
PROVIDER_NAME = re.compile(r"[a-z0-9-]{1,32}")
REGION = re.compile(r"[a-z]{2}(?:-[a-z]+)+-\d{1,2}")
MAX_PROMPT_BYTES = 256 * 1024
MAX_REPLY_BYTES = 512 * 1024
_TRANSIENT_CODES = frozenset(
    {
        "ThrottlingException",
        "TooManyRequestsException",
        "ServiceUnavailableException",
        "InternalServerException",
        "ModelNotReadyException",
        "ModelTimeoutException",
        "RequestTimeout",
    }
)
_TRANSIENT_ERRORS = frozenset(
    {"EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError", "ConnectionClosedError"}
)
_CREDENTIAL_ERRORS = frozenset(
    {
        "NoCredentialsError",
        "PartialCredentialsError",
        "NoRegionError",
        "TokenRetrievalError",
        "SSOTokenLoadError",
        "UnauthorizedSSOTokenError",
        "ProfileNotFound",
    }
)


class ModelUnavailable(Exception):
    """No provider, credentials, network access or recorded reply; the call is not retried."""


class TransientModelError(ModelUnavailable):
    """Throttling or a retryable provider fault."""


class ModelBudgetExceeded(Exception):
    """The task's model call or token budget is spent."""


class ModelOutputError(ContractError):
    """The model replied, but not with the structured output its role requires."""


@dataclass(frozen=True)
class ModelReply:
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str


class ModelProvider(Protocol):
    name: str

    def complete(self, model_id, system, messages, *, max_tokens, temperature, timeout):
        """Return a ModelReply; messages is a list of {"role", "text"} turns."""
        ...

    def check(self):
        """Raise ModelUnavailable when calls cannot succeed (no network call required)."""
        ...


def _bounded(env, key, default, low, high, kind=float):
    value = kind(env.get(key, default))
    if not (math.isfinite(value) and low <= value <= high):
        raise ValueError(f"{key} must be {low}–{high}")
    return value


@dataclass(frozen=True)
class ModelConfig:
    provider: str = "none"
    model_id: str = ""
    region: str = ""
    timeout_seconds: float = 60.0
    max_retries: int = 2
    max_repairs: int = 1
    max_output_tokens: int = 4096
    temperature: float = 0.0
    max_calls_per_task: int = 12
    max_tokens_per_task: int = 200_000
    replay_directory: Path | None = None
    record_directory: Path | None = None

    @property
    def enabled(self):
        return self.provider != "none"

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        provider = env.get("MODEL_PROVIDER", "none")
        if provider not in PROVIDERS:
            raise ValueError("MODEL_PROVIDER must be none, bedrock or replay")
        model_id = env.get("MODEL_ID", "")
        region = env.get("MODEL_REGION") or env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION")
        config = cls(
            provider=provider,
            model_id=model_id,
            region=region or "",
            timeout_seconds=_bounded(env, "MODEL_TIMEOUT_SECONDS", 60, 1, 115),
            max_retries=_bounded(env, "MODEL_MAX_RETRIES", 2, 0, 5, int),
            max_repairs=_bounded(env, "MODEL_MAX_REPAIRS", 1, 0, 3, int),
            max_output_tokens=_bounded(env, "MODEL_MAX_OUTPUT_TOKENS", 4096, 256, 16384, int),
            temperature=_bounded(env, "MODEL_TEMPERATURE", 0, 0, 1),
            max_calls_per_task=_bounded(env, "MODEL_MAX_CALLS_PER_TASK", 12, 1, 100, int),
            max_tokens_per_task=_bounded(
                env, "MODEL_MAX_TOKENS_PER_TASK", 200_000, 1000, 5_000_000, int
            ),
            replay_directory=Path(env["MODEL_REPLAY_DIRECTORY"])
            if env.get("MODEL_REPLAY_DIRECTORY")
            else None,
            record_directory=Path(env["MODEL_RECORD_DIRECTORY"])
            if env.get("MODEL_RECORD_DIRECTORY")
            else None,
        )
        if config.enabled and not MODEL_ID.fullmatch(model_id):
            raise ValueError("MODEL_ID is required when MODEL_PROVIDER is set")
        if provider == "bedrock" and not REGION.fullmatch(config.region):
            raise ValueError("Bedrock requires MODEL_REGION or AWS_REGION")
        if provider == "replay" and config.replay_directory is None:
            raise ValueError("MODEL_PROVIDER=replay requires MODEL_REPLAY_DIRECTORY")
        return config


def backend_name(provider_name, model_id):
    require(
        PROVIDER_NAME.fullmatch(provider_name) and MODEL_ID.fullmatch(model_id),
        "invalid model identity",
    )
    return f"model-v1:{provider_name}:{model_id}"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def prompt_record(model_id, system, messages):
    return canonical(
        {"schema_version": 1, "model_id": model_id, "system": system, "messages": messages}
    )


def _error_code(error):
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code", ""))
    return ""


def _error_message(error):
    """AWS's own explanation (e.g. a missing Marketplace subscription), bounded and one line."""
    response = getattr(error, "response", None)
    message = response.get("Error", {}).get("Message", "") if isinstance(response, dict) else ""
    return " ".join(str(message).split())[:160]


class BedrockProvider:
    """Amazon Bedrock Converse API; credentials come from the standard AWS chain."""

    name = "bedrock"

    def __init__(self, region, *, client=None):
        self.region, self._fixed, self._clients = region, client, {}

    def _boto3(self):
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise ModelUnavailable(
                "boto3 is not installed; install the bedrock extra: pip install -e '.[bedrock]'"
            ) from None
        return boto3, Config

    def _client(self, timeout):
        if self._fixed is not None:
            return self._fixed
        seconds = max(1, math.ceil(timeout))
        if seconds not in self._clients:
            boto3, Config = self._boto3()
            self._clients[seconds] = boto3.session.Session().client(
                "bedrock-runtime",
                region_name=self.region,
                config=Config(
                    connect_timeout=5,
                    read_timeout=seconds,
                    retries={"max_attempts": 1, "mode": "standard"},  # ModelClient retries.
                ),
            )
        return self._clients[seconds]

    def check(self):
        if self._fixed is not None:
            return
        boto3, _ = self._boto3()
        try:
            credentials = boto3.session.Session().get_credentials()
        except Exception as error:
            raise ModelUnavailable("AWS credentials unavailable: " + type(error).__name__) from None
        if credentials is None:
            raise ModelUnavailable("AWS credentials unavailable")

    def complete(self, model_id, system, messages, *, max_tokens, temperature, timeout):
        try:
            response = self._client(timeout).converse(
                modelId=model_id,
                system=[{"text": system}],
                messages=[{"role": m["role"], "content": [{"text": m["text"]}]} for m in messages],
                inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
            )
        except ModelUnavailable:
            raise
        except Exception as error:
            name, code = type(error).__name__, _error_code(error)
            if code in _TRANSIENT_CODES or name in _TRANSIENT_ERRORS:
                raise TransientModelError(f"Bedrock {code or name}") from None
            if name in _CREDENTIAL_ERRORS:
                raise ModelUnavailable("AWS credentials unavailable: " + name) from None
            detail = _error_message(error)
            raise ModelUnavailable(
                f"Bedrock call failed: {code or name}" + (f": {detail}" if detail else "")
            ) from None
        try:
            content = response["output"]["message"]["content"]
            text = "".join(part["text"] for part in content if "text" in part)
            usage = response.get("usage", {})
            return ModelReply(
                text=text,
                input_tokens=int(usage.get("inputTokens", 0)),
                output_tokens=int(usage.get("outputTokens", 0)),
                stop_reason=str(response.get("stopReason", "")),
            )
        except (KeyError, TypeError, ValueError):
            raise ModelOutputError("Bedrock returned an unexpected response shape") from None


class ReplayProvider:
    """Serve recorded replies keyed by prompt hash; never contacts a network service."""

    name = "replay"

    def __init__(self, directory):
        self.directory = Path(directory)

    def check(self):
        if not self.directory.is_dir():
            raise ModelUnavailable("model replay directory does not exist")

    def complete(self, model_id, system, messages, *, max_tokens, temperature, timeout):
        key = hashlib.sha256(prompt_record(model_id, system, messages)).hexdigest()
        path = self.directory / (key + ".json")
        try:
            with path.open("rb") as stream:
                data = stream.read(MAX_REPLY_BYTES + 1)
        except FileNotFoundError:
            raise ModelUnavailable("no recorded model reply for prompt " + key) from None
        require(len(data) <= MAX_REPLY_BYTES, "recorded model reply is too large")
        value = json.loads(data)
        require(
            isinstance(value, dict)
            and value.get("schema_version") == 1
            and isinstance(value.get("text"), str),
            "invalid recorded model reply",
        )
        return ModelReply(
            value["text"],
            int(value.get("input_tokens", 0)),
            int(value.get("output_tokens", 0)),
            str(value.get("stop_reason", "end_turn")),
        )


class RecordingProvider:
    """Write each successful reply as a replay fixture for reproducible tests."""

    def __init__(self, inner, directory):
        self.inner, self.directory, self.name = inner, Path(directory), inner.name

    def check(self):
        self.inner.check()

    def complete(self, model_id, system, messages, **options):
        reply = self.inner.complete(model_id, system, messages, **options)
        key = hashlib.sha256(prompt_record(model_id, system, messages)).hexdigest()
        self.directory.mkdir(parents=True, exist_ok=True)
        record = {
            "schema_version": 1,
            "text": reply.text,
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
            "stop_reason": reply.stop_reason,
        }
        with tempfile.NamedTemporaryFile("wb", dir=self.directory, delete=False) as stream:
            stream.write(json.dumps(record, sort_keys=True, indent=2).encode())
        os.replace(stream.name, self.directory / (key + ".json"))
        return reply


def provider_for(config):
    if config.provider == "bedrock":
        provider = BedrockProvider(config.region)
    elif config.provider == "replay":
        provider = ReplayProvider(config.replay_directory)
    else:
        raise ModelUnavailable("no model provider configured")
    if config.record_directory is not None:
        provider = RecordingProvider(provider, config.record_directory)
    return provider


_DEADLINE = contextvars.ContextVar("model_call_deadline", default=None)


@contextmanager
def call_deadline(seconds):
    """Bound model calls made by this thread to the enclosing RPC deadline."""
    token = _DEADLINE.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def _remaining():
    deadline = _DEADLINE.get()
    return math.inf if deadline is None else deadline - time.monotonic()


class ModelClient:
    """One role's view of a provider: budgets, deadlines, retries and durable call records."""

    def __init__(self, config, provider, ledger, role_name):
        require(MODEL_ID.fullmatch(config.model_id), "model client requires a model ID")
        self.config, self.provider, self.ledger, self.role = config, provider, ledger, role_name
        self.backend_name = backend_name(provider.name, config.model_id)

    def complete(self, task_id, purpose, system, messages):
        config = self.config
        prompt = prompt_record(config.model_id, system, messages)
        require(len(prompt) <= MAX_PROMPT_BYTES, "model prompt exceeds 256 KiB")
        for attempt in range(config.max_retries + 1):
            timeout = min(config.timeout_seconds, _remaining() - 0.5)
            if timeout < 1:
                raise TimeoutError("agent deadline leaves no time for a model call")
            call = None
            if self.ledger is not None:
                call = self.ledger.reserve_model_call(
                    task_id,
                    self.role,
                    purpose,
                    self.backend_name,
                    prompt,
                    max_calls=config.max_calls_per_task,
                    max_tokens=config.max_tokens_per_task,
                )
            started = time.monotonic()

            def finish(status, reply=None, error=None, call=call, started=started):
                if call is not None:
                    self.ledger.finish_model_call(
                        call,
                        status=status,
                        reply=reply,
                        error=error,
                        latency_ms=int((time.monotonic() - started) * 1000),
                    )

            try:
                reply = self.provider.complete(
                    config.model_id,
                    system,
                    messages,
                    max_tokens=config.max_output_tokens,
                    temperature=config.temperature,
                    timeout=timeout,
                )
            except TransientModelError as error:
                finish("failed", error=str(error))
                pause = min(0.5 * 2**attempt, 4.0)
                if attempt == config.max_retries or _remaining() - pause < 2:
                    raise
                time.sleep(pause)
                continue
            except BaseException as error:
                finish("failed", error=str(error) or type(error).__name__)
                raise
            if len(reply.text.encode()) > MAX_REPLY_BYTES:
                finish("failed", reply=reply, error="reply too large")
                raise ModelOutputError("model reply exceeds 512 KiB")
            if reply.stop_reason == "max_tokens":
                finish("failed", reply=reply, error="reply truncated")
                raise ModelOutputError("model reply was truncated at the output token limit")
            finish("completed", reply=reply)
            return reply
        raise AssertionError("unreachable")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m compiler_optimizer.agents.model",
        description="Check the configured model provider with one small live call.",
    )
    parser.add_argument("command", choices=["check"])
    parser.add_argument("--prompt", default="Reply with the single word: ready")
    args = parser.parse_args(argv)
    try:
        config = ModelConfig.from_env()
        require(config.enabled, "set MODEL_PROVIDER (and MODEL_ID, MODEL_REGION) first")
        provider = provider_for(config)
        provider.check()
        started = time.monotonic()
        reply = ModelClient(config, provider, None, "check").complete(
            "check",
            "check",
            "You are a connectivity check.",
            [{"role": "user", "text": args.prompt}],
        )
    except (ModelUnavailable, ContractError, ValueError) as error:
        print(f"model unavailable: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "backend": backend_name(provider.name, config.model_id),
                "latency_ms": int((time.monotonic() - started) * 1000),
                "input_tokens": reply.input_tokens,
                "output_tokens": reply.output_tokens,
                "reply": reply.text,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
