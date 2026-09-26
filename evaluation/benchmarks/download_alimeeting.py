"""Download the two pinned AliMeeting4MUG archives without exposing a token.

The old-style ModelScope dataset keeps its data in OSS, outside the Git
repository.  ``MsDataset.load`` populates ModelScope's local cache; this script
copies only raw ZIPs that match the committed source-manifest.json exactly.
The default verify mode is offline and never reads the project .env file.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import hashlib
from importlib import metadata
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any
import zipfile


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
SOURCE_DIR = HERE / "source" / "alimeeting4mug"
ENV_FILE = PROJECT_ROOT / "ai_meeting" / "fastapi-app" / ".env"
DATASET_ID = "modelscope/Alimeeting4MUG"
DATASET_NAME = "Alimeeting4MUG"
NAMESPACE = "modelscope"
ARCHIVES = {"validation": "dev.zip", "test": "except_TS_test1.zip"}
METADATA_GIT_REVISION = "132ff7750d194458c52712e7094aecbb7c9ca4ae"
SDK_VERSION = "1.40.1"
# Changing this requires an explicit source re-pin, not an implicit redownload.
PINNED_MANIFEST_SHA256 = "dc50c8aad4cceaf378714af6e55d39c87f76bffe1bea94d228d80f9fe38e3fd7"


class SourceError(ValueError):
    """An archive or source pin did not meet the locked contract."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(
    source_dir: Path, *, expected_sha256: str = PINNED_MANIFEST_SHA256,
) -> dict[str, Any]:
    """Reject edited pins before reading a source archive or contacting the SDK."""
    manifest_path = source_dir / "source-manifest.json"
    raw = manifest_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise SourceError("source-manifest.json differs from the locked SHA-256")
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceError("Invalid source-manifest.json") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("dataset_id") != DATASET_ID
        or manifest.get("subset") != "default"
        or manifest.get("metadata_git_revision") != METADATA_GIT_REVISION
        or manifest.get("download_tool") != f"modelscope[datasets] {SDK_VERSION} MsDataset.load"
        or not isinstance(manifest.get("archives"), dict)
        or set(manifest["archives"]) != set(ARCHIVES.values())
    ):
        raise SourceError("source-manifest.json has unexpected dataset metadata")
    for name in ARCHIVES.values():
        record = manifest["archives"][name]
        expected_path = f"evaluation/benchmarks/source/alimeeting4mug/{name}"
        if (
            not isinstance(record, dict)
            or record.get("path") != expected_path
            or type(record.get("bytes")) is not int
            or record["bytes"] <= 0
            or not isinstance(record.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
            or record.get("zip_members") != [name.removesuffix(".zip") + ".csv"]
        ):
            raise SourceError(f"Invalid pinned archive entry: {name}")
    return manifest


def verify_archive(path: Path, record: dict[str, Any]) -> None:
    """Verify byte size, SHA-256, single expected ZIP member and CRC."""
    if path.is_symlink() or not path.is_file():
        raise SourceError(f"Missing or nonregular source archive: {path.name}")
    if path.stat().st_size != record["bytes"] or _sha256_file(path) != record["sha256"]:
        raise SourceError(f"Pinned source archive checksum or length mismatch: {path.name}")
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.namelist() != record["zip_members"] or archive.testzip() is not None:
                raise SourceError(f"Pinned source ZIP members or CRC mismatch: {path.name}")
    except zipfile.BadZipFile as exc:
        raise SourceError(f"Invalid pinned source ZIP: {path.name}") from exc


def verify_source(source_dir: Path, manifest: dict[str, Any]) -> list[str]:
    verified: list[str] = []
    for name in ARCHIVES.values():
        verify_archive(source_dir / name, manifest["archives"][name])
        verified.append(name)
    return verified


def _read_project_token(env_file: Path) -> str:
    """Parse one dotenv key as data; never execute shell syntax or print values."""
    try:
        lines = env_file.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise SourceError(f"Cannot read project env file: {env_file}") from exc
    values: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped.removeprefix("export ").strip()
        name, separator, raw = stripped.partition("=")
        if separator and name.strip() == "MODELSCOPE_API_KEY":
            value = raw.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if not value or any(character.isspace() for character in value):
                raise SourceError("MODELSCOPE_API_KEY in project env file is empty or malformed")
            values.append(value)
    if len(values) != 1:
        raise SourceError("Project env file must contain exactly one MODELSCOPE_API_KEY")
    return values[0]


def _private_cache_paths(source_dir: Path) -> tuple[Path, Path]:
    home = source_dir / ".modelscope-private"
    cache = source_dir / "cache"
    for path in (home, cache):
        if path.is_symlink():
            raise SourceError(f"Refusing symlinked ModelScope cache directory: {path.name}")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    return home, cache


def _matching_cache_archive(cache: Path, record: dict[str, Any]) -> Path | None:
    data_files = cache / "datasets" / NAMESPACE / DATASET_NAME / "master" / "data_files"
    if not data_files.is_dir() or data_files.is_symlink():
        return None
    for candidate in sorted(data_files.iterdir()):
        if (candidate.is_file() and not candidate.is_symlink()
                and candidate.stat().st_size == record["bytes"]
                and _sha256_file(candidate) == record["sha256"]):
            verify_archive(candidate, record)
            return candidate
    return None


def _sdk_download(split: str, token: str, home: Path, cache: Path) -> None:
    # Set cache paths before importing ModelScope; its OSS downloader may also
    # create credential/cache files. Both remain under this project source dir.
    os.environ["MODELSCOPE_HOME"] = str(home)
    os.environ["MODELSCOPE_CACHE"] = str(cache)
    try:
        installed_version = metadata.version("modelscope")
        if installed_version != SDK_VERSION:
            raise SourceError(
                f"Expected modelscope[datasets]=={SDK_VERSION}, found {installed_version}"
            )
        from modelscope.msdatasets import MsDataset
    except (ImportError, metadata.PackageNotFoundError) as exc:
        raise SourceError("Install modelscope[datasets]==1.40.1 before downloading") from exc
    # SDK exceptions/logs can contain request details. Deliberately discard
    # them; never echo a token-bearing URL, header or SDK traceback.
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            MsDataset.load(
                DATASET_NAME, namespace=NAMESPACE, subset_name="default",
                split=split, token=token,
            )
    except Exception as exc:
        raise SourceError(
            f"ModelScope download failed for {split} ({type(exc).__name__}); "
            "check the domestic-site token and dataset access"
        ) from None


def _copy_verified_without_overwrite(source: Path, destination: Path, record: dict[str, Any]) -> None:
    verify_archive(source, record)
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=destination.parent, prefix=f".{destination.name}.",
        suffix=".tmp", delete=False,
    ) as output:
        temporary = Path(output.name)
        try:
            with source.open("rb") as input_stream:
                shutil.copyfileobj(input_stream, output)
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        verify_archive(temporary, record)
        try:
            # Hard-link is atomic and fails if another process created dest.
            os.link(temporary, destination)
        except FileExistsError:
            verify_archive(destination, record)
    finally:
        temporary.unlink(missing_ok=True)


