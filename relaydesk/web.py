"""Authenticated HTTP interface; all remote work runs on the durable job worker."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from sync_sub2api import SyncError
from .rules import CADENCE
from .service import Service

COOKIE = "flow_session"
STATIC = Path(__file__).parent / "static"


def password_hash(password: str, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return {"salt": salt, "digest": digest}


def create_app(directory=None, *, service=None, start_scheduler=True):
    root = Path(__file__).resolve().parent.parent
    owned = service is None

    @asynccontextmanager
    async def lifespan(app):
        app.state.service = service or Service(Path(directory or root / "data" / "web"), start_scheduler)
        yield
        if owned:
            app.state.service.close()

    app = FastAPI(title="Flow 同步工作台", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    attempts = defaultdict(deque)
    attempts_lock = threading.Lock()

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.exception_handler(SyncError)
    async def sync_error(request, error):
        return JSONResponse({"error": request.app.state.service.redact(error)}, status_code=400)

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        return JSONResponse({"error": error.detail}, status_code=error.status_code)

    @app.middleware("http")
    async def security(request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin:
                parsed = urlsplit(origin)
                if parsed.netloc.lower() != request.url.netloc.lower() or parsed.scheme != request.url.scheme:
                    return JSONResponse({"error": "请求来源无效，请从工作台页面操作"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"error": "请求来源无效"}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                return JSONResponse({"error": "请使用 JSON 请求"}, status_code=415)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 524288:
                    return JSONResponse({"error": "请求内容过大"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def current(request: Request):
        manager = request.app.state.service
        token = request.cookies.get(COOKIE, "")
        csrf = manager.store.session(token) if token else None
        if not csrf:
            raise HTTPException(401, "请先登录工作台")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not hmac.compare_digest(
                csrf, request.headers.get("x-csrf-token", "")):
            raise HTTPException(403, "会话校验失败，请刷新页面后重试")
        return manager

    async def payload(request):
        try:
            value = await request.json()
        except (ValueError, UnicodeError):
            raise HTTPException(400, "JSON 内容无效") from None
        if not isinstance(value, dict):
            raise HTTPException(400, "请求内容必须是对象")
        return value

    def session_response(request, manager):
        previous = request.cookies.get(COOKIE)
        if previous:
            manager.store.logout(previous)
        token, csrf = manager.store.new_session()
        response = JSONResponse({"authenticated": True, "setup_needed": False, "csrf": csrf})
        response.set_cookie(COOKIE, token, max_age=43200, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", path="/")
        return response

    @app.get("/api/auth/status")
    def auth_status(request: Request):
        manager = request.app.state.service
        csrf = manager.store.session(request.cookies.get(COOKIE, ""))
        return {"setup_needed": manager.store.get("admin") is None, "authenticated": bool(csrf),
                "csrf": csrf}

    @app.post("/api/auth/setup")
    async def setup(request: Request):
        manager = request.app.state.service
        value = (await payload(request)).get("password")
        if not isinstance(value, str) or not 10 <= len(value) <= 256:
            raise HTTPException(400, "工作台密码需要 10–256 个字符")
        if manager.store.get("admin") is not None:
            raise HTTPException(409, "工作台已设置密码，请登录")
        if not manager.store.setup_admin(password_hash(value)):
            raise HTTPException(409, "工作台已完成初始化，请登录")
        manager.log("INFO", "工作台管理员密码已设置")
        return session_response(request, manager)

    @app.post("/api/auth/login")
    async def login(request: Request):
        manager = request.app.state.service
        value = (await payload(request)).get("password", "")
        address = request.client.host if request.client else "local"
        with attempts_lock:
            if len(attempts) > 10000:
                attempts.clear()
            queue = attempts[address]
            while queue and queue[0] < time.time() - 300:
                queue.popleft()
            if len(queue) >= 8:
                raise HTTPException(429, "尝试次数过多，请 5 分钟后再试")
            queue.append(time.time())
        stored = manager.store.get("admin")
        if not stored or not isinstance(value, str) or len(value) > 256 or not hmac.compare_digest(
                password_hash(value, stored["salt"])["digest"], stored["digest"]):
            raise HTTPException(401, "工作台密码不正确")
        with attempts_lock:
            attempts.pop(address, None)
        return session_response(request, manager)

    @app.post("/api/auth/logout")
    def logout(request: Request, manager=Depends(current)):
        manager.store.logout(request.cookies.get(COOKIE, ""))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/api/overview")
    def overview(manager=Depends(current)):
        return manager.overview()

    @app.get("/api/config")
    def config(manager=Depends(current)):
        return manager.public_config()

    @app.put("/api/config")
    async def save_config(request: Request, manager=Depends(current)):
        return manager.save_config(await payload(request))

    @app.get("/api/groups")
    def groups(manager=Depends(current)):
        return {"items": manager.store.get("groups", [])}

    @app.get("/api/users")
    def users(manager=Depends(current)):
        return {"items": manager.store.get("users", []), "updated": manager.store.get("users_updated")}

    @app.get("/api/jobs")
    def jobs(before: float | None = None, limit: int = 100, manager=Depends(current)):
        return {"items": manager.store.jobs(max(1, min(limit, 200)), before)}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str, manager=Depends(current)):
        found = manager.store.job(job_id)
        if not found:
            raise HTTPException(404, "任务不存在")
        return found

    @app.post("/api/jobs/connect")
    async def connect(request: Request, manager=Depends(current)):
        body = await payload(request)
        if body.get("side") not in ("upstream", "target"):
            raise SyncError("请选择上游或本地连接")
        if not isinstance(body.get("settings", {}), dict):
            raise SyncError("连接设置格式无效")
        manager.merge_config(body.get("settings", {}))
        return {"job_id": manager.submit("connect", {"side": body["side"], "settings": body.get("settings", {})})}

    @app.post("/api/jobs/sync")
    async def sync(request: Request, manager=Depends(current)):
        body = await payload(request)
        return {"job_id": manager.submit("preview" if body.get("preview") is True else "sync")}

    @app.post("/api/jobs/refresh-users")
    def refresh_users(manager=Depends(current)):
        return {"job_id": manager.submit("refresh_users")}

    async def stable_check(request, manager, strategy):
        body = await payload(request)
        mode, platform = body.get("mode", "ranking"), body.get("platform", "openai")
        if mode not in {entry[0] for entry in CADENCE[strategy]}:
            raise SyncError("检测模式无效")
        if f"{strategy}:{platform}" not in manager._rules():
            raise SyncError("请先在连接配置中添加并保存这个优选分组")
        return {"job_id": manager.submit(strategy, {"mode": mode, "platform": platform})}

    @app.post("/api/jobs/low-cost")
    async def low_cost(request: Request, manager=Depends(current)):
        return await stable_check(request, manager, "low_cost")

    @app.post("/api/jobs/best-source")
    async def best_source(request: Request, manager=Depends(current)):
        return await stable_check(request, manager, "best_source")

    @app.post("/api/jobs/batch-users")
    async def batch_users(request: Request, manager=Depends(current)):
        body = await payload(request)
        valid = manager.validate_batch(body)
        key = body.get("request_key")
        if key is not None and (not isinstance(key, str) or not 8 <= len(key) <= 100):
            raise SyncError("批次请求标识无效")
        return {"job_id": manager.submit("batch_users", valid, "batch:" + key if key else None)}

    @app.get("/api/jobs/{job_id}/credentials.csv")
    def export(job_id: str, manager=Depends(current)):
        if len(job_id) != 32 or not all(char in "0123456789abcdef" for char in job_id):
            raise HTTPException(404, "批次不存在")
        return Response(manager.export_csv(job_id), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="flow-members-{job_id[:8]}.csv"'})

    @app.get("/api/quota")
    def quota(manager=Depends(current)):
        return {"rule": manager.store.get("quota_rule"), "receipts": manager.store.receipts(200)}

    @app.put("/api/quota")
    async def save_quota(request: Request, manager=Depends(current)):
        return manager.save_quota_rule(await payload(request))

    @app.post("/api/jobs/reset-balances")
    async def reset_balances(request: Request, manager=Depends(current)):
        body = await payload(request)
        if body.get("confirm") is not True or not isinstance(body.get("rule"), dict):
            raise SyncError("请确认本次重置的成员范围和金额")
        rule = manager.save_quota_rule(body["rule"], persist=False)
        if rule["scope"] == "selected" and not rule["user_ids"]:
            raise SyncError("请至少选择一位成员")
        key = body.get("request_key")
        if key is not None and (not isinstance(key, str) or not 8 <= len(key) <= 100):
            raise SyncError("重置请求标识无效")
        return {"job_id": manager.submit("quota_reset", {"rule": rule}, "manual-reset:" + key if key else None)}

    @app.get("/api/logs")
    def logs(after: int = 0, level: str = "", search: str = "", manager=Depends(current)):
        if level not in {"", "INFO", "WARNING", "ERROR"}:
            raise SyncError("日志级别无效")
        return {"items": manager.store.logs(max(0, after), level or None, search[:200])}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
