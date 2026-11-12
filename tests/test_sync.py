"""HTTP integration tests: lifecycle, health filtering, auth and repeatability."""

import copy
import json
import logging
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from sync_sub2api import (PROGRESS, APIError, Client, Config, State, SyncError, Synchronizer, changed_fields,
                          hall_model_pricing, hall_stats, select_usable, stats_text)


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
        self.deny_accounts = False
        self.ignore_account_groups_on_create = False
        self.ignore_schedulable = False
        self.keys = []
        self.local_groups = [{"id": 1, "name": "Manual", "description": "unrelated",
                              "platform": "openai", "status": "active", "rate_multiplier": 9}]
        self.accounts = []
        self.requests = []
        self.logins = 0
        self.refreshes = 0
        self.provider_overrides = {}
        self.model_prices = {}  # model -> hall price (USD per 1M tokens)

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
                          "group_models": [{"model": model, **({"price": self.model_prices[model]}
                                                                if model in self.model_prices else {})}
                                           for model in self.model_health],
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
            if not self.ignore_schedulable:
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
                        if not item.get("group_ids"):
                            item.pop("group_ids", None)  # Real API omits an empty binding.
                return 200, {"items": items, "total": len(rows)}
            if method == "POST":
                if path.endswith("keys") and self.transient_key_limits:
                    self.transient_key_limits -= 1
                    return 429, None
                if path.endswith("keys") and (self.key_limit or self.hourly_key_limit):
                    return 429, None
                if path.endswith("groups") and self.deny_groups:
                    return 403, None
                if path.endswith("accounts") and self.deny_accounts:
                    return 400, {"message": "account rejected"}
                created = copy.deepcopy(body)
                if path.endswith("groups"):
                    self.store_pricing(created, created)
                created["id"] = max([item["id"] for item in rows] + [10]) + 1
                created["status"] = "active"
                if path.endswith("keys"):
                    created.update({"key": "sk-fake-test-secret", "quota": 0, "quota_used": 0})
                if path.endswith("accounts"):
                    created["schedulable"] = True
                    if self.ignore_account_groups_on_create:
                        created.pop("group_ids", None)
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
                    body = copy.deepcopy(body)
                    if root.endswith("groups"):
                        self.store_pricing(body, row)
                    row.update(body)
                    return 200, copy.deepcopy(row)
                return 200, copy.deepcopy(row)
        return 404, None

    @staticmethod
    def store_pricing(body, group):
        """Like Sub2API: price cards gain ids, the group platform and empty optional prices; [] reads back null."""
        if "model_pricing" in body:
            body["model_pricing"] = [{"id": index, "intervals": None, "cache_write_1h_price": None, "fast_multiplier": None,
                                      **entry, "platform": entry.get("platform") or group.get("platform")}
                                     for index, entry in enumerate(body["model_pricing"], 1)] or None

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
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
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

    def test_group_stays_inactive_until_its_account_is_bound(self):
        self.fake.deny_accounts = True
        report = Synchronizer(self.config, State(self.config)).cycle()
        self.assertEqual(len(report["errors"]), 1)
        created = self.fake.local_groups[-1]
        self.assertIn("[7]", created["name"])
        self.assertEqual(created["status"], "inactive")
        self.assertEqual(self.fake.accounts, [])
        self.fake.deny_accounts = False
        report = Synchronizer(self.config, State(self.config)).cycle()
        self.assertEqual(report["errors"], [])
        self.assertEqual(self.fake.local_groups[-1]["status"], "active")
        self.assertEqual(self.fake.accounts[0]["group_ids"], [created["id"]])

    def test_account_created_without_group_is_assigned_and_repaired(self):
        self.fake.ignore_account_groups_on_create = True
        report = Synchronizer(self.config, State(self.config)).cycle()
        self.assertEqual(report["errors"], [])
        group = self.fake.local_groups[-1]
        self.assertEqual(self.fake.accounts[0]["group_ids"], [group["id"]])
        self.assertEqual(group["status"], "active")
        # An account whose group selection was later cleared is assigned again.
        self.fake.accounts[0].pop("group_ids")
        report = Synchronizer(self.config, State(self.config)).cycle()
        self.assertEqual(report["errors"], [])
        self.assertEqual(self.fake.accounts[0]["group_ids"], [group["id"]])

    def test_group_without_account_is_held_while_key_is_pending(self):
        Synchronizer(self.config, State(self.config)).cycle()
        group = self.fake.local_groups[-1]
        self.fake.accounts.clear()      # account deleted on the server
        self.fake.keys.clear()          # and its upstream Key is gone
        self.config.auto_create_keys = False
        report = Synchronizer(self.config, State(self.config)).cycle()
        self.assertEqual(report["pending"], 1)
        self.assertEqual(group["status"], "inactive")
        self.config.auto_create_keys = True
        Synchronizer(self.config, State(self.config)).cycle()
        self.assertEqual(group["status"], "active")
        self.assertEqual(self.fake.accounts[0]["group_ids"], [group["id"]])

    def test_final_audit_disables_group_whose_account_cannot_be_scheduled(self):
        Synchronizer(self.config, State(self.config)).cycle()
        group = self.fake.local_groups[-1]
        self.fake.accounts[0]["schedulable"] = False
        self.fake.ignore_schedulable = True
        report = Synchronizer(self.config, State(self.config)).cycle()
        self.assertEqual(group["status"], "inactive")
        self.assertIn("no_bound_account", [row.get("reason") for row in report["actions"]])
        self.assertEqual(State(self.config).data["groups"]["7"]["last_reason"], "no_bound_account")
        self.fake.ignore_schedulable = False
        report = Synchronizer(self.config, State(self.config)).cycle()
        self.assertEqual(report["errors"], [])
        self.assertEqual(group["status"], "active")

    def test_account_concurrency_follows_setting(self):
        Synchronizer(self.config, State(self.config)).cycle()
        self.assertEqual(self.fake.accounts[0]["concurrency"], 100)
        self.config.concurrency = 7
        report = Synchronizer(self.config, State(self.config)).cycle()
        self.assertEqual(self.fake.accounts[0]["concurrency"], 7)
        self.assertEqual(report["concurrency_fixed"], 1)
        self.fake.requests.clear()
        report = Synchronizer(self.config, State(self.config)).cycle()
        self.assertEqual(report["concurrency_fixed"], 0)
        self.assertEqual(self.fake.mutations(), [])

    def test_logs_name_the_upstream_group(self):
        with self.assertLogs("sub2api-sync", "INFO") as logs:
            self.cycle()
        self.assertIn("「Alpha」#7：分组已启用，账号已新建，模型 1", "\n".join(logs.output))
        self.assertNotIn("group=", "\n".join(logs.output))

    def synced_group(self, upstream_id=7):
        local_id = State(self.config).data["groups"][str(upstream_id)]["local_group_id"]
        return next(row for row in self.fake.local_groups if row["id"] == local_id)

    def test_hall_model_prices_become_group_price_cards(self):
        self.fake.model_prices = {"model-a": {"input_per_million": 1, "output_per_million": 4,
                                              "cache_input_per_million": 0.02, "cache_write_per_million": 0}}
        self.assertEqual(self.config.price_sync_platforms, {"deepseek", "zhipu", "kimi"})
        self.cycle()
        group = self.synced_group()
        self.assertNotIn("model_pricing", group)  # openai is not priced by default
        self.config.price_sync_platforms = {"openai"}
        with self.assertLogs("sub2api-sync", "INFO") as logs:
            report = self.cycle()
        self.assertEqual(report["pricing_updated"], 1)
        self.assertIn("模型价格已更新", "\n".join(logs.output))
        card, = group["model_pricing"]
        self.assertEqual((card["models"], card["platform"], card["billing_mode"]), (["model-a"], "openai", "token"))
        self.assertEqual((card["input_price"], card["output_price"], card["cache_read_price"], card["cache_write_price"]),
                         (1e-6, 4e-6, 2e-8, 0.0))
        self.fake.requests.clear()
        self.assertEqual(self.cycle()["pricing_updated"], 0)
        self.assertEqual(self.fake.mutations(), [])  # server-added fields do not cause rewrites
        self.fake.model_prices["model-a"]["input_per_million"] = 1.5
        self.fake.requests.clear()
        self.cycle()
        writes = [body for method, path, body in self.fake.mutations() if path.endswith(f"/admin/groups/{group['id']}")]
        self.assertEqual([sorted(body) for body in writes], [["model_pricing"]])
        self.assertEqual(group["model_pricing"][0]["input_price"], 1.5e-6)
        # Turning the platform off clears the prices this tool wrote, once.
        self.config.price_sync_platforms = set()
        self.cycle()
        self.assertFalse(group["model_pricing"])
        self.fake.requests.clear()
        self.cycle()
        self.assertEqual(self.fake.mutations(), [])

    def test_manual_group_prices_stay_when_price_sync_is_off(self):
        self.cycle()
        group = self.synced_group()
        group["model_pricing"] = [{"models": ["model-a"], "billing_mode": "token", "input_price": 9e-6, "output_price": 9e-6}]
        self.fake.requests.clear()
        self.cycle()
        self.assertEqual(group["model_pricing"][0]["input_price"], 9e-6)
        self.assertEqual(self.fake.mutations(), [])

    def test_price_cards_share_identical_prices_and_skip_incomplete_ones(self):
        flash = {"input_per_million": 1, "output_per_million": 4, "cache_input_per_million": 0.02, "cache_write_per_million": 0}
        prices = {"deepSeek-v4.1-flash-1017": flash, "deepseek-v4.1-flash-1017": {**flash, "input_per_million": 9},
                  "deepseek-flash": flash, "deepseek-v4-pro": {"input_per_million": 4.5, "output_per_million": 13.5},
                  "kimi-k3.1-pro": {"input_per_million": None, "output_per_million": None},
                  "broken": {"input_per_million": -1, "output_per_million": 2}, "unpriced": None}
        models = ["deepSeek-v4.1-flash-1017", "deepseek-v4.1-flash-1017", "deepseek-flash", "deepseek-v4-pro",
                  "kimi-k3.1-pro", "broken", "unpriced", "absent"]
        cards = hall_model_pricing("deepseek", models, prices)
        self.assertEqual([card["models"] for card in cards], [["deepSeek-v4.1-flash-1017", "deepseek-flash"], ["deepseek-v4-pro"]])
        self.assertEqual(cards[1], {"platform": "deepseek", "models": ["deepseek-v4-pro"], "billing_mode": "token",
                                    "input_price": 4.5e-6, "output_price": 1.35e-5})
        stored = {"model_pricing": copy.deepcopy(cards)}
        FakeSites.store_pricing(stored, {"platform": "deepseek"})
        self.assertEqual(changed_fields(stored, {"model_pricing": cards}), {})
        self.assertEqual(changed_fields({"model_pricing": None}, {"model_pricing": []}), {})

    def test_hall_stats_end_the_description_and_refresh_in_place(self):
        hall = {"summary_range": "1h", "user_latency_ms": 3240.7, "summary_tps": 37.6,
                "success_rates": {"1h": 0.9912}, "range_cache": {"ready": True, "stale": False, "cache_hit_rate": 0.8096}}
        self.fake.groups[0]["description"] = "upstream note"
        self.fake.provider_overrides[7] = hall
        report = self.cycle()
        group = self.synced_group()
        marker = report["groups"][0]["marker"]
        self.assertEqual(group["description"],
                         f"{marker}\nupstream note\n供应商大厅 1h：延迟 3.2s · 缓存 81% · TPS 38 · 成功率 99%")
        self.assertEqual(report["groups"][0]["stats"], "延迟 3.2s · 缓存 81% · TPS 38 · 成功率 99%")
        self.fake.requests.clear()
        self.cycle()
        self.assertEqual(self.fake.mutations(), [])  # unchanged measurements are not rewritten
        hall["user_latency_ms"] = 12100
        hall["range_cache"]["stale"] = True
        self.fake.requests.clear()
        with self.assertLogs("sub2api-sync", "INFO") as logs:
            report = self.cycle()
        writes = [body for method, path, body in self.fake.mutations() if path.endswith(f"/admin/groups/{group['id']}")]
        self.assertEqual(writes, [{"description": f"{marker}\nupstream note\n供应商大厅 1h：延迟 12.1s · 缓存 — · TPS 38 · 成功率 99%"}])
        self.assertEqual(next(action for action in report["actions"] if action.get("upstream_id") == 7)["group_result"], "refreshed")
        self.assertIn("分组指标已刷新", "\n".join(logs.output))
        # A hall without measurements leaves only the marker and the upstream description.
        self.fake.provider_overrides.clear()
        self.cycle()
        self.assertEqual(group["description"], f"{marker}\nupstream note")

    def test_hall_stats_ignore_missing_and_invalid_measurements(self):
        self.assertEqual(stats_text(hall_stats({})), "")
        stats = hall_stats({"user_latency_ms": 0, "summary_tps": True, "success_rates": {"1h": 1.5},
                            "runtime_cache_1h": {"ready": True, "cache_hit_rate": 0.5},
                            "range_cache": {"ready": True, "cache_hit_rate": 0.9}})  # range data needs summary_range
        self.assertEqual(stats, {"latency_ms": None, "cache_hit_rate": 0.5, "tps": None, "success_rate": None})
        self.assertEqual(stats_text(stats), "延迟 — · 缓存 50% · TPS — · 成功率 —")
        self.assertIsNone(hall_stats({"runtime_cache_1h": {"cache_hit_rate": 0.5}})["cache_hit_rate"])  # not ready

    def test_progress_reports_each_group_and_the_pending_request(self):
        events = []

        class Recorder(logging.Handler):
            def emit(self, record):
                events.append(getattr(record, "progress", None) or {"request": record.request})

        recorder = Recorder()
        PROGRESS.addHandler(recorder)
        PROGRESS.setLevel(logging.INFO)
        self.addCleanup(PROGRESS.setLevel, logging.NOTSET)
        self.addCleanup(PROGRESS.removeHandler, recorder)
        self.cycle()
        steps = [event for event in events if "step" in event]
        self.assertEqual(steps[0]["step"], "读取上游分组与专属倍率")
        self.assertIn({"step": "同步分组 「Alpha」#7", "done": 1, "total": 1}, steps)
        requests = [event["request"] for event in events if "request" in event]
        self.assertIn("本地 POST /admin/groups", requests)
        self.assertIsNone(requests[-1])  # cleared once the response arrived

    def test_every_sync_fixes_concurrency_of_disabled_accounts_but_not_unrelated_ones(self):
        self.cycle()
        owned = self.fake.accounts[0]
        self.fake.health = "failed"
        self.cycle()
        self.assertEqual(owned["status"], "inactive")
        self.fake.accounts.append({"id": 500, "name": "Manual", "notes": "hand-made", "platform": "openai",
                                   "type": "apikey", "credentials": {}, "status": "active", "concurrency": 3})
        self.config.concurrency = 9
        report = self.cycle()
        self.assertEqual(owned["concurrency"], 9)
        self.assertEqual(owned["status"], "inactive")  # only the concurrency is corrected
        self.assertEqual(self.fake.accounts[-1]["concurrency"], 3)
        self.assertIn({"action": "concurrency", "account_id": owned["id"], "from": 100, "to": 9}, report["actions"])
        self.assertEqual(report["concurrency_fixed"], 1)

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
