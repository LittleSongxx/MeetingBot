"""Offline tests for pinned ModelScope source acquisition."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from benchmarks import download_alimeeting as download


class AliMeetingDownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source_dir = self.root / "source"
        self.source_dir.mkdir()
        self.cache = self.source_dir / "cache"
        self.archive_bytes: dict[str, bytes] = {}
        archives = {}
        for name in download.ARCHIVES.values():
            path = self.root / name
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(name.removesuffix(".zip") + ".csv", "idx\tcontent\n")
            raw = path.read_bytes()
            self.archive_bytes[name] = raw
            archives[name] = {
                "path": f"evaluation/benchmarks/source/alimeeting4mug/{name}",
                "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                "zip_members": [name.removesuffix(".zip") + ".csv"],
            }
        self.manifest = {
            "dataset_id": download.DATASET_ID,
            "subset": "default",
            "metadata_git_revision": download.METADATA_GIT_REVISION,
            "download_tool": f"modelscope[datasets] {download.SDK_VERSION} MsDataset.load",
            "archives": archives,
        }
        self.manifest_path = self.source_dir / "source-manifest.json"
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.manifest_sha = hashlib.sha256(self.manifest_path.read_bytes()).hexdigest()

    def _cache_archive(self, name: str) -> None:
        data_files = (self.cache / "datasets" / "modelscope" /
                      "Alimeeting4MUG" / "master" / "data_files")
        data_files.mkdir(parents=True, exist_ok=True)
        (data_files / hashlib.sha256(self.archive_bytes[name]).hexdigest()[:20]).write_bytes(
            self.archive_bytes[name]
        )

    def test_source_manifest_digest_is_locked(self) -> None:
        self.assertEqual(download.load_manifest(self.source_dir, expected_sha256=self.manifest_sha),
                         self.manifest)
        self.manifest_path.write_text(json.dumps({**self.manifest, "subset": "changed"}),
                                      encoding="utf-8")
        with self.assertRaisesRegex(download.SourceError, "locked SHA-256"):
            download.load_manifest(self.source_dir, expected_sha256=self.manifest_sha)

    def test_verify_rejects_changed_source_without_modifying_it(self) -> None:
        for name, raw in self.archive_bytes.items():
            (self.source_dir / name).write_bytes(raw)
        self.assertEqual(download.verify_source(self.source_dir, self.manifest),
                         list(download.ARCHIVES.values()))
        bad_path = self.source_dir / "dev.zip"
        bad_path.write_bytes(self.archive_bytes["dev.zip"] + b"changed")
        before = bad_path.read_bytes()
        with self.assertRaisesRegex(download.SourceError, "checksum"):
            download.download_source(self.source_dir, self.root / "missing.env", self.manifest)
        self.assertEqual(bad_path.read_bytes(), before)

    def test_reuses_verified_cache_without_reading_token_or_calling_sdk(self) -> None:
        for name in download.ARCHIVES.values():
            self._cache_archive(name)
        with patch.object(download, "_read_project_token", side_effect=AssertionError("read token")), \
             patch.object(download, "_sdk_download", side_effect=AssertionError("called SDK")):
            verified = download.download_source(self.source_dir, self.root / "missing.env",
                                                self.manifest)
        self.assertEqual(verified, list(download.ARCHIVES.values()))
        for name in verified:
            self.assertEqual((self.source_dir / name).read_bytes(), self.archive_bytes[name])

    def test_download_uses_env_token_only_when_needed(self) -> None:
        env_file = self.root / ".env"
        env_file.write_text("OTHER_KEY=ignored\nMODELSCOPE_API_KEY='secret-test-token'\n",
                            encoding="utf-8")
        called: list[tuple[str, str]] = []

        def fake_sdk(split: str, token: str, home: Path, cache: Path) -> None:
            called.append((split, token))
            self._cache_archive(download.ARCHIVES[split])

        with patch.object(download, "_sdk_download", side_effect=fake_sdk):
            download.download_source(self.source_dir, env_file, self.manifest)
        self.assertEqual(called, [("validation", "secret-test-token"),
                                  ("test", "secret-test-token")])
        self.assertEqual(download.verify_source(self.source_dir, self.manifest),
                         list(download.ARCHIVES.values()))

    def test_duplicate_env_token_and_wrong_zip_member_fail_closed(self) -> None:
        env_file = self.root / ".env"
        env_file.write_text("MODELSCOPE_API_KEY=first\nMODELSCOPE_API_KEY=second\n",
                            encoding="utf-8")
        with self.assertRaisesRegex(download.SourceError, "exactly one"):
            download._read_project_token(env_file)
        wrong_zip = self.source_dir / "dev.zip"
        with zipfile.ZipFile(wrong_zip, "w") as archive:
            archive.writestr("wrong.csv", "idx\tcontent\n")
        altered = dict(self.manifest["archives"]["dev.zip"])
        altered["bytes"] = wrong_zip.stat().st_size
        altered["sha256"] = hashlib.sha256(wrong_zip.read_bytes()).hexdigest()
        with self.assertRaisesRegex(download.SourceError, "members"):
            download.verify_archive(wrong_zip, altered)


if __name__ == "__main__":
    unittest.main()