def download_source(source_dir: Path, env_file: Path, manifest: dict[str, Any]) -> list[str]:
    """Reuse pinned cache, and call the SDK only for truly missing archives."""
    missing: list[tuple[str, str]] = []
    for split, name in ARCHIVES.items():
        destination = source_dir / name
        if destination.exists() or destination.is_symlink():
            verify_archive(destination, manifest["archives"][name])
        else:
            missing.append((split, name))
    if not missing:
        return verify_source(source_dir, manifest)
    home, cache = _private_cache_paths(source_dir)
    token: str | None = None
    for split, name in missing:
        record = manifest["archives"][name]
        candidate = _matching_cache_archive(cache, record)
        if candidate is None:
            if token is None:
                token = _read_project_token(env_file)
            _sdk_download(split, token, home, cache)
            candidate = _matching_cache_archive(cache, record)
        if candidate is None:
            raise SourceError(
                f"SDK finished but pinned {name} was not found in the project cache; "
                "upstream data may have changed"
            )
        _copy_verified_without_overwrite(candidate, source_dir / name, record)
    return verify_source(source_dir, manifest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("verify", "download"), default="verify")
    parser.add_argument("--source-dir", type=Path, default=SOURCE_DIR)
    parser.add_argument("--env-file", type=Path, default=ENV_FILE)
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.source_dir)
        verified = (
            verify_source(args.source_dir, manifest) if args.mode == "verify"
            else download_source(args.source_dir, args.env_file, manifest)
        )
    except (OSError, SourceError) as exc:
        print(f"AliMeeting4MUG {args.mode} failed: {exc}", file=sys.stderr)
        return 1
    for name in verified:
        record = manifest["archives"][name]
        print(f"verified {name}: {record['bytes']} bytes, sha256 {record['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
