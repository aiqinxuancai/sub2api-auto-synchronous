"""Local HTTP integration tests for employee management and the Web console."""

import copy
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from relaydesk.service import Service, next_month
from relaydesk.low_cost import LowCostSelector
from relaydesk.scoring import DEFAULT_WEIGHTS
from relaydesk.web import create_app
from sync_sub2api import SyncError
from test_sync import FakeSites


class WebSites(FakeSites):
    def __init__(self):
        super().__init__()
        self.users = [
            {"id": 1, "email": "admin@example.com", "username": "管理员", "role": "admin",
             "balance": 500, "concurrency": 10, "status": "active", "allowed_groups": []},
            {"id": 2, "email": "alice@example.com", "username": "Alice", "role": "user",
             "balance": 7.5, "concurrency": 5, "status": "active", "allowed_groups": []},
            {"id": 3, "email": "bob@example.com", "username": "Bob", "role": "user",
             "balance": 23, "concurrency": 5, "status": "active", "allowed_groups": []},
        ]
        self.user_mutations = []
        self.fail_users = set()
        self.uncertain_users = set()

    def handle(self, method, raw_path, headers, body):
        parsed = urlsplit(raw_path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path == "/up/api/v1/auth/login" and body.get("password") != "test-password":
            return 401, None
        if path.startswith("/down/api/v1/admin/users"):
            if headers.get("x-api-key") != "test-admin-key":
                return 401, None
            if method == "GET":
                page, size = int(query.get("page", [1])[0]), int(query.get("page_size", [100])[0])
                return 200, {"items": copy.deepcopy(self.users[(page - 1) * size:page * size]), "total": len(self.users)}
            self.user_mutations.append((method, path, copy.deepcopy(body), headers.get("Idempotency-Key")))
            if path == "/down/api/v1/admin/users" and method == "POST":
                if any(user["email"] == body["email"] for user in self.users):
                    return 409, None
                row = {**body, "id": max(user["id"] for user in self.users) + 1, "status": "active"}
                row.pop("password", None)
                self.users.append(row)
                return 200, copy.deepcopy(row)
            user_id = int(path.split("/users/")[1].split("/")[0])
            if user_id in self.fail_users:
                return 403, None
            user = next(user for user in self.users if user["id"] == user_id)
            if method == "POST" and body.get("operation") != "set":
                return 400, None
            user["balance"] = body["balance"]
            return (503, None) if user_id in self.uncertain_users else (200, copy.deepcopy(user))
        return super().handle(method, raw_path, headers, body)


class LocalSites:
    def __init__(self):
        self.fake = WebSites()
        fake = self.fake

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self): self.respond()
            def do_POST(self): self.respond()
            def do_PUT(self): self.respond()

            def respond(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else None
                status, data = fake.handle(self.command, self.path, self.headers, body)
                envelope = {"code": 0 if status == 200 else status, "message": "result", "data": data}
                if status == 429 and fake.hourly_key_limit:
                    envelope["reason"] = "API_KEY_CREATE_RATE_LIMITED"
                raw = json.dumps(envelope).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args): pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def configure(self, service):
        service.save_config({
            "upstream_base": self.base + "/up", "upstream_email": "test@example.com",
            "upstream_password": "test-password", "target_base": self.base + "/down",
            "target_admin_api_key": "test-admin-key", "request_interval": 0,
            "max_new_keys": 2, "sync_enabled": False})

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()


