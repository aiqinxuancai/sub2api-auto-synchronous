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
        with opener.open(req, timeout=5) as response:
            return json.load(response)

    deadline = time.monotonic() + 50
    while True:
        try:
            assert request("/healthz")["status"] == "ok"
            break
        except (OSError, AssertionError):
            if time.monotonic() >= deadline:
                raise RuntimeError("Container did not become ready") from None
            time.sleep(1)
    if args.phase == "initialize":
        assert request("/api/auth/status")["setup_needed"]
        csrf = request("/api/auth/setup", {"password": PASSWORD})["csrf"]
        request("/api/config", {"upstream_email": "smoke@example.com", "upstream_password": UPSTREAM_SECRET,
            "target_admin_api_key": ADMIN_SECRET, "prefix": "persisted-smoke-", "sync_enabled": False,
            "low_cost_enabled": False, "low_cost_rank_hours": 3, "low_cost_health_minutes": 7,
            "low_cost_retry_minutes": 2}, method="PUT", csrf=csrf)
    else:
        assert not request("/api/auth/status")["setup_needed"]
        csrf = request("/api/auth/login", {"password": PASSWORD})["csrf"]
    saved = request("/api/config")
    assert saved["prefix"] == "persisted-smoke-"
    assert saved["low_cost_rank_hours"] == 3 and saved["low_cost_health_minutes"] == 7
    assert saved["low_cost_retry_minutes"] == 2
    assert saved["secrets_set"]["upstream_password"] and saved["secrets_set"]["target_admin_api_key"]
    raw = (args.data_dir / "config.json").read_text(encoding="utf-8")
    assert UPSTREAM_SECRET not in raw and ADMIN_SECRET not in raw
    document = json.loads(raw)
    assert document["config"]["upstream_password"]["encrypted"]
    request("/api/auth/logout", {}, csrf=csrf)
    print(json.dumps({"phase": args.phase, "status": "passed", "persistent_config": True, "encrypted_credentials": True}))


if __name__ == "__main__":
    main()
