"""Redacted release provenance and a clean source-archive reproduction gate."""

import argparse
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def usage(root):
    """Read existing agent evidence without taking ownership or recovering operations."""
    database = Path(root).resolve() / "agent.sqlite3"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return [
            dict(r)
            for r in db.execute(
                "SELECT call_id,role,backend,prompt_sha256,response_sha256,state,"
                "input_tokens,output_tokens,latency_ms FROM model_calls ORDER BY call_id"
            )
        ]


def provenance(root=ROOT, agent_states=(), images=()):
    root = Path(root)
    files = {}
    for directory in ("src", "docker", "tools", "infra"):
        for path in sorted((root / directory).rglob("*")):
            if (
                path.is_file()
                and not path.is_symlink()
                and not any(
                    part.startswith(".") or part == "__pycache__" or part.endswith(".egg-info")
                    for part in path.relative_to(root).parts
                )
                and path.suffix in {".py", ".sql", ".proto", ".json", ".Dockerfile", ".tf"}
            ):
                files[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    configuration = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in (
            "compose.yaml",
            "Dockerfile",
            "pyproject.toml",
            "constraints.txt",
            ".env.example",
        )
        if (root / name).exists()
    }
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False
    )
    # No environment values, prompts, completions, errors, endpoints or credentials.
    return {
        "schema_version": 1,
        "validation_domain": "local_process_and_network; not physical_multi_host",
        "source_revision": revision.stdout.strip() if revision.returncode == 0 else None,
        "source_sha256": files,
        "configuration_sha256": configuration,
        "image_references": [
            line.strip()
            for name in (
                "compose.yaml",
                "Dockerfile",
                "docker/compiler.Dockerfile",
                "docker/riscv.Dockerfile",
            )
            if (root / name).exists()
            for line in (root / name).read_text().splitlines()
            if line.strip().startswith(("image:", "FROM "))
        ],
        "execution_images": {
            image: json.loads(
                subprocess.check_output(
                    ["docker", "image", "inspect", image, "--format", "{{json .Id}}"], text=True
                )
            )
            for image in images
        },
        "python": platform.python_version(),
        "machine": platform.machine(),
        "dependencies": {
            d.metadata["Name"]: d.version
            for d in importlib.metadata.distributions()
            if d.metadata["Name"]
        },
        "model_usage": [usage(state) for state in agent_states],
    }


def clean_check(archive):
    """Exercise a packaged tree in isolation, without .env, venv, build or local state."""
    with tempfile.TemporaryDirectory(prefix="optimizer-clean-") as temporary:
        with tarfile.open(archive) as source:
            # Release archives are local inputs; data filtering also rejects unsafe links/paths.
            source.extractall(temporary, filter="data")
        root = Path(temporary) / "project"
        if (root / ".env").exists() or (root / ".venv").exists():
            raise ValueError("release includes ignored local setup")
        python = Path(temporary) / "venv/bin/python"
        subprocess.run([sys.executable, "-m", "venv", str(python.parent.parent)], check=True)
        subprocess.run(
            [str(python), "-m", "pip", "install", "-c", "constraints.txt", "-e", ".[dev]"],
            cwd=root,
            check=True,
        )
        for arguments in (
            [
                str(python.parent / "cmake"),
                "-S",
                ".",
                "-B",
                "build",
                "-DCMAKE_BUILD_TYPE=Release",
                f"-DPython3_EXECUTABLE={python}",
            ],
            [str(python.parent / "cmake"), "--build", "build", "--parallel"],
        ):
            subprocess.run(arguments, cwd=root, check=True)
        for args in (
            ["tools/generate_contracts.py", "--check"],
            ["-m", "pytest", "-q", "-m", "not docker"],
            ["-m", "build"],
            ["tools/project.py", "package", "--output", "dist/clean-release.tar.gz"],
        ):
            subprocess.run(
                [str(python), *args],
                cwd=root,
                check=True,
                env={**os.environ, "PIP_CONSTRAINT": str(root / "constraints.txt")},
            )


def add_provenance(archive, root=ROOT, *, agent_states=(), images=()):
    data = json.dumps(
        provenance(root, agent_states=agent_states, images=images), sort_keys=True, indent=2
    ).encode()
    info = tarfile.TarInfo("provenance.json")
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", type=Path, help="install and test a source release in a fresh venv"
    )
    parser.add_argument("--agent-state", action="append", default=[])
    parser.add_argument("--image", action="append", default=[])
    args = parser.parse_args()
    if args.check:
        clean_check(args.check)
    else:
        print(
            json.dumps(
                provenance(agent_states=args.agent_state, images=args.image),
                sort_keys=True,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
