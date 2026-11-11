"""A tag resolves to reproducible, repository-specific Docker deployment files."""

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.prepare_release import prepare_project, release_info, update_source_archive, write_assets


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "pyproject.toml"
        self.project.write_text('[project]\nversion = "0.4.0"\n', encoding="utf-8")
        self.page = self.root / "relaydesk/static/index.html"
        self.page.parent.mkdir(parents=True)
        self.page.write_text('<footer><span class="version">v0.4</span></footer>', encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_tag_and_repository_resolve_without_extra_secrets_or_settings(self):
        info = release_info("My-Team/My-Repo", "v0.4.0")
        self.assertEqual(info["image"], "ghcr.io/my-team/my-repo")
        self.assertEqual(info["prerelease"], "false")
        for tag in ("arbitrary", "v0.4.0\ninvalid", "v0.4.0+metadata"):
            with self.assertRaises(ValueError):
                release_info("owner/repository", tag)

    def test_tag_overrides_checkout_version_before_installation(self):
        info = release_info("owner/repository", "v0.0.1")
        self.assertEqual(info["version"], "0.0.1")
        prepare_project(info, self.root)
        self.assertEqual(self.project.read_text(encoding="utf-8"), '[project]\nversion = "0.0.1"\n')
        self.assertIn('>v0.0.1<', self.page.read_text(encoding="utf-8"))
        prepare_project(info, self.root)
        self.assertEqual(self.project.read_text(encoding="utf-8"), '[project]\nversion = "0.0.1"\n')

    def test_invalid_repository_is_rejected(self):
        for repository in ("missing-owner", "owner/repo\ninvalid", "owner/repo/extra"):
            with self.assertRaises(ValueError):
                release_info(repository, "v0.0.1")

    def test_missing_version_marker_does_not_partially_stamp_project(self):
        original = self.project.read_bytes()
        self.page.write_text('<footer>Flow</footer>', encoding="utf-8")
        with self.assertRaises(ValueError):
            prepare_project(release_info("owner/repository", "v0.0.1"), self.root)
        self.assertEqual(self.project.read_bytes(), original)

    def test_source_archive_matches_built_version_without_duplicate_files(self):
        archive = self.root / "source.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            output.comment = b"git-commit-id"
            output.write(self.project, "pyproject.toml")
            output.write(self.page, "relaydesk/static/index.html")
            output.writestr("README.md", "tracked source")
        prepare_project(release_info("owner/repository", "v0.0.1"), self.root)
        update_source_archive(archive, self.root)
        with zipfile.ZipFile(archive) as updated:
            self.assertEqual(updated.read("pyproject.toml"), self.project.read_bytes())
            self.assertEqual(updated.read("relaydesk/static/index.html"), self.page.read_bytes())
            self.assertEqual(updated.read("README.md"), b"tracked source")
            self.assertEqual(updated.comment, b"git-commit-id")
            self.assertEqual(len(updated.namelist()), len(set(updated.namelist())))

    def test_prerelease_can_be_published_without_moving_latest(self):
        info = release_info("owner/repository", "v0.4.0-rc.1")
        self.assertEqual(info["prerelease"], "true")
        prepare_project(info, self.root)
        self.assertIn('version = "0.4.0-rc.1"', self.project.read_text(encoding="utf-8"))

    def test_compose_uses_digest_and_checksums_cover_downloads(self):
        info = release_info("Owner/Repository", "v0.4.0")
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
