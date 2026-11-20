"""Inspect local submission directories and create deterministic source/package bundles."""

import io
import os
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path

from compiler_optimizer.shared.manifest import ContractError, parse_manifest, require, validate_path
from compiler_optimizer.shared.v1 import contracts_pb2 as pb

MAX_CONTENT_BYTES = 10 * 1024**2


def bundle(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, contents in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100444 << 16
            archive.writestr(info, contents)
    return output.getvalue()


@dataclass(frozen=True)
class SubmissionPackage:
    manifest: pb.SubmissionManifest
    files: dict[str, bytes]
    sources: dict[str, bytes]
    package_bundle: bytes
    source_bundle: bytes


def read_directory(directory: str | Path) -> dict[str, bytes]:
    root = Path(directory)
    require(not root.is_symlink() and root.is_dir(), "submission must be a real directory")
    files = {}
    size = 0
    try:

        def walk_error(error):
            raise error

        for current, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
            for name in [*dirs, *names]:
                path = Path(current) / name
                relative = path.relative_to(root).as_posix()
                validate_path(relative)
                try:
                    relative.encode("utf-8")
                except UnicodeError as error:
                    raise ContractError("submission paths must be UTF-8") from error
                mode = path.lstat().st_mode
                require(
                    stat.S_ISDIR(mode) or stat.S_ISREG(mode),
                    "symlinks/nonregular files are forbidden",
                )
                if stat.S_ISDIR(mode):
                    continue
                require(len(files) < 100, "submission exceeds 100 files")
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as source:
                    require(
                        stat.S_ISREG(os.fstat(source.fileno()).st_mode),
                        "file changed during ingestion",
                    )
                    contents = source.read(MAX_CONTENT_BYTES - size + 1)
                size += len(contents)
                require(size <= MAX_CONTENT_BYTES, "submission exceeds 10 MiB file contents")
                files[relative] = contents
    except OSError as error:
        raise ContractError("cannot read submission package") from error
    return files


def read_package(directory: str | Path) -> SubmissionPackage:
    return package_from_files(read_directory(directory))


def package_from_files(files: dict[str, bytes]) -> SubmissionPackage:
    require(
        len(files) <= 100 and sum(map(len, files.values())) <= MAX_CONTENT_BYTES,
        "submission exceeds package limits",
    )
    for name in files:
        validate_path(name)
    require("submission.json" in files, "submission.json is required")
    manifest = parse_manifest(files["submission.json"])
    required = set(manifest.sources)
    fixtures = set()
    for case in manifest.tests:
        fixtures.update((case.stdin, case.expected_stdout))
    require(required | fixtures <= files.keys(), "source or fixture file is missing")
    require(not required & fixtures, "source files cannot also be test fixtures")
    sources = {
        name: data
        for name, data in files.items()
        if name in required or (name.endswith((".h", ".hpp")) and name not in fixtures)
    }
    return SubmissionPackage(manifest, files, sources, bundle(files), bundle(sources))


def unbundle(data: bytes) -> dict[str, bytes]:
    """Read only bounded regular ZIP_STORED snapshots, without filesystem extraction."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            require(0 < len(entries) <= 100, "invalid snapshot file count")
            require(
                sum(entry.file_size for entry in entries) <= MAX_CONTENT_BYTES,
                "snapshot exceeds file-content limit",
            )
            files = {}
            for entry in entries:
                validate_path(entry.filename)
                require(
                    entry.filename not in files
                    and not entry.is_dir()
                    and entry.compress_type == zipfile.ZIP_STORED
                    and stat.S_ISREG(entry.external_attr >> 16),
                    "invalid snapshot entry",
                )
                files[entry.filename] = archive.read(entry)
            return files
    except (zipfile.BadZipFile, RuntimeError, OSError) as error:
        raise ContractError("invalid stored source/package snapshot") from error
