"""Local HTTP integration tests for employee management and the Web console."""

import copy
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from relaydesk.service import Service, next_month
from relaydesk.low_cost import LowCostSelector
from relaydesk.rules import new_rule
from relaydesk.scoring import DEFAULT_WEIGHTS
from relaydesk.web import create_app
from sync_sub2api import SyncError, progress, progress_request
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
        # Model distribution per member, as /admin/dashboard/models reports it.
        self.usage = {
            2: [{"model": "claude-sonnet-4-5", "requests": 30, "input_tokens": 9000, "output_tokens": 3000,
                 "cache_creation_tokens": 0, "cache_read_tokens": 6000, "total_tokens": 18000,
                 "cost": 80.0, "actual_cost": 72.5, "account_cost": 40.0},
                {"model": "gpt-5", "requests": 10, "input_tokens": 1000, "output_tokens": 1000,
                 "cache_creation_tokens": 0, "cache_read_tokens": 0, "total_tokens": 2000,
                 "cost": 25.0, "actual_cost": 20.0, "account_cost": 9.0}],
        }
        self.usage_queries = []
        self.usage_status = 200
        self.timeline = []

    def handle(self, method, raw_path, headers, body):
        parsed = urlsplit(raw_path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path == "/up/api/v1/auth/login" and body.get("password") != "test-password":
            return 401, None
        if path == "/down/api/v1/admin/dashboard/models":
            if headers.get("x-api-key") != "test-admin-key":
                return 401, None
            self.usage_queries.append({key: values[0] for key, values in query.items()})
            self.timeline.append(("usage", int(query["user_id"][0])))
            if self.usage_status != 200:
                return self.usage_status, None
            return 200, {"models": copy.deepcopy(self.usage.get(int(query["user_id"][0]), [])),
                         "start_date": query["start_date"][0], "end_date": query["end_date"][0]}
        if path.startswith("/down/api/v1/admin/users"):
            if headers.get("x-api-key") != "test-admin-key":
                return 401, None
            if method == "GET":
                page, size = int(query.get("page", [1])[0]), int(query.get("page_size", [100])[0])
                return 200, {"items": copy.deepcopy(self.users[(page - 1) * size:page * size]), "total": len(self.users)}
            self.user_mutations.append((method, path, copy.deepcopy(body), headers.get("Idempotency-Key")))
            if "/users/" in path:
                self.timeline.append(("reset", int(path.split("/users/")[1].split("/")[0])))
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

    def test_running_job_shows_its_step_and_pending_request(self):
        store = self.service.store
        job_id, _ = store.create_job("sync", {})
        store.set_job(job_id, "running")
        self.service.context.job_id = job_id  # as on the worker thread
        try:
            progress("同步分组 「Alpha」#7", 2, 5)
            progress_request("target", "PUT", "/admin/groups/3?page=1")
        finally:
            self.service.context.job_id = None
        shown = self.client.get(f"/api/jobs/{job_id}").json()["progress"]
        self.assertEqual({key: shown[key] for key in ("step", "done", "total", "request")},
                         {"step": "同步分组 「Alpha」#7", "done": 2, "total": 5, "request": "本地 PUT /admin/groups/3"})
        listed = next(item for item in self.client.get("/api/jobs").json()["items"] if item["id"] == job_id)
        self.assertEqual(listed["progress"]["step"], shown["step"])
        self.assertEqual(store.logs(job_id=job_id), [])  # progress is not written to the log
        store.set_job(job_id, "success")
        self.assertNotIn("progress", self.client.get(f"/api/jobs/{job_id}").json())

    def test_job_logs_list_only_that_jobs_lines(self):
        other = self.job("/jobs/refresh-users")
        sync = self.job("/jobs/sync")
        lines = self.client.get(f"/api/jobs/{sync['id']}/logs").json()["items"]
        self.assertEqual(lines[0]["message"], "开始同步可用分组")
        self.assertIn(lines[-1]["message"], {"任务完成", "任务部分完成，请查看明细"})
        self.assertEqual({line["job_id"] for line in lines}, {sync["id"]})
        newer = self.client.get(f"/api/jobs/{sync['id']}/logs?after={lines[-2]['id']}").json()["items"]
        self.assertEqual(newer, lines[-1:])
        self.assertTrue(self.client.get(f"/api/jobs/{other['id']}/logs").json()["items"])
        self.assertEqual(self.client.get("/api/jobs/missing/logs").status_code, 404)

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
        store.save_reset_summary(job_id, 2, {"period": "2026-02", "state": "running", "amount": 77,
                                             "since": "2026-01-01", "until": "2026-02-01", "balance": 7.5}, {})
        self.service.close()
        self.service = Service(self.root / "data", start_scheduler=False)
        result = await_job(self.service, job_id)
        self.assertEqual(result["result"]["skipped"], [2])
        self.assertEqual(self.sites.fake.users[1]["balance"], 7.5)
        self.assertEqual(self.sites.fake.users[2]["balance"], 77)
        self.assertEqual(next(receipt for receipt in store.receipts() if receipt["user_id"] == 2)["state"], "uncertain")
        # The member whose reset was cut off keeps its summary, marked for review.
        _, items = self.service.store.reset_summary(job_id)
        self.assertEqual({item["user_id"]: item["state"] for item in items}, {2: "uncertain", 3: "success"})

    def test_reset_first_records_each_members_balance_usage_and_models(self):
        now = datetime(2026, 3, 12, 4, tzinfo=timezone.utc)  # 12:00 in Shanghai
        with patch("relaydesk.service.utcnow", return_value=now):
            job = self.job("/jobs/reset-balances", {"confirm": True, "rule": {"scope": "selected", "user_ids": [2, 3], "amount": 100}})
        self.assertEqual(job["status"], "success")
        fake = self.sites.fake
        # Each member is summarized right before that member's own reset.
        self.assertEqual(fake.timeline, [("usage", 2), ("reset", 2), ("usage", 3), ("reset", 3)])
        self.assertEqual(fake.usage_queries[0], {"user_id": "2", "start_date": "2026-03-01", "end_date": "2026-03-12",
                                                 "timezone": "Asia/Shanghai"})
        self.assertEqual(job["result"]["usage"], {"users": 2, "balance": 30.5, "cost": 92.5, "requests": 40,
                                                  "tokens": 20000, "missing": 0})
        listed = self.client.get("/api/quota").json()["summaries"]
        self.assertEqual(len(listed), 1)
        self.assertEqual({key: listed[0][key] for key in ("job_id", "users", "succeeded", "balance", "cost", "requests",
                                                          "tokens", "missing", "since", "until", "amount")},
                         {"job_id": job["id"], "users": 2, "succeeded": 2, "balance": 30.5, "cost": 92.5,
                          "requests": 40, "tokens": 20000, "missing": 0, "since": "2026-03-01",
                          "until": "2026-03-12", "amount": 100})
        detail = self.client.get(f"/api/quota/summaries/{job['id']}").json()
        alice, bob = detail["items"]
        self.assertEqual((alice["email"], alice["balance"], alice["cost"], alice["state"]), ("alice@example.com", 7.5, 92.5, "success"))
        self.assertEqual([model["model"] for model in alice["models"]], ["claude-sonnet-4-5", "gpt-5"])
        self.assertEqual((alice["usage"]["cost"], alice["usage"]["cache_read_tokens"]), (105.0, 6000))
        self.assertEqual((bob["email"], bob["balance"], bob["cost"], bob["models"]), ("bob@example.com", 23, 0, []))
        self.assertIn("重置前 $7.5，本期消费 $92.50", "\n".join(line["message"] for line in self.service.store.logs(job_id=job["id"])))
        self.assertEqual(self.client.get("/api/quota/summaries/" + "0" * 32).status_code, 404)

    def test_monthly_summary_covers_the_closed_month_then_starts_at_the_last_reset(self):
        rule = {**self.service.store.get("quota_rule"), "amount": 50, "scope": "selected", "user_ids": [2]}
        with patch("relaydesk.service.utcnow", return_value=datetime(2026, 1, 31, 16, 0, 5, tzinfo=timezone.utc)):
            await_job(self.service, self.service.submit("quota_reset", {"rule": rule, "period": "2026-02"}))
        self.assertEqual((self.sites.fake.usage_queries[-1]["start_date"], self.sites.fake.usage_queries[-1]["end_date"]),
                         ("2026-01-01", "2026-02-01"))
        last = self.service.store.last_reset(2)
        later = datetime.fromtimestamp(last, timezone.utc) + timedelta(days=3)
        with patch("relaydesk.service.utcnow", return_value=later):
            await_job(self.service, self.service.submit("quota_reset", {"rule": rule}))
        zone = ZoneInfo("Asia/Shanghai")
        self.assertEqual((self.sites.fake.usage_queries[-1]["start_date"], self.sites.fake.usage_queries[-1]["end_date"]),
                         (datetime.fromtimestamp(last, zone).date().isoformat(), later.astimezone(zone).date().isoformat()))

    def test_reset_goes_ahead_when_usage_cannot_be_read(self):
        self.sites.fake.usage_status = 404
        job = self.job("/jobs/reset-balances", {"confirm": True, "rule": {"scope": "all_employees", "amount": 8}})
        self.assertEqual(job["status"], "success")
        self.assertEqual([user["balance"] for user in self.sites.fake.users], [500, 8, 8])
        # A site without the endpoint is asked once, not once per member.
        self.assertEqual(len(self.sites.fake.usage_queries), 1)
        self.assertEqual(job["result"]["usage"]["missing"], 2)
        _, items = self.service.store.reset_summary(job["id"])
        self.assertEqual([(item["balance"], item["cost"], item["usage_error"]) for item in items],
                         [(7.5, None, "本地站点不支持按成员查询用量"), (23, None, "本地站点不支持按成员查询用量")])
        warnings = [line["message"] for line in self.service.store.logs(job_id=job["id"], level="WARNING")]
        self.assertEqual(warnings, ["本地站点不支持按成员查询用量（/admin/dashboard/models），本次只记录重置前余额"])
        self.sites.fake.usage_status = 500
        job = self.job("/jobs/reset-balances", {"confirm": True, "rule": {"scope": "all_employees", "amount": 9}})
        self.assertEqual([user["balance"] for user in self.sites.fake.users], [500, 9, 9])
        self.assertEqual(len(self.sites.fake.usage_queries), 3)
        _, items = self.service.store.reset_summary(job["id"])
        self.assertTrue(all("HTTP 500" in item["usage_error"] for item in items))
        self.assertEqual(self.client.get("/api/quota").json()["summaries"][0]["missing"], 2)

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

    def rules(self, *rules):
        return self.service.save_config({"stable_groups": [dict(rule) for rule in rules]})

    def stable(self, key):
        return next(entry for entry in self.client.get("/api/overview").json()["stable_groups"] if entry["key"] == key)

    def test_each_platform_has_its_own_top_n(self):
        config = self.client.get("/api/config").json()
        self.assertEqual((config["sync_platform_limits"], config["stable_groups"]), ({"openai": 0}, []))
        for patch_value in ({"sync_platform_limits": ["openai"]}, {"sync_platform_limits": {"fake": 0}},
                            {"sync_platform_limits": {"openai": -1}}, {"sync_platform_limits": {"openai": True}},
                            {"sync_platform_limits": {"openai": 10001}}, {"sync_platforms": ["openai"]}, {"sync_top_n": 2}):
            response = self.client.put("/api/config", json=patch_value, headers=self.headers)
            self.assertEqual(response.status_code, 400, patch_value)
        response = self.client.put("/api/config", json={"sync_platform_limits": {"gemini": 3, "openai": 0}}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.json()["sync_platform_limits"].items()), [("openai", 0), ("gemini", 3)])
        self.assertEqual(self.service.config().sync_limits, {"openai": 0, "gemini": 3})

    def test_price_sync_platforms_setting(self):
        self.assertEqual(self.client.get("/api/config").json()["price_sync_platforms"], ["deepseek", "zhipu", "kimi"])
        for value in ("deepseek", ["fake"], [1], None):
            response = self.client.put("/api/config", json={"price_sync_platforms": value}, headers=self.headers)
            self.assertEqual(response.status_code, 400, value)
        response = self.client.put("/api/config", json={"price_sync_platforms": ["kimi", "openai", "kimi"]}, headers=self.headers)
        self.assertEqual(response.json()["price_sync_platforms"], ["openai", "kimi"])
        self.assertEqual(self.service.config().price_sync_platforms, {"openai", "kimi"})
        self.assertEqual(self.job("/jobs/sync")["result"]["pricing_updated"], 0)

    def test_preferred_group_rules_validation_and_defaults(self):
        low, best = new_rule("low_cost", "openai"), new_rule("best_source", "openai")
        invalid = ["x", [{"strategy": "cheap", "platform": "openai"}], [{"strategy": "low_cost", "platform": "fake"}],
                   [low, low], [{**low, "name": "A"}, {**best, "platform": "deepseek", "name": "A"}],
                   [{**low, "name": " "}], [{**low, "enabled": "yes"}], [{**low, "unknown": 1}], [{**best, "top_n": 5}],
                   [{**low, "top_n": 0}], [{**low, "top_n": 101}], [{**low, "top_n": True}], [{**low, "top_n": 2.5}],
                   [{**low, "score_minutes": 0}], [{**low, "score_minutes": 1441}], [{**low, "health_minutes": 0}],
                   [{**low, "retry_minutes": 0}], [{**low, "rank_tolerance": -1}], [{**low, "rank_tolerance": 51}],
                   [{**low, "weights": {**DEFAULT_WEIGHTS, "price": 101}}], [{**low, "weights": dict.fromkeys(DEFAULT_WEIGHTS, 0)}],
                   [{**low, "weights": {"price": 1}}], [{**low, "require_detection": 1}], [{**low, "exclude_keywords": "测试"}],
                   [{**low, "exclude_keywords": ["x" * 51]}], [{**low, "exclude_keywords": [f"k{index}" for index in range(51)]}],
                   [{**best, "keep_top": 0}], [{**best, "keep_top": 101}], [{**best, "check_minutes": 1441}]]
        for value in invalid:
            response = self.client.put("/api/config", json={"stable_groups": value}, headers=self.headers)
            self.assertEqual(response.status_code, 400, value)
        response = self.client.put("/api/config", json={"stable_groups": [
            {"strategy": "best_source", "platform": "deepseek", "name": " 源站 DS "},
            {**low, "exclude_keywords": [" 测试 ", "", "Azure", "azure", "公益"]}]}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        saved = response.json()["stable_groups"]
        self.assertEqual(saved[0], new_rule("best_source", "deepseek", name="源站 DS"))
        self.assertEqual(saved[1]["exclude_keywords"], ["测试", "Azure", "公益"])
        document = json.loads((self.service.directory / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(document["config"]["stable_groups"], saved)
        overview = self.client.get("/api/overview").json()
        self.assertEqual([(row["key"], row["label"]) for row in overview["stable_groups"]],
                         [("best_source:deepseek", "最优源站 DeepSeek"), ("low_cost:openai", "最低价 OpenAI")])
        self.assertEqual(overview["low_cost_weight_defaults"], DEFAULT_WEIGHTS)
        self.assertEqual(overview["stable_defaults"]["low_cost"]["weights"], DEFAULT_WEIGHTS)
        self.assertEqual(self.post("/jobs/low-cost", {"mode": "rate"}).status_code, 400)
        self.assertEqual(self.post("/jobs/best-source", {"platform": "gemini"}).status_code, 400)

    def test_best_source_job_schedule_and_overview(self):
        self.rules(new_rule("best_source", "openai", enabled=True, check_minutes=7), new_rule("low_cost", "openai"))
        job = self.job("/jobs/best-source")
        self.assertEqual(job["status"], "success", job)
        self.assertEqual((job["result"]["upstream_id"], job["result"]["provider_rank"]), (7, 1))
        self.assertEqual((job["result"]["platform"], job["result"]["label"]), ("openai", "最优源站 OpenAI"))
        entry = self.stable("best_source:openai")
        self.assertEqual(entry["status"]["status"], "active")
        self.assertAlmostEqual(entry["schedule"]["next_rank"] - time.time(), 7 * 60, delta=5)
        self.assertEqual(self.client.get("/api/overview").json()["stats"]["active"], 1)
        when = datetime.now(timezone.utc)
        self.service.store.put("best_source:openai_schedule", {"next_rank": 0})
        with patch.object(self.service, "submit", return_value="mock-job") as submit:
            self.service.tick(when)
            submit.assert_called_once_with("best_source", {"mode": "ranking", "platform": "openai"})
        self.assertEqual(self.service.store.get("best_source:openai_schedule")["next_rank"], when.timestamp() + 60)

    def test_rules_on_several_platforms_get_their_own_checks(self):
        self.sites.fake.groups[1]["platform"] = "deepseek"
        self.sites.fake.healthy_group_ids = {7, 8}
        self.rules(new_rule("best_source", "openai", enabled=True), new_rule("best_source", "deepseek", enabled=True))
        openai = self.job("/jobs/best-source")
        deepseek = self.job("/jobs/best-source", {"platform": "deepseek"})
        self.assertEqual((openai["result"]["upstream_id"], deepseek["result"]["upstream_id"]), (7, 8))
        self.assertNotEqual(openai["result"]["local_group_id"], deepseek["result"]["local_group_id"])
        self.assertIn("开始检测最优源站 DeepSeek 分组", [row["message"] for row in self.service.store.logs()])
        sync = self.job("/jobs/sync")
        self.assertEqual(set(sync["result"]["stable"]), {"best_source:openai", "best_source:deepseek"})

    def test_removed_rule_stops_its_group_then_is_forgotten(self):
        self.rules(new_rule("best_source", "openai", enabled=True))
        first = self.job("/jobs/best-source")
        self.rules()
        self.assertEqual(self.service.store.get("stable_retired"), ["best_source:openai"])
        self.assertLessEqual(self.service.store.get("best_source:openai_schedule")["next_rank"], time.time())
        self.assertEqual(self.client.get("/api/overview").json()["stable_groups"], [])
        self.assertEqual(self.post("/jobs/best-source").status_code, 400)
        with patch.object(self.service, "submit", return_value="mock-job") as submit:
            self.service.tick(datetime.now(timezone.utc))
            submit.assert_any_call("best_source", {"mode": "ranking", "platform": "openai"})
        stopped = await_job(self.service, self.service.submit("best_source", {"mode": "ranking", "platform": "openai"}))
        self.assertEqual((stopped["status"], stopped["result"]["status"]), ("success", "inactive"))
        group = next(row for row in self.sites.fake.local_groups if row["id"] == first["result"]["local_group_id"])
        self.assertEqual(group["status"], "inactive")
        self.assertEqual(self.service.store.get("stable_retired"), [])
        self.assertIsNone(self.service.store.get("best_source:openai"))
        # Re-adding the rule reuses the same local group and upstream Key.
        self.rules(new_rule("best_source", "openai", enabled=True))
        again = self.job("/jobs/best-source")
        self.assertEqual(again["result"]["local_group_id"], first["result"]["local_group_id"])
        self.assertEqual(len(self.sites.fake.keys), 1)

    def test_legacy_settings_migrate_to_platform_limits_and_rules(self):
        config = self.service.store.get("config")
        for key in ("sync_platform_limits", "stable_groups"):
            config.pop(key)
        config.update(sync_platforms=["openai", "gemini"], sync_top_n=5, sync_top_scope="per_platform",
                      low_cost_enabled=True, low_cost_name="便宜", low_cost_top_n=8, low_cost_weight_price=60,
                      low_cost_exclude_keywords=["测试"], low_cost_rank_hours=3, best_source_keep_top=4)
        self.service.store.put("config", config)
        self.service.store.put("low_cost", {"status": "active", "local_group_id": 5})
        self.service.store.put("low_cost_schedule", {"next_rank": 123})
        self.service.close()
        self.service = Service(self.root / "data", start_scheduler=False)
        saved = json.loads((self.service.directory / "config.json").read_text(encoding="utf-8"))["config"]
        self.assertFalse([key for key in saved if key.startswith(("low_cost_", "best_source_", "sync_top", "sync_platforms"))])
        self.assertEqual(saved["sync_platform_limits"], {"openai": 5, "gemini": 5})
        low, best = saved["stable_groups"]
        self.assertEqual(low, new_rule("low_cost", "openai", enabled=True, name="便宜", top_n=8, exclude_keywords=["测试"],
                                       weights={**DEFAULT_WEIGHTS, "price": 60}))
        self.assertEqual(best, new_rule("best_source", "openai", keep_top=4))
        self.assertEqual(self.service.store.get("low_cost:openai"), {"status": "active", "local_group_id": 5})
        self.assertEqual(self.service.store.get("low_cost:openai_schedule"), {"next_rank": 123})
        self.assertIsNone(self.service.store.get("low_cost"))

    def test_low_cost_job_reports_scores_for_candidates(self):
        self.rules(new_rule("low_cost", "openai", enabled=True))
        job = self.job("/jobs/low-cost")
        self.assertEqual(job["status"], "success", job)
        result = job["result"]
        self.assertEqual(result["score_rank"], 1)
        self.assertIsNotNone(result["score"])
        scored = [row for row in result["candidates"] if row["eligible"]]
        self.assertTrue(all(row["score"] is not None and row["parts"] for row in scored))
        self.assertEqual([row["score_rank"] for row in scored], list(range(1, len(scored) + 1)))

    def test_low_cost_job_and_independent_schedules(self):
        self.rules(new_rule("low_cost", "openai", enabled=True, score_minutes=120, health_minutes=3))
        job = self.job("/jobs/low-cost")
        self.assertEqual(job["status"], "success", job)
        self.assertEqual(job["result"]["upstream_id"], 7)
        entry = self.stable("low_cost:openai")
        self.assertAlmostEqual(entry["schedule"]["next_rank"] - entry["schedule"]["next_health"], 7200 - 180, delta=1)
        self.assertEqual(entry["status"]["status"], "active")
        when = datetime.now(timezone.utc)
        self.service.store.put("low_cost:openai_schedule", {"next_rank": when.timestamp() + 7200, "next_health": 0})
        with patch.object(self.service, "submit", return_value="mock-job") as submit:
            self.service.tick(when)
            submit.assert_called_once_with("low_cost", {"mode": "health", "platform": "openai"})
        self.assertFalse(self.sites.fake.user_mutations)

    def test_existing_settings_migrate_and_keep_retained_key_mapping(self):
        self.job("/jobs/sync")
        previous = self.service.config().state_file.read_bytes()
        config = self.service.store.get("config")
        for field in ("sync_platform_limits", "stable_groups"):
            config.pop(field)
        self.service.store.put("config", config)
        self.service.close()
        self.service = Service(self.root / "data", start_scheduler=False)
        self.assertEqual(self.service.public_config()["sync_platform_limits"], {"openai": 0})
        self.assertEqual([rule["enabled"] for rule in self.service.public_config()["stable_groups"]], [False, False])
        self.assertEqual(self.service.config().state_file.read_bytes(), previous)
        self.assertEqual(len(self.sites.fake.keys), 1)

    def test_low_cost_all_intervals_persist_to_file_and_reschedule(self):
        self.rules(new_rule("low_cost", "openai", enabled=True))
        self.job("/jobs/low-cost")
        rule = new_rule("low_cost", "openai", enabled=True, score_minutes=180, health_minutes=7, retry_minutes=4)
        response = self.client.put("/api/config", json={"stable_groups": [rule]}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        document = json.loads((self.service.directory / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(document["config"]["stable_groups"], [rule])
        schedule = self.service.store.get("low_cost:openai_schedule")
        self.assertLessEqual(schedule["next_rank"], time.time())
        self.assertLessEqual(schedule["next_health"], time.time())
        self.job("/jobs/low-cost")
        schedule = self.service.store.get("low_cost:openai_schedule")
        self.assertAlmostEqual(schedule["next_rank"] - schedule["next_health"], 3 * 3600 - 7 * 60, delta=1)
        for value in (0, True, 1441):
            response = self.client.put("/api/config", json={"stable_groups": [{**rule, "retry_minutes": value}]}, headers=self.headers)
            self.assertEqual(response.status_code, 400)

    def test_low_cost_pending_key_and_failed_reads_use_configured_retry(self):
        self.rules(new_rule("low_cost", "openai", enabled=True, score_minutes=180, health_minutes=7, retry_minutes=4))
        self.sites.fake.hourly_key_limit = True
        job = self.job("/jobs/low-cost")
        self.assertEqual(job["result"]["pending"], 1)
        schedule = self.service.store.get("low_cost:openai_schedule")
        self.assertAlmostEqual(schedule["next_rank"] - time.time(), 4 * 60, delta=2)
        self.assertAlmostEqual(schedule["next_health"] - time.time(), 4 * 60, delta=2)
        when = datetime.now(timezone.utc)
        self.service.store.put("low_cost:openai_schedule", {"next_rank": 0, "next_health": 0})
        with patch.object(self.service, "submit", return_value="mock-job"):
            self.service.tick(when)
        schedule = self.service.store.get("low_cost:openai_schedule")
        self.assertEqual(schedule["next_rank"], when.timestamp() + 4 * 60)
        self.assertEqual(schedule["next_health"], when.timestamp() + 4 * 60)

    def test_interval_changes_during_detection_are_used_for_the_next_schedule(self):
        rule = new_rule("low_cost", "openai", enabled=True)
        other_rule = new_rule("best_source", "deepseek", enabled=True)
        self.rules(rule, other_rule)
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
                with self.assertRaises(SyncError):
                    self.service.submit("low_cost", {"platform": "openai"})
                other = self.service.submit("best_source", {"platform": "deepseek"})
                with self.assertRaises(SyncError):
                    self.rules({**rule, "top_n": 5}, other_rule)
                self.rules({**rule, "score_minutes": 180, "health_minutes": 7, "retry_minutes": 4}, other_rule)
            finally:
                release.set()
            self.assertEqual(await_job(self.service, job_id)["status"], "success")
            await_job(self.service, other)
        schedule = self.service.store.get("low_cost:openai_schedule")
        self.assertAlmostEqual(schedule["next_rank"] - schedule["next_health"], 3 * 3600 - 7 * 60, delta=1)

if __name__ == "__main__":
    unittest.main()
