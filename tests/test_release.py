"""A tag resolves to reproducible, repository-specific Docker deployment files."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.prepare_release import compose_text, release_info, write_assets


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "pyproject.toml"
        self.project.write_text('[project]\nversion = "0.4.0"\n', encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_tag_and_repository_resolve_without_extra_secrets_or_settings(self):
        info = release_info("My-Team/My-Repo", "v0.4.0", self.project)
        self.assertEqual(info["image"], "ghcr.io/my-team/my-repo")
        self.assertEqual(info["prerelease"], "false")
        for tag in ("v0.4.1", "arbitrary", "v0.4.0\ninvalid", "v0.4.0+metadata"):
            with self.assertRaises(ValueError):
                release_info("owner/repository", tag, self.project)

    def test_prerelease_can_be_published_without_moving_latest(self):
        self.project.write_text('[project]\nversion = "0.4.0-rc.1"\n', encoding="utf-8")
        info = release_info("owner/repository", "v0.4.0-rc.1", self.project)
        self.assertEqual(info["prerelease"], "true")

    def test_compose_uses_digest_and_checksums_cover_downloads(self):
        info = release_info("Owner/Repository", "v0.4.0", self.project)
        target = self.root / "dist"
        target.mkdir()
        (target / "package.whl").write_bytes(b"example distribution")
        digest = "sha256:" + "a" * 64
        write_assets(target, info, digest)
        compose = (target / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("ghcr.io/owner/repository@" + digest, compose)
        self.assertIn("./data/web:/app/data", compose)
        self.assertNotIn("${", compose)
        self.assertNotIn("environment:", compose)
        metadata = json.loads((target / "image.json").read_text())
        self.assertEqual(metadata["digest"], digest)
        for line in (target / "SHA256SUMS").read_text().splitlines():
            expected, name = line.split("  ", 1)
            self.assertEqual(hashlib.sha256((target / name).read_bytes()).hexdigest(), expected)
        self.assertIn("package.whl", (target / "SHA256SUMS").read_text())


if __name__ == "__main__":
    unittest.main()