def await_job(service, job_id, timeout=12):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = service.store.job(job_id)
        if job and job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.02)
    raise AssertionError("Job did not finish")


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sites = LocalSites()
        self.service = Service(self.root / "data", start_scheduler=False)
        self.sites.configure(self.service)
        self.client = TestClient(create_app(service=self.service))
        self.client.__enter__()
        response = self.client.post("/api/auth/setup", json={"password": "test-console-password"})
        self.assertEqual(response.status_code, 200)
        self.headers = {"X-CSRF-Token": response.json()["csrf"]}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.service.close()
        self.sites.close()
        self.temp.cleanup()

    def post(self, path, body=None):
        return self.client.post("/api" + path, json=body or {}, headers=self.headers)

    def job(self, path, body=None):
        response = self.post(path, body)
        self.assertEqual(response.status_code, 200, response.text)
        return await_job(self.service, response.json()["job_id"])

    def test_job_history_pages_back_beyond_the_polled_page(self):
        store = self.service.store
        ids = []
        for index in range(5):
            job_id, _ = store.create_job("connect", {"index": index})
            with store.connect() as connection:
                connection.execute("UPDATE jobs SET status='success', created=? WHERE id=?", (1000 + index, job_id))
            ids.append(job_id)
        page = lambda query: [item["id"] for item in self.client.get("/api/jobs?" + query).json()["items"]]
        self.assertEqual(page("before=1005&limit=2"), [ids[4], ids[3]])
        self.assertEqual(page("before=1003&limit=2"), [ids[2], ids[1]])
        self.assertEqual(page("before=1001&limit=2"), [ids[0]])
        self.assertEqual(len(page("limit=100000")), len(page("limit=200")))
        self.assertEqual(self.client.get(f"/api/jobs/{ids[0]}").json()["id"], ids[0])
        self.assertEqual(self.client.get("/api/jobs/missing").status_code, 404)

    def test_authentication_csrf_origin_and_logout(self):
        with TestClient(create_app(service=self.service)) as other:
            self.assertEqual(other.get("/api/config").status_code, 401)
            self.assertEqual(other.get("/api/logs").status_code, 401)
            self.assertEqual(other.get("/").status_code, 200)
        self.assertEqual(self.client.put("/api/config", json={}).status_code, 403)
        response = self.client.put("/api/config", json={}, headers={**self.headers, "Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.post("/api/auth/setup", json={"password": "another-password"}).status_code, 409)
        self.assertEqual(self.post("/auth/logout").status_code, 200)
        self.assertEqual(self.client.get("/api/overview").status_code, 401)
        response = self.client.post("/api/auth/login", json={"password": "test-console-password"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=strict", response.headers["set-cookie"])

    def test_secrets_are_encrypted_and_redacted_from_api_and_logs(self):
        public = self.client.get("/api/overview")
        self.assertNotIn("test-admin-key", public.text)
        self.assertNotIn("upstream_password", {key for key in public.json()["config"] if key != "secrets_set"})
        self.assertTrue(public.json()["config"]["secrets_set"]["upstream_password"])
        self.service.log("ERROR", "test-password test-admin-key sk-sensitive-created-key")
        logs = self.client.get("/api/logs").text
        self.assertNotIn("test-password", logs)
        self.assertNotIn("test-admin-key", logs)
        self.assertNotIn("sk-sensitive-created-key", logs)
        for name in ("flow.sqlite3", "flow.sqlite3-wal"):
            path = self.service.directory / name
            if path.exists():
                self.assertNotIn(b"test-password", path.read_bytes())
                self.assertNotIn(b"test-console-password", path.read_bytes())

    def test_connection_uses_supplied_password_and_does_not_replace_saved_config(self):
        first = self.job("/jobs/connect", {"side": "upstream"})
        self.assertEqual(first["status"], "success")
        bad = self.job("/jobs/connect", {"side": "upstream", "settings": {"upstream_password": "wrong-password"}})
        self.assertEqual(bad["status"], "failed")
        self.assertEqual(self.service.store.get("config")["upstream_password"], "test-password")
        good = self.job("/jobs/connect", {"side": "target"})
        self.assertEqual(good["status"], "success")

    def test_sync_and_preview_show_real_model_and_rate_and_reuse_keys(self):
        preview = self.job("/jobs/sync", {"preview": True})
        self.assertEqual(preview["status"], "success")
        self.assertFalse(self.sites.fake.mutations())
        synced = self.job("/jobs/sync")
        self.assertEqual(synced["status"], "success")
        groups = self.client.get("/api/groups").json()["items"]
        active = next(group for group in groups if group["status"] == "active")
        self.assertEqual(active["models"], ["model-a"])
        self.assertEqual(active["local_rate"], 0.25)
        self.assertIn("[sub2api-sync:", active["marker"])
        self.assertEqual(len([group for group in groups if group["status"] == "excluded"]), 2)
        self.job("/jobs/sync")
        self.assertEqual(len(self.sites.fake.keys), 1)

    def test_batch_creation_skips_existing_and_exports_only_encrypted_credentials(self):
        payload = {"users": [{"email": "alice@example.com"},
                             {"email": "new@example.com", "username": "=formula", "password": "new-secret-password"}],
                   "balance": 12.5, "concurrency": 3, "allowed_groups": [1], "request_key": "test-batch-stable"}
        job = self.job("/jobs/batch-users", payload)
        self.assertEqual(job["status"], "success")
        self.assertEqual(job["result"]["skipped"], ["alice@example.com"])
        self.assertEqual(len(job["result"]["created"]), 1)
        self.assertNotIn("new-secret-password", json.dumps(job))
        self.assertEqual(self.sites.fake.users[-1]["role"], "user")
        self.assertEqual(self.sites.fake.users[-1]["balance"], 12.5)
        csv = self.client.get(f"/api/jobs/{job['id']}/credentials.csv")
        self.assertEqual(csv.status_code, 200)
        self.assertIn("new-secret-password", csv.text)
        self.assertIn("'=formula", csv.text)
        second = self.post("/jobs/batch-users", payload).json()["job_id"]
        self.assertEqual(second, job["id"])
        self.assertEqual(len(self.sites.fake.users), 4)
        self.assertNotIn("new-secret-password", self.client.get("/api/logs").text)
        self.assertNotIn(b"new-secret-password", self.service.store.path.read_bytes())
        self.assertTrue(self.sites.fake.user_mutations[-1][3].startswith("flow-create-"))

    def test_batch_rejects_duplicate_email_or_short_password(self):
        self.assertEqual(self.post("/jobs/batch-users", {"users": [{"email": "a@example.com"}, {"email": "A@example.com"}]}).status_code, 400)
        self.assertEqual(self.post("/jobs/batch-users", {"users": [{"email": "a@example.com", "password": "123"}]}).status_code, 400)
        self.assertEqual(len(self.sites.fake.users), 3)

    def test_monthly_reset_at_shanghai_midnight_sets_selected_absolute_balance_once(self):
        with patch("relaydesk.service.utcnow", return_value=datetime(2026, 1, 15, tzinfo=timezone.utc)):
            rule = self.service.save_quota_rule({"enabled": True, "scope": "selected", "user_ids": [2], "amount": 100})
        self.assertEqual(rule["next_run_at"], "2026-01-31T16:00:00+00:00")
        self.service.tick(datetime(2026, 1, 31, 15, 59, 59, tzinfo=timezone.utc))
        self.assertFalse(self.sites.fake.user_mutations)
        due = datetime(2026, 1, 31, 16, tzinfo=timezone.utc)
        self.service.tick(due)
        job = self.service.store.jobs()[0]
        result = await_job(self.service, job["id"])
        self.assertEqual(result["result"]["period"], "2026-02")
        self.assertEqual(self.sites.fake.users[1]["balance"], 100)
        self.assertEqual(self.sites.fake.users[2]["balance"], 23)
        self.assertEqual(self.sites.fake.users[0]["balance"], 500)
        self.sites.fake.users[1]["balance"] = 93
        self.service.tick(due)
        duplicate = self.service.submit("quota_reset", {"rule": rule, "period": "2026-02"})
        await_job(self.service, duplicate)
        self.assertEqual(self.sites.fake.users[1]["balance"], 93)
        self.assertEqual(len(self.sites.fake.user_mutations), 1)

    def test_zero_balance_uses_update_all_employees_excludes_admin(self):
        job = self.job("/jobs/reset-balances", {"confirm": True, "rule": {"amount": 0, "scope": "all"}})
        self.assertEqual(job["status"], "success")
        self.assertEqual([user["balance"] for user in self.sites.fake.users], [500, 0, 0])
        self.assertTrue(all(method == "PUT" for method, _, _, _ in self.sites.fake.user_mutations))

    def test_selected_admin_is_reset_and_manual_reset_requires_confirmation(self):
        self.assertEqual(self.post("/jobs/reset-balances", {"rule": {"scope": "all_members", "amount": 12}}).status_code, 400)
        self.job("/jobs/reset-balances", {"confirm": True, "rule": {"scope": "selected", "user_ids": [1], "amount": 12}})
        self.assertEqual(self.sites.fake.users[0]["balance"], 12)
        self.assertEqual(self.sites.fake.users[1]["balance"], 7.5)

    def test_all_members_includes_admins_and_all_employees_excludes_them(self):
        self.job("/jobs/reset-balances", {"confirm": True, "rule": {"scope": "all_members", "amount": 9}})
        self.assertEqual([user["balance"] for user in self.sites.fake.users], [9, 9, 9])
        self.job("/jobs/reset-balances", {"confirm": True, "rule": {"scope": "all_employees", "amount": 4}})
        self.assertEqual([user["balance"] for user in self.sites.fake.users], [9, 4, 4])
        self.assertEqual(self.client.put("/api/quota", json={"scope": "all"}, headers=self.headers).json()["scope"], "all_employees")

    def test_legacy_include_admins_rule_is_migrated(self):
        rule = self.service.save_quota_rule({"scope": "all", "include_admins": True})
        self.assertEqual(rule["scope"], "all_members")
        self.assertNotIn("include_admins", rule)
        self.service.store.put("quota_rule", {**rule, "scope": "all", "include_admins": False})
        self.service.close()
        self.service = Service(self.root / "data", start_scheduler=False)
        self.assertEqual(self.service.store.get("quota_rule")["scope"], "all_employees")
        self.assertNotIn("include_admins", self.service.store.get("quota_rule"))

    def test_uncertain_delivery_is_not_repeated_after_balance_spending(self):
        self.sites.fake.uncertain_users = {2}
        rule = {**self.service.store.get("quota_rule"), "amount": 100, "scope": "selected", "user_ids": [2]}
        first = await_job(self.service, self.service.submit("quota_reset", {"rule": rule, "period": "2026-02"}))
        self.assertEqual(first["status"], "partial")
        self.assertEqual(self.service.store.receipts()[0]["state"], "uncertain")
        self.sites.fake.users[1]["balance"] = 90
        self.sites.fake.uncertain_users.clear()
        await_job(self.service, self.service.submit("quota_reset", {"rule": rule, "period": "2026-02"}))
        self.assertEqual(self.sites.fake.users[1]["balance"], 90)
        self.assertEqual(len(self.sites.fake.user_mutations), 1)

    def test_scheduled_definite_failure_retries_only_failed_members(self):
        rule = self.service.save_quota_rule({"enabled": True, "scope": "all", "amount": 100})
        self.sites.fake.fail_users = {2}
        period = datetime.now(timezone.utc).astimezone(ZoneInfo(rule["timezone"])).strftime("%Y-%m")
        job = await_job(self.service, self.service.submit("quota_reset", {"rule": rule, "period": period}))
        self.assertEqual(job["status"], "partial")
        end = time.time() + 2
        while not self.service.store.get("quota_retry") and time.time() < end: time.sleep(.01)
        self.assertIsNotNone(self.service.store.get("quota_retry"))
        self.sites.fake.users[2]["balance"] = 91
        self.sites.fake.fail_users.clear()
        self.service.tick(datetime.fromtimestamp(time.time() + 301, timezone.utc))
        retry = next(item for item in self.service.store.jobs() if item["id"] != job["id"])
        await_job(self.service, retry["id"])
        self.assertEqual(self.sites.fake.users[1]["balance"], 100)
        self.assertEqual(self.sites.fake.users[2]["balance"], 91)

    def test_manual_reset_request_key_prevents_double_submission(self):
        body = {"confirm": True, "rule": {"scope": "all", "amount": 5}, "request_key": "same-reset-intent"}
        first = self.job("/jobs/reset-balances", body)
        self.sites.fake.users[1]["balance"] = 3
        second = self.post("/jobs/reset-balances", body).json()["job_id"]
        self.assertEqual(first["id"], second)
        self.assertEqual(self.sites.fake.users[1]["balance"], 3)

    def test_restart_resumes_pending_monthly_job_and_skips_success_and_uncertain(self):
        rule = {**self.service.store.get("quota_rule"), "amount": 77, "scope": "all"}
        store = self.service.store
        job_id, _ = store.create_job("quota_reset", {"rule": rule, "period": "2026-02"})
        store.claim_receipt("2026-02", 2, job_id)
        self.service.close()
        self.service = Service(self.root / "data", start_scheduler=False)
        result = await_job(self.service, job_id)
        self.assertEqual(result["result"]["skipped"], [2])
        self.assertEqual(self.sites.fake.users[1]["balance"], 7.5)
        self.assertEqual(self.sites.fake.users[2]["balance"], 77)
        self.assertEqual(next(receipt for receipt in store.receipts() if receipt["user_id"] == 2)["state"], "uncertain")

    def test_catchup_only_current_month_after_downtime(self):
        with patch("relaydesk.service.utcnow", return_value=datetime(2026, 1, 5, tzinfo=timezone.utc)):
            self.service.save_quota_rule({"enabled": True, "scope": "all", "amount": 15})
        self.service.tick(datetime(2026, 4, 3, tzinfo=timezone.utc))
        job = await_job(self.service, self.service.store.jobs()[0]["id"])
        self.assertEqual(job["result"]["period"], "2026-04")
        self.assertEqual(len(self.service.store.receipts()), 2)
        self.assertEqual(self.service.store.get("quota_rule")["next_run_at"], "2026-04-30T16:00:00+00:00")

    def test_invalid_settings_and_rule_are_rejected_without_writes(self):
        for values in ({"interval_minutes": 0}, {"target_base": "file:///c:/test"}, {"upstream_auth_mode": []}, {"request_interval": 61}):
            self.assertEqual(self.client.put("/api/config", json=values, headers=self.headers).status_code, 400)
        for values in ({"enabled": True}, {"timezone": "invalid/zone"}, {"scope": []}, {"amount": -1}):
            self.assertEqual(self.client.put("/api/quota", json=values, headers=self.headers).status_code, 400)
        response = self.client.put("/api/config", json={"upstream_password": ""}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.store.get("config")["upstream_password"], "test-password")

    def test_connection_config_is_frozen_while_task_runs(self):
        entered, release = threading.Event(), threading.Event()
        original = self.service._refresh_users

        def blocked():
            entered.set()
            release.wait(5)
            return original()

        with patch.object(self.service, "_refresh_users", side_effect=blocked):
            job_id = self.service.submit("refresh_users")
            self.assertTrue(entered.wait(2))
            try:
                with self.assertRaises(SyncError):
                    self.service.save_config({"target_base": "http://other.example.com"})
                self.service.save_config({"interval_minutes": 15})
            finally:
                release.set()
            self.assertEqual(await_job(self.service, job_id)["status"], "success")

    def test_month_boundary_handles_year_and_dst(self):
        self.assertEqual(next_month(datetime(2026, 12, 10, tzinfo=timezone.utc), "Asia/Shanghai").isoformat(), "2026-12-31T16:00:00+00:00")
        self.assertEqual(next_month(datetime(2026, 2, 15, tzinfo=timezone.utc), "America/New_York").hour, 5)
        self.assertEqual(next_month(datetime(2026, 3, 15, tzinfo=timezone.utc), "America/New_York").hour, 4)

    def test_platform_top_n_and_low_cost_configuration_validation(self):
        config = self.client.get("/api/config").json()
        self.assertEqual(config["sync_platforms"], ["openai"])
        for patch_value in ({"sync_platforms": "openai"}, {"sync_platforms": ["fake"]},
                            {"sync_top_n": -1}, {"sync_top_n": True}, {"sync_top_scope": []},
                            {"low_cost_score_minutes": 0}, {"low_cost_health_minutes": 0}, {"low_cost_enabled": "yes"}):
            response = self.client.put("/api/config", json=patch_value, headers=self.headers)
            self.assertEqual(response.status_code, 400, patch_value)
        response = self.client.put("/api/config", json={"sync_platforms": ["openai", "gemini"],
            "sync_top_n": 2, "sync_top_scope": "global", "low_cost_score_minutes": 2}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sync_top_n"], 2)

    def test_low_cost_exclude_keywords_validation_and_normalization(self):
        self.assertEqual(self.client.get("/api/config").json()["low_cost_exclude_keywords"], [])
        for value in ("测试", [1], [None], ["x" * 51], [f"k{index}" for index in range(51)]):
            response = self.client.put("/api/config", json={"low_cost_exclude_keywords": value}, headers=self.headers)
            self.assertEqual(response.status_code, 400, value)
        response = self.client.put("/api/config", json={"low_cost_exclude_keywords": [" 测试 ", "", "Azure", "azure", "公益"]},
                                   headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["low_cost_exclude_keywords"], ["测试", "Azure", "公益"])
        document = json.loads((self.service.directory / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(document["config"]["low_cost_exclude_keywords"], ["测试", "Azure", "公益"])
        self.service.store.put("low_cost_schedule", {"next_rank": 9e12, "next_health": 9e12})
        self.service.save_config({"low_cost_exclude_keywords": ["Alpha"]})
        self.assertLess(self.service.store.get("low_cost_schedule")["next_rank"], 9e12)

    def test_low_cost_scoring_settings_validation(self):
        config = self.client.get("/api/config").json()
        self.assertEqual((config["low_cost_top_n"], config["low_cost_score_minutes"], config["low_cost_rank_tolerance"]), (10, 10, 2))
        self.assertEqual({key: config["low_cost_weight_" + key] for key in DEFAULT_WEIGHTS}, DEFAULT_WEIGHTS)
        zero_weights = {"low_cost_weight_" + key: 0 for key in DEFAULT_WEIGHTS}
        for value in ({"low_cost_top_n": 0}, {"low_cost_top_n": 101}, {"low_cost_top_n": True}, {"low_cost_top_n": 2.5},
                      {"low_cost_score_minutes": 0}, {"low_cost_score_minutes": 1441},
                      {"low_cost_rank_tolerance": -1}, {"low_cost_rank_tolerance": 51}, {"low_cost_rank_tolerance": 1.5},
                      {"low_cost_weight_price": -1}, {"low_cost_weight_price": 101}, {"low_cost_weight_cache": "x"},
                      zero_weights, {"low_cost_rank_hours": 2}):
            self.assertEqual(self.client.put("/api/config", json=value, headers=self.headers).status_code, 400, value)
        response = self.client.put("/api/config", json={"low_cost_top_n": 25, "low_cost_score_minutes": 15,
                                                        "low_cost_rank_tolerance": 0, **zero_weights, "low_cost_weight_price": 100},
                                   headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["low_cost_weight_price"], 100)
        self.assertEqual(self.client.get("/api/overview").json()["low_cost_weight_defaults"], DEFAULT_WEIGHTS)
        self.assertEqual(self.client.post("/api/jobs/low-cost", json={"mode": "rate"}, headers=self.headers).status_code, 400)

    def test_best_source_settings_validation(self):
        config = self.client.get("/api/config").json()
        self.assertEqual((config["best_source_enabled"], config["best_source_name"], config["best_source_check_minutes"],
                          config["best_source_keep_top"], config["best_source_retry_minutes"]), (False, "最优源站 OpenAI", 10, 10, 1))
        for value in ({"best_source_check_minutes": 0}, {"best_source_check_minutes": 1441}, {"best_source_keep_top": 0},
                      {"best_source_keep_top": 101}, {"best_source_retry_minutes": 0}, {"best_source_enabled": "yes"},
                      {"best_source_name": " "}, {"best_source_name": "最低价 OpenAI"}):
            self.assertEqual(self.client.put("/api/config", json=value, headers=self.headers).status_code, 400, value)
        response = self.client.put("/api/config", json={"best_source_name": " 源站优选 ", "best_source_keep_top": 5},
                                   headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["best_source_name"], "源站优选")

    def test_best_source_job_schedule_and_overview(self):
        self.service.save_config({"best_source_enabled": True, "best_source_check_minutes": 7})
        job = self.job("/jobs/best-source")
        self.assertEqual(job["status"], "success", job)
        self.assertEqual((job["result"]["upstream_id"], job["result"]["provider_rank"]), (7, 1))
        overview = self.client.get("/api/overview").json()
        self.assertEqual(overview["best_source"]["status"], "active")
        self.assertAlmostEqual(overview["best_source_schedule"]["next_rank"] - time.time(), 7 * 60, delta=5)
        self.assertEqual(overview["stats"]["active"], 1)
        when = datetime.now(timezone.utc)
        self.service.store.put("best_source_schedule", {"next_rank": 0})
        self.service.store.put("low_cost_schedule", {"next_rank": when.timestamp() + 3600, "next_health": when.timestamp() + 3600})
        with patch.object(self.service, "submit", return_value="mock-job") as submit:
            self.service.tick(when)
            submit.assert_called_once_with("best_source", {"mode": "ranking"})
        self.assertEqual(self.service.store.get("best_source_schedule")["next_rank"], when.timestamp() + 60)

    def test_best_source_disabled_after_openai_is_deselected(self):
        self.service.save_config({"best_source_enabled": True})
        self.job("/jobs/best-source")
        self.service.save_config({"sync_platforms": ["gemini"]})
        self.assertLessEqual(self.service.store.get("best_source_schedule")["next_rank"], time.time())
        with patch.object(self.service, "submit", return_value="mock-job") as submit:
            self.service.tick(datetime.now(timezone.utc))
            submit.assert_any_call("best_source", {"mode": "ranking"})

    def test_obsolete_low_cost_settings_are_removed_on_startup(self):
        config = self.service.store.get("config")
        config.update(low_cost_rank_hours=3, low_cost_rate_check_enabled=True, low_cost_rate_rise_percent=20)
        config.pop("low_cost_score_minutes")
        self.service.store.put("config", config)
        self.service.close()
        self.service = Service(self.root / "data", start_scheduler=False)
        saved = json.loads((self.service.directory / "config.json").read_text(encoding="utf-8"))["config"]
        self.assertNotIn("low_cost_rank_hours", saved)
        self.assertNotIn("low_cost_rate_check_enabled", saved)
        self.assertEqual(saved["low_cost_score_minutes"], 10)

    def test_low_cost_job_reports_scores_for_candidates(self):
        self.service.save_config({"low_cost_enabled": True})
        job = self.job("/jobs/low-cost")
        self.assertEqual(job["status"], "success", job)
        result = job["result"]
        self.assertEqual(result["score_rank"], 1)
        self.assertIsNotNone(result["score"])
        scored = [row for row in result["candidates"] if row["eligible"]]
        self.assertTrue(all(row["score"] is not None and row["parts"] for row in scored))
        self.assertEqual([row["score_rank"] for row in scored], list(range(1, len(scored) + 1)))

    def test_low_cost_job_and_independent_schedules(self):
        self.service.save_config({"low_cost_enabled": True, "low_cost_score_minutes": 120, "low_cost_health_minutes": 3})
        job = self.job("/jobs/low-cost")
        self.assertEqual(job["status"], "success", job)
        self.assertEqual(job["result"]["upstream_id"], 7)
        overview = self.client.get("/api/overview").json()
        schedule = overview["low_cost_schedule"]
        self.assertAlmostEqual(schedule["next_rank"] - schedule["next_health"], 7200 - 180, delta=1)
        self.assertEqual(overview["low_cost"]["status"], "active")
        when = datetime.now(timezone.utc)
        self.service.store.put("low_cost_schedule", {"next_rank": when.timestamp() + 7200, "next_health": 0})
        with patch.object(self.service, "submit", return_value="mock-job") as submit:
            self.service.tick(when)
            submit.assert_called_once_with("low_cost", {"mode": "health"})
        self.assertFalse(self.sites.fake.user_mutations)

    def test_existing_settings_migrate_and_keep_retained_key_mapping(self):
        self.job("/jobs/sync")
        previous = self.service.config().state_file.read_bytes()
        config = self.service.store.get("config")
        for field in ("sync_platforms", "sync_top_n", "sync_top_scope", "low_cost_enabled", "low_cost_score_minutes",
                      "low_cost_exclude_keywords"):
            config.pop(field)
        self.service.store.put("config", config)
        self.service.close()
        self.service = Service(self.root / "data", start_scheduler=False)
        self.assertEqual(self.service.public_config()["sync_platforms"], ["openai"])
        self.assertEqual(self.service.public_config()["low_cost_exclude_keywords"], [])
        self.assertEqual(self.service.config().state_file.read_bytes(), previous)
        self.assertEqual(len(self.sites.fake.keys), 1)

    def test_low_cost_all_intervals_persist_to_file_and_reschedule(self):
        self.service.save_config({"low_cost_enabled": True})
        self.job("/jobs/low-cost")
        patch_value = {"low_cost_score_minutes": 180, "low_cost_health_minutes": 7, "low_cost_retry_minutes": 4}
        response = self.client.put("/api/config", json=patch_value, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        document = json.loads((self.service.directory / "config.json").read_text(encoding="utf-8"))
        for field, value in patch_value.items():
            self.assertEqual(document["config"][field], value)
        schedule = self.service.store.get("low_cost_schedule")
        self.assertLessEqual(schedule["next_rank"], time.time())
        self.assertLessEqual(schedule["next_health"], time.time())
        self.job("/jobs/low-cost")
        schedule = self.service.store.get("low_cost_schedule")
        self.assertAlmostEqual(schedule["next_rank"] - schedule["next_health"], 3 * 3600 - 7 * 60, delta=1)
        for value in (0, True, 1441):
            self.assertEqual(self.client.put("/api/config", json={"low_cost_retry_minutes": value}, headers=self.headers).status_code, 400)

    def test_low_cost_pending_key_and_failed_reads_use_configured_retry(self):
        self.service.save_config({"low_cost_enabled": True, "low_cost_score_minutes": 180,
                                  "low_cost_health_minutes": 7, "low_cost_retry_minutes": 4})
        self.sites.fake.hourly_key_limit = True
        job = self.job("/jobs/low-cost")
        self.assertEqual(job["result"]["pending"], 1)
        schedule = self.service.store.get("low_cost_schedule")
        self.assertAlmostEqual(schedule["next_rank"] - time.time(), 4 * 60, delta=2)
        self.assertAlmostEqual(schedule["next_health"] - time.time(), 4 * 60, delta=2)
        when = datetime.now(timezone.utc)
        self.service.store.put("low_cost_schedule", {"next_rank": 0, "next_health": 0})
        with patch.object(self.service, "submit", return_value="mock-job"):
            self.service.tick(when)
        schedule = self.service.store.get("low_cost_schedule")
        self.assertEqual(schedule["next_rank"], when.timestamp() + 4 * 60)
        self.assertEqual(schedule["next_health"], when.timestamp() + 4 * 60)

    def test_interval_changes_during_detection_are_used_for_the_next_schedule(self):
        self.service.save_config({"low_cost_enabled": True})
        entered, release = threading.Event(), threading.Event()
        original = LowCostSelector.reconcile

        def blocked(selector, snapshot, **kwargs):
            entered.set()
            release.wait(5)
            return original(selector, snapshot, **kwargs)

        with patch.object(LowCostSelector, "reconcile", new=blocked):
            job_id = self.service.submit("low_cost")
            self.assertTrue(entered.wait(3))
            try:
                self.service.save_config({"low_cost_score_minutes": 180, "low_cost_health_minutes": 7,
                                          "low_cost_retry_minutes": 4})
            finally:
                release.set()
            self.assertEqual(await_job(self.service, job_id)["status"], "success")
        schedule = self.service.store.get("low_cost_schedule")
        self.assertAlmostEqual(schedule["next_rank"] - schedule["next_health"], 3 * 3600 - 7 * 60, delta=1)


if __name__ == "__main__":
    unittest.main()
