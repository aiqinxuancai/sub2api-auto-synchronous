"""Configuration file authority, migration, encrypted secrets and atomic persistence."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from relaydesk.service import Service, default_settings
from relaydesk.storage import Store
from sync_sub2api import SyncError


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_fresh_install_ignores_environment_and_dotenv_and_creates_file(self):
        (self.root / ".env").write_text("invalid dotenv line", encoding="utf-8")
        with patch.dict(os.environ, {"UPSTREAM_BASE_URL": "https://environment.invalid", "UPSTREAM_EMAIL": "env@example.com",
                                    "TARGET_ADMIN_API_KEY": "environment-key", "SYNC_INTERVAL_SECONDS": "1"}):
            service = Service(self.root / "data", start_scheduler=False)
            try:
                config = service.public_config()
                self.assertEqual(config["upstream_base"], "https://upstream.example.com")
                self.assertEqual(config["upstream_email"], "")
                self.assertFalse(config["sync_enabled"])
                self.assertFalse(config["secrets_set"]["target_admin_api_key"])
                self.assertEqual(config["interval_minutes"], 30)
                self.assertTrue((self.root / "data/config.json").is_file())
            finally:
                service.close()

    def test_saved_file_encrypts_credentials_and_survives_restart(self):
        service = Service(self.root, start_scheduler=False)
        service.save_config({"upstream_password": "private-password", "target_admin_api_key": "adm_sc_private_key",
                             "prefix": "saved-", "low_cost_rank_hours": 2, "low_cost_health_minutes": 9,
                             "low_cost_retry_minutes": 3})
        raw = (self.root / "config.json").read_text(encoding="utf-8")
        self.assertNotIn("private-password", raw)
        self.assertNotIn("adm_sc_private_key", raw)
        self.assertEqual(json.loads(raw)["config"]["prefix"], "saved-")
        self.assertIn("encrypted", json.loads(raw)["config"]["upstream_password"])
        service.close()
        restarted = Service(self.root, start_scheduler=False)
        try:
            self.assertEqual(restarted.store.get("config")["upstream_password"], "private-password")
            self.assertEqual(restarted.public_config()["low_cost_retry_minutes"], 3)
            self.assertEqual(restarted.public_config()["low_cost_rank_hours"], 2)
            with restarted.store.connect() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM settings WHERE key IN ('config','quota_rule')").fetchone()[0], 0)
            if os.name != "nt":
                self.assertEqual((self.root / "config.json").stat().st_mode & 0o777, 0o600)
        finally:
            restarted.close()

    def test_sqlite_configuration_migrates_once_and_file_wins_on_restart(self):
        store = Store(self.root)
        original = default_settings()
        original.update(upstream_password="legacy-secret", prefix="legacy-")
        rule = {"enabled": False, "scope": "selected", "user_ids": [3], "timezone": "Asia/Shanghai", "amount": 30}
        mapping = self.root / "profiles/saved/state.json"
        mapping.parent.mkdir(parents=True)
        mapping.write_text('{"groups":{"7":{"upstream_key_id":123}}}', encoding="utf-8")
        before = mapping.read_bytes()
        with store.connect() as connection:
            for key, value in {"config": original, "quota_rule": rule}.items():
                connection.execute("INSERT INTO settings VALUES (?,?)", (key, store.encrypt(value)))
        migrated = Store(self.root)
        self.assertEqual(migrated.get("config"), original)
        self.assertEqual(migrated.get("quota_rule"), rule)
        self.assertNotIn("legacy-secret", (self.root / "config.json").read_text(encoding="utf-8"))
        updated = {**original, "prefix": "file-wins-"}
        migrated.put("config", updated)
        with migrated.connect() as connection:
            connection.execute("INSERT INTO settings VALUES ('config',?)", (migrated.encrypt(original),))
        self.assertEqual(Store(self.root).get("config")["prefix"], "file-wins-")
        self.assertEqual(mapping.read_bytes(), before)

    def test_failed_save_preserves_previous_file(self):
        store = Store(self.root)
        store.put("config", default_settings())
        before = (self.root / "config.json").read_bytes()
        with patch("relaydesk.configuration.write_private_json", side_effect=OSError("disk full")):
            with self.assertRaises(SyncError):
                store.put("config", {**default_settings(), "prefix": "failed-"})
        self.assertEqual((self.root / "config.json").read_bytes(), before)

    def test_invalid_file_is_not_overwritten_by_migration_or_defaults(self):
        store = Store(self.root)
        for text in ("{broken", '{"version":true}', '{"version":1,"config":[]}', '{"version":9}'):
            (self.root / "config.json").write_text(text, encoding="utf-8")
            with self.assertRaises(SyncError):
                Store(self.root)
            self.assertEqual((self.root / "config.json").read_text(encoding="utf-8"), text)

    def test_multiple_store_instances_preserve_both_sections_during_concurrent_saves(self):
        first, second = Store(self.root), Store(self.root)
        errors = []

        def update(store, key):
            try:
                for index in range(10):
                    store.put(key, {"counter": index})
            except Exception as error:
                errors.append(type(error).__name__)

        threads = [threading.Thread(target=update, args=(first, "config")),
                   threading.Thread(target=update, args=(second, "quota_rule"))]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(first.get("config"), {"counter": 9})
        self.assertEqual(first.get("quota_rule"), {"counter": 9})

    def test_cli_reads_web_file_and_does_not_load_dotenv(self):
        result = subprocess.run([sys.executable, "-X", "utf8", str(Path(__file__).resolve().parent.parent / "sync_sub2api.py"),
                                 "--data-dir", str(self.root), "--inspect-upstream"],
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 1)
        self.assertIn("请先启动 Web", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
