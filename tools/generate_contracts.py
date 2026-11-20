"""Regenerate checked-in Python/gRPC contracts using the pinned dev toolchain."""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def generate(output: Path) -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{ROOT / 'src'}",
            f"--python_out={output}",
            f"--pyi_out={output}",
            f"--grpc_python_out={output}",
            str(ROOT / "src/compiler_optimizer/shared/v1/contracts.proto"),
        ],
        check=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if checked-in stubs are stale")
    args = parser.parse_args()
    if args.check:
        with tempfile.TemporaryDirectory(prefix="compiler-contracts-") as temp:
            output = Path(temp)
            generate(output)
            for generated in output.rglob("*"):
                if generated.is_file():
                    committed = ROOT / "src" / generated.relative_to(output)
                    if not committed.is_file() or generated.read_bytes() != committed.read_bytes():
                        parser.exit(1, f"Stale generated contract: {committed}\n")
        print("Generated contracts are up to date.")
    else:
        generate(ROOT / "src")
