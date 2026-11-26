"""One-command local startup, evaluation, demonstration and curated release packaging."""

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def environment():
    env = dict(os.environ)
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError(".env must contain literal KEY=VALUE assignments")
            tokens = shlex.split(value, comments=True)
            if len(tokens) > 1:
                raise ValueError("quote values containing spaces in .env")
            env.setdefault(key, tokens[0] if tokens else "")
    for published, host in (
        ("RABBITMQ_AMQP_PORT", "RABBITMQ_PORT"),
        ("REDIS_HOST_PORT", "REDIS_PORT"),
    ):
        if published in env:
            env.setdefault(host, env[published])
    return env


def command(arguments, env):
    subprocess.run(arguments, cwd=ROOT, env=env, check=True)


def startup(env):
    command(["docker", "compose", "config", "--quiet"], env)
    command(
        ["docker", "compose", "up", "--detach", "--build", "--wait", "--wait-timeout", "240"], env
    )
    from compiler_optimizer.evaluation.__main__ import native_target

    target = native_target()
    profile, service = (
        ("native-arm", "compiler-arm") if target == "linux-aarch64" else ("toolchain", "compiler")
    )
    command(["docker", "compose", "--profile", profile, "build", service], env)
    command([sys.executable, "tools/check_stack.py"], env)
    command([sys.executable, "tools/check_observability.py"], env)
    print(
        "READY: native CPU toolchain, RabbitMQ, Redis, application APIs, Prometheus and Grafana",
        flush=True,
    )


def package(destination, evidence, *, agent_states=(), images=()):
    """Bundle an explicit source allowlist, never credentials, git state or virtualenvs."""
    destination = Path(destination).absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError("package already exists; refusing to overwrite a release")
    allowed = [
        "project",
        "README.md",
        "pyproject.toml",
        "constraints.txt",
        "MANIFEST.in",
        "Dockerfile",
        "compose.yaml",
        ".env.example",
        ".dockerignore",
        ".clang-format",
        ".editorconfig",
        "CMakeLists.txt",
        "src",
        "docker",
        "docs",
        "infra",
        "backlog",
        "examples",
        "benchmarks",
        "monitoring",
        "tools",
        "tests",
        ".github",
    ]

    def safe(info):
        if any(
            part in ("__pycache__", ".pytest_cache", ".ruff_cache", ".git", ".aws", ".venv")
            or part in (".terraform", "backend.json")
            or part.endswith((".tfstate", ".tfplan", "local.tfvars.json"))
            or ".tfstate." in part
            or (part.startswith(".env") and part != ".env.example")
            or part.endswith(".egg-info")
            for part in Path(info.name).parts
        ) or info.name.endswith((".pyc", ".pyo")):
            return None
        if info.issym():
            raise ValueError("release source must not contain symlinks")
        return info

    prepared = []
    for item in evidence:
        path = Path(item).resolve()
        if not path.is_relative_to((ROOT / "var").resolve()) or not path.is_dir():
            raise ValueError("evidence must be a generated directory under project/var")
        report_path = path / ("report.json" if (path / "report.json").exists() else "demo.json")
        if json.loads(report_path.read_text())["status"] not in ("complete", "passed"):
            raise ValueError("release evidence is incomplete or failed")
        if path.name in [p.name for p in prepared]:
            raise ValueError("evidence directory names must be unique")
        prepared.append(path)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        from reproduction import add_provenance

        with tarfile.open(temporary, "x:gz") as archive:
            add_provenance(archive, ROOT, agent_states=agent_states, images=images)
            for name in allowed:
                path = ROOT / name
                if path.exists():
                    archive.add(path, arcname="project/" + name, filter=safe)
            for path in sorted((ROOT / "dist").glob("*")):
                if path.name.endswith((".whl", ".tar.gz")) and path.name.startswith(
                    "distributed_compiler_optimizer-"
                ):
                    archive.add(path, arcname="distributions/" + path.name, filter=safe)
            for path in prepared:
                archive.add(path, arcname="evidence/" + path.name, filter=safe)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_name(destination.name + ".sha256").write_text(
        digest + "  " + destination.name + "\n"
    )
    return destination


def main():
    if sys.argv[1:2] == ["infra"]:  # AWS environments (action Q); see docs/aws-infrastructure.md
        env = environment()
        env["PIP_CONSTRAINT"] = str(ROOT / "constraints.txt")
        try:
            import boto3  # noqa: F401
        except ImportError:
            command([sys.executable, "-m", "pip", "install", "-e", ".[aws]"], env)
        os.execvpe(
            sys.executable, [sys.executable, str(ROOT / "tools/infra.py"), *sys.argv[2:]], env
        )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "up",
            "down",
            "status",
            "serve",
            "scheduler",
            "worker",
            "demo",
            "evaluate",
            "test",
            "package",
        ),
    )
    parser.add_argument(
        "--no-start", action="store_true", help="Reuse a running stack and built native image"
    )
    parser.add_argument("--output")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--evidence", action="append", default=[])
    parser.add_argument("--agent-state", action="append", default=[])
    parser.add_argument("--image", action="append", default=[])
    args = parser.parse_args()
    env = environment()
    env["PIP_CONSTRAINT"] = str(ROOT / "constraints.txt")
    os.environ.update(env)
    if args.command in ("up", "serve", "demo", "evaluate") and not args.no_start:
        startup(env)
    if args.command in ("demo", "evaluate"):
        options = [sys.executable, "-m", "compiler_optimizer.evaluation", args.command]
        if args.output:
            options += ["--output", args.output]
        if args.command == "evaluate":
            options += ["--trials", str(args.trials)]
        command(options, env)
    elif args.command in ("serve", "scheduler", "worker"):
        env.setdefault("STATE_DIRECTORY", "var/service")
        env.setdefault("GRPC_PORT", "50052")
        env.setdefault(
            "HEALTH_PORT", {"serve": "18083", "scheduler": "18084", "worker": "18085"}[args.command]
        )
        env.setdefault("HEALTH_HOST", "127.0.0.1")
        role = "coordinator" if args.command == "serve" else args.command
        env["EXECUTION_ENABLED"] = "true" if role == "coordinator" else "false"
        env["TRANSFER_ENABLED"] = "true" if role == "coordinator" else "false"
        os.execvpe(
            sys.executable,
            [sys.executable, "-m", "compiler_optimizer.runtime", "--role", role],
            env,
        )
    elif args.command == "down":
        command(["docker", "compose", "down"], env)  # Volumes and evidence are preserved.
    elif args.command == "status":
        command([sys.executable, "tools/check_stack.py"], env)
        command([sys.executable, "tools/check_observability.py"], env)
    elif args.command in ("test", "package"):
        command(
            [sys.executable, "-m", "pip", "install", "-c", "constraints.txt", "-e", ".[dev]"], env
        )
        if args.command == "test":
            command([sys.executable, "-m", "pytest", "-q", "-m", "not docker"], env)
        else:
            command([sys.executable, "-m", "build"], env)
            path = package(
                args.output or "dist/compiler-optimizer-demo.tar.gz",
                args.evidence,
                agent_states=args.agent_state,
                images=args.image,
            )
            print("PACKAGED: " + str(path))


if __name__ == "__main__":
    main()
