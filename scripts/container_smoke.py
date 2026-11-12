"""Exercise first-run setup and persisted Web configuration in a disposable container."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

PASSWORD = "flow-container-smoke-password"
UPSTREAM_SECRET = "smoke-upstream-password"
ADMIN_SECRET = "adm_sc_smoke_container_key"
PLATFORM_LIMITS = {"openai": 0, "deepseek": 5}
# Omitted rule fields take the server defaults; weights are sent whole.
STABLE_GROUPS = [
    {"strategy": "low_cost", "platform": "openai", "enabled": False, "name": "最低价 OpenAI", "score_minutes": 15,
     "health_minutes": 7, "retry_minutes": 2,
     "weights": {"price": 55, "rating": 15, "popularity": 15, "cache": 10, "latency": 10, "success": 10}},
    {"strategy": "best_source", "platform": "deepseek", "enabled": False, "name": "最优源站 DeepSeek",
     "check_minutes": 12, "keep_top": 6},
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("initialize", "verify"))
    parser.add_argument("--url", default="http://127.0.0.1:8787")
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, payload=None, method=None, csrf=None):
        headers = {"Content-Type": "application/json"}
        if csrf:
            headers["X-CSRF-Token"] = csrf
        req = urllib.request.Request(args.url + path, data=json.dumps(payload).encode() if payload is not None else None,
                                     headers=headers, method=method)
        try:
            with opener.open(req, timeout=5) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"{method or 'GET'} {path} -> HTTP {error.code}: {error.read().decode(errors='replace')}") from None

    deadline = time.monotonic() + 50
    while True:
        try:
            assert request("/healthz")["status"] == "ok"
            break
        except (OSError, AssertionError):
            if time.monotonic() >= deadline:
                raise RuntimeError("Container did not become ready") from None
            time.sleep(0.25)
    if args.phase == "initialize":
        assert request("/api/auth/status")["setup_needed"]
        csrf = request("/api/auth/setup", {"password": PASSWORD})["csrf"]
        request("/api/config", {"upstream_email": "smoke@example.com", "upstream_password": UPSTREAM_SECRET,
            "target_admin_api_key": ADMIN_SECRET, "prefix": "persisted-smoke-", "sync_enabled": False,
            "sync_platform_limits": PLATFORM_LIMITS, "stable_groups": STABLE_GROUPS},
            method="PUT", csrf=csrf)
    else:
        assert not request("/api/auth/status")["setup_needed"]
        csrf = request("/api/auth/login", {"password": PASSWORD})["csrf"]
    saved = request("/api/config")
    assert saved["prefix"] == "persisted-smoke-"
    assert saved["sync_platform_limits"] == PLATFORM_LIMITS, saved["sync_platform_limits"]
    low, best = saved["stable_groups"]
    assert {key: low[key] for key in STABLE_GROUPS[0]} == STABLE_GROUPS[0], low
    assert low["top_n"] == 10 and low["exclude_keywords"] == [], low
    assert {key: best[key] for key in STABLE_GROUPS[1]} == STABLE_GROUPS[1], best
    assert saved["secrets_set"]["upstream_password"] and saved["secrets_set"]["target_admin_api_key"]
    raw = (args.data_dir / "config.json").read_text(encoding="utf-8")
    assert UPSTREAM_SECRET not in raw and ADMIN_SECRET not in raw
    document = json.loads(raw)
    assert document["config"]["upstream_password"]["encrypted"]
    request("/api/auth/logout", {}, csrf=csrf)
    print(json.dumps({"phase": args.phase, "status": "passed", "persistent_config": True, "encrypted_credentials": True}))


if __name__ == "__main__":
    main()
