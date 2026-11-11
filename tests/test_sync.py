"""HTTP integration tests: lifecycle, health filtering, auth and repeatability."""

import copy
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from sync_sub2api import APIError, Client, Config, State, SyncError, Synchronizer, select_usable


def iso_after(seconds=300):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


class FakeSites:
    def __init__(self):
        self.groups = [
            {"id": 7, "name": "Alpha", "platform": "openai", "status": "active",
             "rate_multiplier": 1.5},
            {"id": 8, "name": "Bad", "platform": "openai", "status": "active",
             "rate_multiplier": 1.0},
            {"id": 9, "name": "Unknown", "platform": "openai", "status": "active",
             "rate_multiplier": 1.0},
        ]
        self.rates = {"7": 0.25}
        self.health = "healthy"
        self.model_health = {"model-a": "healthy", "model-b": "failed", "model-c": "insufficient"}
        self.expired = False
        self.stale = False
        self.incomplete = False
        self.fail_rates = False
        self.key_limit = False
        self.hourly_key_limit = False
        self.healthy_group_ids = {7}
        self.transient_key_limits = 0
        self.deny_groups = False
        self.keys = []
        self.local_groups = [{"id": 1, "name": "Manual", "description": "unrelated",
                              "platform": "openai", "status": "active", "rate_multiplier": 9}]
        self.accounts = []
        self.requests = []
        self.logins = 0
        self.refreshes = 0
        self.provider_overrides = {}

    def providers(self):
        items = []
        for group in self.groups:
            cards = [{"model": model, "health": health if group["id"] in self.healthy_group_ids else "failed",
                      "probe_state": "enabled", "health_expires_at": iso_after(-30 if self.expired else 300)}
                     for model, health in self.model_health.items()]
            if group["id"] in self.healthy_group_ids and self.health != "healthy":
                for card in cards:
                    card["health"] = self.health
            items.append({"group_id": group["id"], "platform": group["platform"], "visible_in_hall": True,
                          "rate_multiplier": group["rate_multiplier"],
                          "available": group["id"] == 7, "probe_state": "enabled",
                          "detections_enabled": True, "probe_expires_at": iso_after(),
                          "group_models": [{"model": model} for model in self.model_health],
                          "model_cards": cards, "model_detection": {"applicable": True, "status": "passed",
                              "stage_enabled": {"detection": True, "text": False},
                              "stages": {"detection": {"state": "completed"}}},
                          **self.provider_overrides.get(group["id"], {})})
        return {"items": items, "total": len(items) + int(self.incomplete),
                "generated_at": iso_after(-1), "stale": self.stale}

    def handle(self, method, raw_path, headers, body):
        parsed = urlsplit(raw_path)
        path, query = parsed.path, parse_qs(parsed.query)
        self.requests.append((method, path, copy.deepcopy(body)))
        if path == "/up/api/v1/auth/login":
            self.logins += 1
            return 200, {"access_token": "up-token", "refresh_token": "up-refresh", "expires_in": 3600}
        if path == "/up/api/v1/auth/refresh":
            self.refreshes += 1
            return 200, {"access_token": "up-token", "refresh_token": "rotated-refresh", "expires_in": 3600}
        if path.startswith("/up/") and headers.get("Authorization") != "Bearer up-token":
            return 401, None
        if path.startswith("/down/") and headers.get("x-api-key") != "test-admin-key":
            return 401, None
        if path == "/up/api/v1/groups/available":
            return 200, self.groups
        if path == "/up/api/v1/groups/rates":
            return (503, None) if self.fail_rates else (200, self.rates)
        if path == "/up/api/v2/public/providers":
            return 200, self.providers()
        if path == "/down/api/v1/admin/groups/sort-order" and method == "PUT":
            for update in body["updates"]:
                row = next(item for item in self.local_groups if item["id"] == update["id"])
                row["sort_order"] = update["sort_order"]
            return 200, {"message": "updated"}
        if path.startswith("/down/api/v1/admin/accounts/") and path.endswith("/schedulable"):
            item_id = int(path.split("/")[-2])
            row = next(item for item in self.accounts if item["id"] == item_id)
            row["schedulable"] = body["schedulable"]
            return 200, copy.deepcopy(row)
        collections = {
            "/up/api/v1/keys": self.keys,
            "/down/api/v1/admin/groups": self.local_groups,
            "/down/api/v1/admin/accounts": self.accounts,
        }
        if path in collections:
            rows = collections[path]
            if method == "GET":
                page = int(query.get("page", [1])[0])
                page_size = int(query.get("page_size", [100])[0])
                items = copy.deepcopy(rows[(page - 1) * page_size:page * page_size])
                if path.endswith("accounts"):
                    for item in items:
                        item["credentials"].pop("api_key", None)
                        item["credentials_status"] = {"has_api_key": True}
                return 200, {"items": items, "total": len(rows)}
            if method == "POST":
                if path.endswith("keys") and self.transient_key_limits:
                    self.transient_key_limits -= 1
                    return 429, None
                if path.endswith("keys") and (self.key_limit or self.hourly_key_limit):
                    return 429, None
                if path.endswith("groups") and self.deny_groups:
                    return 403, None
                created = copy.deepcopy(body)
                created["id"] = max([item["id"] for item in rows] + [10]) + 1
                created["status"] = "active"
                if path.endswith("keys"):
                    created.update({"key": "sk-fake-test-secret", "quota": 0, "quota_used": 0})
                if path.endswith("accounts"):
                    created["schedulable"] = True
                rows.append(created)
                return 200, copy.deepcopy(created)
        for root, rows in collections.items():
            if path.startswith(root + "/"):
                item_id = int(path.rsplit("/", 1)[1])
                row = next((item for item in rows if item["id"] == item_id), None)
                if row is None:
                    return 404, None
                if method == "PUT":
                    body.pop("schedulable", None)  # Real API requires the separate POST route.
                    row.update(copy.deepcopy(body))
                    return 200, copy.deepcopy(row)
                return 200, copy.deepcopy(row)
        return 404, None

    def mutations(self):
        return [(method, path, body) for method, path, body in self.requests
                if method in {"POST", "PUT", "DELETE"} and "/auth/" not in path]


class SyncIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSites()
        fake = self.fake

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def do_PUT(self):
                self.respond()

            def respond(self):
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length)) if length else None
                status, data = fake.handle(self.command, self.path, self.headers, payload)
                envelope = {"code": 0 if status == 200 else status,
                            "message": "success" if status == 200 else "rejected", "data": data}
                if status == 429 and fake.hourly_key_limit:
                    envelope["reason"] = "API_KEY_CREATE_RATE_LIMITED"
                raw = json.dumps(envelope).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.temp = tempfile.TemporaryDirectory()
        root = f"http://127.0.0.1:{self.server.server_port}"
        self.config = Config(
            upstream_base=root + "/up", target_base=root + "/down",
            upstream_email="test@example.com", upstream_password="test-password",
            target_admin_api_key="test-admin-key", dry_run=False,
            request_interval=0, rate_limit_retries=0,
            state_file=Path(self.temp.name) / "state.json",
            report_file=Path(self.temp.name) / "report.json")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def cycle(self):
        return Synchronizer(self.config, State(self.config)).cycle()

    def test_create_healthy_group_and_model_then_repeat_without_duplicates_or_writes(self):
        report = self.cycle()
        self.assertEqual(report["usable_groups"], 1)
        self.assertEqual(report["groups"][0]["models"], ["model-a"])
        self.assertEqual(report["groups"][0]["local_rate"], 0.25)
        self.assertEqual(len(self.fake.keys), 1)
        self.assertEqual(len(self.fake.accounts), 1)
        self.assertEqual(len(self.fake.local_groups), 2)
        group = self.fake.local_groups[-1]
        self.assertIn("[sub2api-sync:", group["description"])
        self.assertEqual(group["model_allowlist"], {"enabled": True, "models": ["model-a"]})
        self.assertEqual(self.fake.accounts[0]["credentials"]["model_mapping"], {"model-a": "model-a"})
        self.fake.requests.clear()
        report = self.cycle()
        self.assertEqual(report["errors"], [])
        self.assertEqual(self.fake.mutations(), [])
        self.assertEqual(self.fake.logins, 1)

    def test_unavailable_disables_then_healthy_reenables_same_marked_resources(self):
        self.cycle()
        group_id = self.fake.local_groups[-1]["id"]
        account_id = self.fake.accounts[0]["id"]
        self.fake.health = "failed"
        self.cycle()
        self.assertEqual(self.fake.local_groups[-1]["status"], "inactive")
        self.assertEqual(self.fake.accounts[0]["status"], "inactive")
        self.assertFalse(self.fake.accounts[0]["schedulable"])
        self.assertEqual(self.fake.local_groups[0]["status"], "active")
        self.fake.health = "healthy"
        self.cycle()
        self.assertEqual(self.fake.local_groups[-1]["id"], group_id)
        self.assertEqual(self.fake.accounts[0]["id"], account_id)
        self.assertEqual(self.fake.local_groups[-1]["status"], "active")
        self.assertTrue(self.fake.accounts[0]["schedulable"])
        self.assertEqual(len(self.fake.keys), 1)

    def test_zero_user_rate_and_rate_factor_are_preserved(self):
        self.fake.rates = {"7": 0}
        self.config.rate_factor = Decimal("1.2")
        self.cycle()
        self.assertEqual(self.fake.local_groups[-1]["rate_multiplier"], 0)
        self.assertEqual(self.fake.accounts[0]["rate_multiplier"], 0)
        self.fake.rates = {"7": 0.25}
        self.cycle()
        self.assertEqual(self.fake.local_groups[-1]["rate_multiplier"], 0.3)
        self.assertEqual(self.fake.accounts[0]["rate_multiplier"], 0.25)

    def test_renamed_upstream_key_is_retained_and_reused_after_recovery(self):
        self.cycle()
        key_id = self.fake.keys[0]["id"]
        self.fake.keys[0]["name"] = "Human renamed this Key"
        self.fake.health = "failed"
        self.cycle()
        self.fake.health = "healthy"
        self.cycle()
        self.assertEqual(len(self.fake.keys), 1)
        self.assertEqual(State(self.config).data["groups"]["7"]["upstream_key_id"], key_id)
        self.assertFalse(any(method == "DELETE" for method, _, _ in self.fake.requests))

    def test_upstream_key_deletion_is_prohibited(self):
        client = Client(self.config, State(self.config), "upstream")
        with self.assertRaises(SyncError):
            client.request("DELETE", "/keys/11")
        self.assertEqual(self.fake.requests, [])

    def test_preview_does_not_create_keys_groups_or_accounts(self):
        self.config.dry_run = True
        report = self.cycle()
        self.assertTrue(report["target_verified"])
        self.assertEqual(self.fake.mutations(), [])
        self.assertEqual(report["actions"][0]["action"], "create")

    def test_stale_or_partial_provider_snapshot_never_disables_existing_resources(self):
        self.cycle()
        for field in ("stale", "incomplete"):
            self.fake.requests.clear()
            setattr(self.fake, field, True)
            with self.assertRaises(SyncError):
                self.cycle()
            self.assertEqual(self.fake.mutations(), [])
            self.assertEqual(self.fake.local_groups[-1]["status"], "active")
            setattr(self.fake, field, False)

    def test_failed_rate_read_never_writes_or_treats_missing_rate_as_default(self):
        self.cycle()
        self.fake.requests.clear()
        self.fake.fail_rates = True
        with self.assertRaises(APIError):
            self.cycle()
        self.assertEqual(self.fake.mutations(), [])

    def test_expired_health_excludes_models_and_disables_group(self):
        self.cycle()
        self.fake.expired = True
        report = self.cycle()
        self.assertEqual(report["usable_groups"], 0)
        self.assertEqual(self.fake.local_groups[-1]["status"], "inactive")

    def test_model_changes_update_allowlist_and_mapping_without_new_key(self):
        self.cycle()
        self.fake.model_health.update({"model-a": "failed", "model-b": "healthy"})
        self.cycle()
        self.assertEqual(self.fake.local_groups[-1]["model_allowlist"]["models"], ["model-b"])
        self.assertEqual(self.fake.accounts[0]["credentials"]["model_mapping"], {"model-b": "model-b"})
        self.assertEqual(len(self.fake.keys), 1)

    def test_state_loss_recovers_mapping_from_markers_and_reuses_key(self):
        self.cycle()
        self.config.state_file.unlink()
        report = self.cycle()
        self.assertEqual(report["errors"], [])
        self.assertEqual(len(self.fake.local_groups), 2)
        self.assertEqual(len(self.fake.accounts), 1)
        self.assertEqual(len(self.fake.keys), 1)

    def test_tampered_marker_stops_before_writes(self):
        self.cycle()
        self.fake.local_groups[-1]["description"] = "manually reassigned"
        self.fake.requests.clear()
        with self.assertRaises(SyncError):
            self.cycle()
        self.assertEqual(self.fake.mutations(), [])

    def test_removed_upstream_access_disables_only_previously_marked_groups(self):
        self.cycle()
        self.fake.groups = [group for group in self.fake.groups if group["id"] != 7]
        self.cycle()
        self.assertEqual(self.fake.local_groups[-1]["status"], "inactive")
        self.assertEqual(self.fake.local_groups[0]["status"], "active")

    def test_key_rate_limit_stops_before_creating_an_empty_local_group(self):
        self.fake.key_limit = True
        report = self.cycle()
        self.assertTrue(report["errors"])
        self.assertEqual(len(self.fake.local_groups), 1)
        self.assertEqual(self.fake.accounts, [])

    def test_zero_key_creation_budget_marks_group_pending(self):
        self.config.max_new_keys = 0
        report = self.cycle()
        self.assertEqual(report["pending"], 1)
        self.assertEqual(report["errors"], [])
        self.assertEqual(self.fake.mutations(), [])

    def test_short_rate_limit_recovers_without_duplicate_resources(self):
        self.config.rate_limit_retries = 1
        self.fake.transient_key_limits = 1
        with patch("sync_sub2api.time.sleep") as sleep:
            report = self.cycle()
        self.assertEqual(report["errors"], [])
        self.assertEqual(len(self.fake.keys), 1)
        self.assertEqual(len(self.fake.accounts), 1)
        self.assertEqual(len(self.fake.local_groups), 2)
        sleep.assert_called_once_with(60)

    def test_hourly_key_limit_keeps_updating_existing_groups_and_reports_pending(self):
        self.fake.groups = [{"id": 8, "name": "Existing", "platform": "openai",
                             "status": "active", "rate_multiplier": 0.5}]
        self.fake.healthy_group_ids = {8}
        self.cycle()
        self.fake.groups.append({"id": 7, "name": "New", "platform": "openai",
                                 "status": "active", "rate_multiplier": 0.2})
        self.fake.healthy_group_ids = {7, 8}
        self.fake.model_health.update({"model-a": "failed", "model-b": "healthy"})
        self.fake.hourly_key_limit = True
        self.config.rate_limit_retries = 2
        with patch("sync_sub2api.time.sleep") as sleep:
            report = self.cycle()
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["pending"], 1)
        self.assertEqual(len(report["warnings"]), 1)
        self.assertEqual(self.fake.local_groups[-1]["model_allowlist"]["models"], ["model-b"])
        self.assertEqual(len(self.fake.keys), 1)
        sleep.assert_not_called()

    def test_expired_session_refreshes_and_persists_rotated_token(self):
        state = State(self.config)
        state.data["sessions"]["upstream"] = {
            "api_url": self.config.upstream_api, "email": self.config.upstream_email,
            "access_token": "expired", "refresh_token": "up-refresh"}
        state.save()
        self.cycle()
        self.assertEqual(self.fake.refreshes, 1)
        self.assertEqual(self.fake.logins, 0)
        self.assertEqual(State(self.config).data["sessions"]["upstream"]["refresh_token"], "rotated-refresh")

    def test_pagination_reads_all_keys_before_reusing_existing_named_key(self):
        synchronizer = Synchronizer(self.config, State(self.config))
        name = f"sub2api-sync-{synchronizer.target_id}-g7"
        self.fake.keys = [{"id": index, "name": f"other-{index}", "group_id": 8,
                           "status": "active", "key": "sk-other"} for index in range(1, 102)]
        self.fake.keys.append({"id": 102, "name": name, "group_id": 7,
                               "status": "active", "key": "sk-owned", "quota": 0})
        report = self.cycle()
        self.assertEqual(report["new_upstream_keys"], 0)
        self.assertEqual(len(self.fake.keys), 102)

    def test_group_scope_does_not_disable_other_previously_synced_groups(self):
        self.cycle()
        self.config.group_ids = {8}
        self.cycle()
        self.assertEqual(self.fake.local_groups[-1]["status"], "active")

    def test_state_identity_prevents_cross_site_mapping_reuse(self):
        self.cycle()
        self.config.target_base = "http://other.example"
        self.config.target_api = "http://other.example/api/v1"
        with self.assertRaises(SyncError):
            State(self.config)

    def test_error_messages_and_reports_do_not_include_api_keys_or_passwords(self):
        self.fake.deny_groups = True
        report = self.cycle()
        serialized = json.dumps(report)
        for secret in ("test-password", "test-admin-key", "sk-fake-test-secret", "up-token"):
            self.assertNotIn(secret, serialized)

    def test_invalid_rate_is_rejected_before_any_mutation(self):
        for value in ("NaN", "Infinity", -1, "bad"):
            self.fake.requests.clear()
            self.fake.rates = {"7": value}
            with self.assertRaises(SyncError):
                self.cycle()
            self.assertEqual(self.fake.mutations(), [])


if __name__ == "__main__":
    unittest.main()
