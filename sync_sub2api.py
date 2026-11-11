#!/usr/bin/env python3
"""Synchronize usable AIHub/Sub2API groups, multipliers and model allowlists.

Uses the configuration saved by the Web console. Defaults to a preview; --apply enables writes.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import re
import signal
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from provider_ranking import PLATFORMS, rank_metadata, ranked_items, visible

LOG = logging.getLogger("sub2api-sync")
USER_AGENT = "sub2api-sync/0.1"
MAX_BODY_BYTES = 32 * 1024 * 1024


class SyncError(Exception):
    """A diagnostic that is safe to display without exposing credentials."""


class APIError(SyncError):
    def __init__(self, status: int, method: str, path: str, code: str = "",
                 retry_after: float = 0):
        self.status = status
        self.retry_after = retry_after
        self.code = re.sub(r"[^A-Za-z0-9_-]", "", str(code))[:80]
        hint = ""
        if status == 403:
            hint = "；请检查管理密钥的分组、账号权限，自动创建分组可能需要全权访问"
        elif status == 429:
            hint = "；接口已限流，后续周期会继续处理剩余分组"
        super().__init__(f"{method} {path.split('?')[0]}: HTTP {status} {self.code}{hint}")


def as_int(value: str, name: str, minimum: int = 1) -> int:
    try:
        result = int(value)
    except ValueError:
        raise SyncError(f"{name} 必须为整数") from None
    if result < minimum:
        raise SyncError(f"{name} 必须 >= {minimum}")
    return result


def multiplier(value: Any, name: str = "倍率") -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise SyncError(f"{name} 必须为非负数字") from None
    if not result.is_finite() or result < 0 or result > Decimal("999999.9999"):
        raise SyncError(f"{name} 超出范围")
    return result


def scaled_rate(value: Any, factor: Decimal) -> float:
    rate = multiplier(value) * factor
    rate = rate.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return float(multiplier(rate))


def normalize_base(value: str, name: str) -> tuple[str, str]:
    value = value.rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise SyncError(f"{name} 必须是 http(s) 站点地址，不带账号、查询参数或片段")
    if value.endswith("/api/v1"):
        return value[:-7], value
    return value, value + "/api/v1"


DEFAULT_ACCOUNT_CONCURRENCY = 100
LEGACY_DEFAULT_CONCURRENCY = 3


def account_concurrency(values: dict[str, Any]) -> int:
    """Forwarding-account concurrency from Web settings.

    The setting used to be `concurrency` with a default of 3. An untouched old
    default moves to the new default; a value the user changed is kept.
    """
    if "account_concurrency" in values:
        return values["account_concurrency"]
    legacy = values.get("concurrency")
    if type(legacy) is int and legacy != LEGACY_DEFAULT_CONCURRENCY:
        return legacy
    return DEFAULT_ACCOUNT_CONCURRENCY


@dataclass
class Config:
    upstream_base: str
    target_base: str
    upstream_email: str = ""
    upstream_password: str = field(default="", repr=False)
    upstream_access_token: str = field(default="", repr=False)
    upstream_refresh_token: str = field(default="", repr=False)
    target_admin_api_key: str = field(default="", repr=False)
    target_email: str = ""
    target_password: str = field(default="", repr=False)
    target_access_token: str = field(default="", repr=False)
    target_refresh_token: str = field(default="", repr=False)
    interval: int = 1800
    group_ids: set[int] = field(default_factory=set)
    sync_platforms: set[str] = field(default_factory=lambda: {"openai"})
    sync_top_n: int = 0
    sync_top_scope: str = "per_platform"
    prefix: str = "upstream-"
    rate_factor: Decimal = Decimal("1.0")
    sync_accounts: bool = True
    auto_create_keys: bool = True
    concurrency: int = DEFAULT_ACCOUNT_CONCURRENCY
    priority: int = 50
    dry_run: bool = True
    timeout: int = 30
    state_file: Path = Path("data/sync-state.json")
    report_file: Path = Path("data/last-report.json")
    providers_path: str = "/api/v2/public/providers"
    max_new_keys: int = 50
    disable_unavailable: bool = True
    request_interval: float = 1.1
    rate_limit_retries: int = 2
    upstream_api: str = field(init=False)
    target_api: str = field(init=False)

    def __post_init__(self) -> None:
        self.upstream_base, self.upstream_api = normalize_base(
            self.upstream_base, "UPSTREAM_BASE_URL")
        self.target_base, self.target_api = normalize_base(self.target_base, "TARGET_BASE_URL")
        if self.upstream_api == self.target_api:
            raise SyncError("上游与目标站点不能相同")
        if len(self.prefix) > 50:
            raise SyncError("GROUP_NAME_PREFIX 不能超过 50 个字符")
        if not self.sync_platforms <= set(PLATFORMS):
            raise SyncError("同步平台无效")
        if type(self.sync_top_n) is not int or not 0 <= self.sync_top_n <= 10000:
            raise SyncError("同步前 N 个必须为 0–10000 的整数，0 表示不限制")
        if self.sync_top_scope not in {"per_platform", "global"}:
            raise SyncError("同步数量范围无效")
        if not 0 <= self.request_interval <= 60:
            raise SyncError("REQUEST_MIN_INTERVAL_SECONDS 必须在 0–60 之间")
        if not self.providers_path.startswith("/") or self.providers_path.startswith("//"):
            raise SyncError("PROVIDERS_API_PATH 必须是上游站点内的绝对路径")

    @classmethod
    def from_web(cls, values: dict[str, Any], directory: Path) -> Config:
        identity = "|".join([values["upstream_base"].rstrip("/"), values["upstream_email"],
                             values["target_base"].rstrip("/")])
        profile = hashlib.sha256(identity.encode()).hexdigest()[:16]
        return cls(
            upstream_base=values["upstream_base"], target_base=values["target_base"],
            upstream_email=values["upstream_email"], upstream_password=values["upstream_password"],
            upstream_access_token=values["upstream_access_token"] if values["upstream_auth_mode"] == "session" else "",
            upstream_refresh_token=values["upstream_refresh_token"] if values["upstream_auth_mode"] == "session" else "",
            target_admin_api_key=values["target_admin_api_key"] if values["target_auth_mode"] == "key" else "",
            target_email=values["target_email"], target_password=values["target_password"],
            target_access_token=values["target_access_token"] if values["target_auth_mode"] == "session" else "",
            target_refresh_token=values["target_refresh_token"] if values["target_auth_mode"] == "session" else "",
            interval=values["interval_minutes"] * 60, group_ids=set(values["group_ids"]),
            sync_platforms=set(values.get("sync_platforms", ["openai"])), sync_top_n=values.get("sync_top_n", 0),
            sync_top_scope=values.get("sync_top_scope", "per_platform"),
            prefix=values["prefix"], rate_factor=Decimal(str(values["rate_factor"])),
            sync_accounts=values["sync_accounts"], auto_create_keys=values["auto_create_keys"],
            concurrency=account_concurrency(values), priority=values["priority"], timeout=values["timeout"],
            dry_run=False, state_file=directory / "profiles" / profile / "state.json",
            report_file=directory / "profiles" / profile / "report.json",
            providers_path=values["providers_path"], max_new_keys=values["max_new_keys"],
            disable_unavailable=values["disable_unavailable"], request_interval=values["request_interval"],
        )

    def has_target_auth(self) -> bool:
        return bool(self.target_admin_api_key or self.target_access_token
                    or self.target_refresh_token or (self.target_email and self.target_password))


def write_private_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class State:
    def __init__(self, config: Config):
        self.path = config.state_file
        identity = {"upstream_api": config.upstream_api,
                    "upstream_email": config.upstream_email, "target_api": config.target_api}
        self.data: dict[str, Any] = {
            "version": 1, "identity": identity, "sessions": {}, "groups": {}}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                raise SyncError("状态文件无法读取；请恢复备份或指定新的 STATE_FILE") from None
            if self.data.get("identity") != identity or self.data.get("version") != 1:
                raise SyncError("状态文件属于其他站点或账号；请使用独立的 STATE_FILE")
        if not isinstance(self.data.get("groups"), dict) or not isinstance(
                self.data.get("sessions"), dict):
            raise SyncError("状态文件结构无效")

    def save(self) -> None:
        write_private_json(self.path, self.data)


@contextlib.contextmanager
def state_lock(path: Path):
    """OS releases the lock after a crash; keep the inode to avoid lock-file races."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a+b") as handle:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SyncError("已有同步进程使用此状态文件，本轮退出") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, config: Config, state: State, role: str):
        self.config, self.state, self.role = config, state, role
        self.base = getattr(config, role + "_base")
        self.api = getattr(config, role + "_api")
        self.email = getattr(config, role + "_email")
        self.password = getattr(config, role + "_password")
        self.admin_key = config.target_admin_api_key if role == "target" else ""
        self.opener = urllib.request.build_opener(NoRedirect())
        stored = state.data["sessions"].get(role, {})
        if stored.get("api_url") != self.api or stored.get("email") != self.email:
            stored = {}
        self.access_token = stored.get("access_token") or getattr(config, role + "_access_token")
        self.refresh_token = stored.get("refresh_token") or getattr(config, role + "_refresh_token")
        self.server_time = datetime.now(timezone.utc)
        self.last_request_at = 0.0
        self.idempotency_key = ""

    def _raw(self, method: str, path: str, payload: Any = None,
             headers: dict[str, str] | None = None, site_path: bool = False) -> Any:
        url = (self.base if site_path else self.api) + path
        request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json",
                           "Accept-Language": "zh-CN"}
        request_headers.update(headers or {})
        if self.idempotency_key and method in {"POST", "PUT"}:
            request_headers["Idempotency-Key"] = self.idempotency_key
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        delay = self.config.request_interval - (time.monotonic() - self.last_request_at)
        if delay > 0:
            time.sleep(min(delay, 60))
        self.last_request_at = time.monotonic()
        try:
            with self.opener.open(request, timeout=self.config.timeout) as response:
                raw = response.read(MAX_BODY_BYTES + 1)
                if len(raw) > MAX_BODY_BYTES:
                    raise SyncError("接口响应超过 32 MiB")
                if response.headers.get("Date"):
                    try:
                        self.server_time = parsedate_to_datetime(response.headers["Date"])
                    except (ValueError, TypeError):
                        pass
        except urllib.error.HTTPError as error:
            retry_after = 0.0
            retry_header = error.headers.get("Retry-After", "")
            try:
                retry_after = max(0, float(retry_header))
            except ValueError:
                try:
                    retry_after = max(0, (parsedate_to_datetime(retry_header)
                                          - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError):
                    pass
            try:
                data = json.loads(error.read(65536))
                code = data.get("reason") or data.get("code") or ""
            except (ValueError, AttributeError):
                code = ""
            finally:
                error.close()
            raise APIError(error.code, method, path, code, retry_after) from None
        except (OSError, urllib.error.URLError):
            raise SyncError(f"{self.role}: {method} {path.split('?')[0]} 连接失败或超时") from None
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            raise SyncError(f"{self.role}: {path.split('?')[0]} 未返回 JSON") from None
        if isinstance(data, dict) and "code" in data:
            if data["code"] != 0:
                raise APIError(400, method, path, data.get("reason") or data["code"])
            return data.get("data")
        return data

    def _save_session(self, data: dict[str, Any]) -> None:
        if data.get("requires_2fa"):
            raise SyncError(f"{self.role}: 登录需要二次验证，请导入已登录的 access/refresh token")
        if not data.get("access_token"):
            raise SyncError(f"{self.role}: 登录接口没有返回 access_token")
        self.access_token = data["access_token"]
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        self.state.data["sessions"][self.role] = {
            "api_url": self.api, "email": self.email, "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": time.time() + float(data.get("expires_in", 0)),
        }
        self.state.save()

    def authenticate(self) -> None:
        if self.refresh_token:
            try:
                self._save_session(self._raw("POST", "/auth/refresh",
                                             {"refresh_token": self.refresh_token}))
                return
            except APIError as error:
                if error.status not in {400, 401, 403}:
                    raise
        if self.email and self.password:
            self._save_session(self._raw("POST", "/auth/login",
                                         {"email": self.email, "password": self.password}))
            return
        raise SyncError(f"{self.role}: 缺少登录凭据或会话已过期")

    def _authorized_request(self, method: str, path: str, payload: Any = None,
                            site_path: bool = False) -> Any:
        if self.admin_key:
            return self._raw(method, path, payload, {"x-api-key": self.admin_key}, site_path)
        if not self.access_token:
            self.authenticate()
        try:
            return self._raw(method, path, payload,
                             {"Authorization": "Bearer " + self.access_token}, site_path)
        except APIError as error:
            if error.status != 401:
                raise
            self.authenticate()
            return self._raw(method, path, payload,
                             {"Authorization": "Bearer " + self.access_token}, site_path)

    def request(self, method: str, path: str, payload: Any = None,
                site_path: bool = False) -> Any:
        if self.role == "upstream" and method == "DELETE" and path.startswith("/keys"):
            raise SyncError("上游 Key 保留策略禁止删除密钥")
        for attempt in range(self.config.rate_limit_retries + 1):
            try:
                return self._authorized_request(method, path, payload, site_path)
            except APIError as error:
                if (error.status != 429 or error.code == "API_KEY_CREATE_RATE_LIMITED"
                        or error.retry_after > 60 or attempt == self.config.rate_limit_retries):
                    raise
                delay = max(1, error.retry_after or 60)
                LOG.info("%s %s %s 已限流，%.0f 秒后重试", self.role, method,
                         path.split("?")[0], delay)
                # Retry only explicit 429 rejections, never ambiguous mutation failures.
                time.sleep(delay)
        raise SyncError("限流重试失败")

    def list_all(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen: set[int] = set()
        for page in range(1, 10001):
            separator = "&" if "?" in path else "?"
            data = self.request("GET", f"{path}{separator}page={page}&page_size=100")
            if isinstance(data, list):
                return data
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise SyncError(f"{path}: 列表结构无效，取消同步")
            chunk = data["items"]
            for item in chunk:
                if item["id"] in seen:
                    raise SyncError(f"{path}: 分页重复，取消同步")
                seen.add(item["id"])
                items.append(item)
            if "total" in data:
                if len(items) >= data["total"]:
                    return items
                if not chunk:
                    raise SyncError(f"{path}: 分页不完整，取消同步")
            elif len(chunk) < 100:
                return items
        raise SyncError(f"{path}: 分页数量异常")


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass
class SelectedGroup:
    source: dict[str, Any]
    models: list[str]
    source_rate: float
    local_rate: float
    provider_rank: int = 0
    sync_order: int = 0
    provider_score: float = 0


def select_usable(groups: list[dict[str, Any]], rates: dict[str, Any],
                  providers: dict[str, Any], config: Config,
                  now: datetime) -> tuple[list[SelectedGroup], dict[int, str]]:
    """Use the same model-card health as /providers; unknown is not healthy."""
    if not isinstance(providers, dict) or not isinstance(providers.get("items"), list):
        raise SyncError("供应商大厅接口结构无效，取消同步")
    if providers.get("stale") is True:
        raise SyncError("供应商大厅返回过期快照，本轮保留现有配置")
    items = providers["items"]
    if providers.get("total", len(items)) != len(items):
        raise SyncError("供应商大厅快照不完整，本轮保留现有配置")
    generated = parse_time(providers.get("generated_at"))
    if generated is None or (now - generated).total_seconds() > 600:
        raise SyncError("供应商大厅快照缺少有效时间或已超过 10 分钟")
    providers_by_id: dict[int, dict[str, Any]] = {}
    for item in items:
        group_id = as_int(str(item.get("group_id", "")), "provider group_id")
        if group_id in providers_by_id:
            raise SyncError("供应商大厅返回重复分组 ID")
        providers_by_id[group_id] = item
    ranks = rank_metadata(providers, now)
    ranked = ranked_items(providers, now, config.sync_platforms)
    if config.sync_top_n:
        if config.sync_top_scope == "global":
            allowed_ids = {item["group_id"] for item in ranked[:config.sync_top_n]}
        else:
            allowed_ids = {item["group_id"] for item in ranked
                           if ranks[item["group_id"]]["provider_rank"] <= config.sync_top_n}
    else:
        allowed_ids = {item["group_id"] for item in ranked}
    selected: list[SelectedGroup] = []
    excluded: dict[int, str] = {}
    seen: set[int] = set()
    for group in groups:
        group_id = as_int(str(group.get("id", "")), "group id")
        if group_id in seen:
            raise SyncError("上游可用分组列表包含重复 ID")
        seen.add(group_id)
        reason = ""
        item = providers_by_id.get(group_id)
        if group.get("platform") not in config.sync_platforms:
            reason = "platform_not_selected"
        elif config.group_ids and group_id not in config.group_ids:
            reason = "group_not_selected"
        elif group.get("status") != "active":
            reason = "upstream_inactive"
        elif item is None:
            reason = "not_in_provider_hall"
        elif not visible(item):
            reason = "not_visible_in_hall"
        elif item.get("platform") != group.get("platform"):
            reason = "platform_mismatch"
        elif group_id not in allowed_ids:
            reason = "outside_sync_top_n"
        elif item.get("detections_enabled") is False or item.get("probe_state") != "enabled":
            reason = "monitoring_disabled_or_unknown"
        elif item.get("availability_unknown") is True:
            reason = "availability_unknown"
        elif not parse_time(item.get("probe_expires_at")) or parse_time(
                item.get("probe_expires_at")) <= now:
            reason = "probe_expired"
        if reason:
            excluded[group_id] = reason
            continue
        declared = {m["model"] for m in item.get("group_models", [])
                    if isinstance(m, dict) and isinstance(m.get("model"), str)}
        models = []
        for card in item.get("model_cards", []):
            expires = parse_time(card.get("health_expires_at"))
            model = card.get("model")
            if (model in declared and model not in models and card.get("health") == "healthy"
                    and card.get("probe_state", item.get("probe_state")) == "enabled"
                    and expires is not None and expires > now):
                models.append(model)
        if not models:
            excluded[group_id] = "no_healthy_models"
            continue
        source_rate = multiplier(rates[str(group_id)] if str(group_id) in rates
                                 else group.get("rate_multiplier"))
        selected.append(SelectedGroup(group, sorted(models), float(source_rate),
                                      scaled_rate(source_rate, config.rate_factor), **ranks.get(group_id, {})))
    selected.sort(key=lambda group: group.sync_order)
    return selected, excluded


def bound_group_ids(account: dict[str, Any]) -> list[int]:
    """Sub2API omits `group_ids`/`groups` entirely when an account has no binding."""
    ids = account.get("group_ids")
    if ids is None and isinstance(account.get("groups"), list):
        ids = [group["id"] for group in account["groups"]]
    return sorted(ids or [])


def ensure_account_binding(client: "Client", account: dict[str, Any], local_group_id: int,
                           label: str) -> None:
    """Bind the account to exactly this group and confirm it on the server.

    Some Sub2API versions ignore `group_ids` when creating an account, so an
    account can exist and be enabled while its group still has no account.
    """
    if bound_group_ids(account) == [local_group_id]:
        return
    client.request("PUT", f"/admin/accounts/{account['id']}", {"group_ids": [local_group_id]})
    confirmed = client.request("GET", f"/admin/accounts/{account['id']}")
    if not isinstance(confirmed, dict) or bound_group_ids(confirmed) != [local_group_id]:
        raise SyncError(f"{label}: 账号 {account['id']} 未能绑定到本地分组 {local_group_id}，分组保持停用")
    account["group_ids"] = [local_group_id]


def changed_fields(current: dict[str, Any], desired: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in desired.items() if current.get(key) != value}


class Synchronizer:
    def __init__(self, config: Config, state: State):
        self.config, self.state = config, state
        self.upstream = Client(config, state, "upstream")
        self.target = Client(config, state, "target")
        self.source_id = hashlib.sha256(
            (config.upstream_api + "|" + config.upstream_email).encode()).hexdigest()[:12]
        self.target_id = hashlib.sha256(config.target_api.encode()).hexdigest()[:12]
        self.new_keys = 0
        self.concurrency_fixed = 0
        self.key_creation_blocked = False
        self.keys: list[dict[str, Any]] | None = None

    def marker(self, group_id: int) -> str:
        return f"[sub2api-sync:{self.source_id}:{group_id}]"

    def name(self, group: dict[str, Any]) -> str:
        suffix = f" [{group['id']}]"
        return self.config.prefix + str(group["name"])[:100 - len(self.config.prefix)
                                                      - len(suffix)] + suffix

    def group_payload(self, selected: SelectedGroup) -> dict[str, Any]:
        group = selected.source
        description = self.marker(group["id"])
        if group.get("description"):
            description += "\n" + str(group["description"])
        desired = {
            "name": self.name(group), "description": description,
            "platform": group["platform"], "rate_multiplier": selected.local_rate,
            "status": "active", "model_allowlist": {"enabled": True, "models": selected.models},
        }
        # Preserve the upstream's image/video price definitions. Apply the resale
        # factor to independent media multipliers, not to both prices and multipliers.
        for key in (
            "long_context_pricing_enabled", "allow_image_generation",
            "allow_batch_image_generation", "image_count_billing", "image_rate_independent",
            "video_rate_independent", "batch_image_discount_multiplier",
            "batch_image_hold_multiplier", "image_price_1k", "image_price_2k", "image_price_4k",
            "video_price_480p", "video_price_720p", "video_price_1080p", "allow_messages_dispatch",
            "allow_live",
        ):
            if key in group:
                desired[key] = group[key]
        for key in ("image_rate_multiplier", "video_rate_multiplier"):
            if key in group:
                desired[key] = scaled_rate(group[key], self.config.rate_factor)
        return desired

    def _discover(self, local: list[dict[str, Any]], field_name: str,
                  mapping_name: str) -> dict[int, dict[str, Any]]:
        pattern = re.compile(r"\[sub2api-sync:" + re.escape(self.source_id) + r":(\d+)\]")
        discovered: dict[int, dict[str, Any]] = {}
        for item in local:
            match = pattern.search(item.get(field_name) or "")
            if match:
                group_id = int(match[1])
                if group_id in discovered:
                    raise SyncError(f"上游分组 {group_id} 存在重复来源标记，请先处理重复数据")
                discovered[group_id] = item
        local_ids = {item["id"]: item for item in local}
        for key, mapping in self.state.data["groups"].items():
            local_id = mapping.get(mapping_name)
            if local_id in local_ids and self.marker(int(key)) not in (
                    local_ids[local_id].get(field_name) or ""):
                raise SyncError(f"本地资源 {local_id} 的来源标记已被修改，本轮停止以免覆盖其他配置")
        return discovered

    def _key_is_usable(self, key: dict[str, Any], group_id: int) -> bool:
        expires = parse_time(key.get("expires_at"))
        return (key.get("group_id") == group_id and key.get("status") == "active"
                and (expires is None or expires > self.upstream.server_time)
                and (not key.get("quota") or key.get("quota_used", 0) < key["quota"])
                and not key.get("ip_whitelist") and not key.get("ip_blacklist"))

    def _get_key(self, group_id: int) -> dict[str, Any] | None:
        if self.keys is None:
            self.keys = self.upstream.list_all("/keys")
        name = f"sub2api-sync-{self.target_id}-g{group_id}"
        mapped_id = self.state.data["groups"].get(str(group_id), {}).get("upstream_key_id")
        for stable in ("low_cost", "best_source"):
            mapped_id = mapped_id or self.state.data.get(stable, {}).get("keys", {}).get(str(group_id))
        candidates = [key for key in self.keys if (key.get("id") == mapped_id or key.get("name") == name)
                      and self._key_is_usable(key, group_id)]
        if candidates:
            key = min(candidates, key=lambda item: (item["id"] != mapped_id, item["id"]))
            if not key.get("key") or "*" in key["key"] or "..." in key["key"]:
                key = self.upstream.request("GET", f"/keys/{key['id']}")
        elif (self.config.auto_create_keys and not self.key_creation_blocked
              and self.new_keys < self.config.max_new_keys):
            key = self.upstream.request("POST", "/keys", {"name": name, "group_id": group_id})
            self.keys.append(key)
            self.new_keys += 1
        else:
            return None
        if not isinstance(key.get("key"), str) or not key["key"] or "*" in key["key"]:
            raise SyncError(f"上游分组 {group_id}: 无法取得完整 API Key")
        return key

    def _hold_without_account(self, group: dict[str, Any] | None,
                              account: dict[str, Any] | None) -> None:
        """Keep a group out of routing while no account has selected it."""
        if (group and self.config.sync_accounts and group.get("status") != "inactive"
                and (account is None or bound_group_ids(account) != [group["id"]])):
            # Sub2API creates groups active; a group without accounts only returns 503.
            self.target.request("PUT", f"/admin/groups/{group['id']}", {"status": "inactive"})
            group["status"] = "inactive"

    def _enforce_concurrency(self, accounts: list[dict[str, Any]], report: dict[str, Any]) -> None:
        """Final pass: every account this tool owns, disabled ones included, uses the configured concurrency."""
        owned = re.compile(r"\[sub2api-(?:sync|lowcost|bestsource):" + re.escape(self.source_id) + r":")
        wanted = self.config.concurrency
        for account in accounts:
            current = account.get("concurrency")
            if current == wanted or not owned.search(account.get("notes") or ""):
                continue
            try:
                self.target.request("PUT", f"/admin/accounts/{account['id']}", {"concurrency": wanted})
            except SyncError as error:
                report["errors"].append({"account_id": account["id"],
                                         "error": f"账号 {account['id']} 并发修正失败：{error}"})
                continue
            account["concurrency"] = wanted
            self.concurrency_fixed += 1
            report["actions"].append({"action": "concurrency", "account_id": account["id"],
                                      "from": current, "to": wanted})
            LOG.info("账号 %d 并发 %s → %d", account["id"], current, wanted)

    def _audit_bindings(self, local_groups: list[dict[str, Any]], accounts: list[dict[str, Any]],
                        report: dict[str, Any]) -> None:
        """Final pass: every active managed group must have an active account that selected it."""
        covered = {group_id for account in accounts
                   if account.get("status") == "active" and account.get("schedulable") is not False
                   for group_id in bound_group_ids(account)}
        for upstream_id, group in sorted(self._discover(local_groups, "description", "local_group_id").items()):
            if group.get("status") != "active" or group["id"] in covered:
                continue
            try:
                self.target.request("PUT", f"/admin/groups/{group['id']}", {"status": "inactive"})
            except SyncError as error:
                report["errors"].append({"upstream_id": upstream_id, "error": str(error)})
                continue
            mapping = self.state.data["groups"].setdefault(str(upstream_id), {})
            mapping.update({"last_status": "inactive", "last_reason": "no_bound_account"})
            self.state.save()
            report["actions"].append({"upstream_id": upstream_id, "action": "disable", "reason": "no_bound_account"})
            report["errors"].append({"upstream_id": upstream_id,
                                     "error": f"本地分组 {group['id']} 没有可用的绑定账号，已停用，下轮自动修复"})
            LOG.warning("group=%d: 本地分组 %d 没有可用的绑定账号，已停用，下轮自动修复", upstream_id, group["id"])

    def _sync_account(self, selected: SelectedGroup, local_group_id: int,
                      account: dict[str, Any] | None, key: dict[str, Any]) -> tuple[int, str]:
        group_id = selected.source["id"]
        mapping = self.state.data["groups"].setdefault(str(group_id), {})
        credentials = {"base_url": self.config.upstream_base, "api_key": key["key"],
                       "model_mapping": {model: model for model in selected.models}}
        digest = hashlib.sha256(key["key"].encode()).hexdigest()
        desired = {"name": self.name(selected.source), "notes": self.marker(group_id),
                   "rate_multiplier": selected.source_rate, "concurrency": self.config.concurrency,
                   "status": "active"}
        if account is None:
            payload = {key: value for key, value in desired.items()
                       if key != "status"}
            payload.update({"platform": selected.source["platform"], "type": "apikey",
                            "credentials": credentials, "group_ids": [local_group_id],
                            "priority": self.config.priority, "upstream_billing_probe_enabled": False})
            account = self.target.request("POST", "/admin/accounts", payload)
            mapping["local_account_id"] = account["id"]
            self.state.save()
            action = "created"
        else:
            if account.get("platform") != selected.source["platform"] or account.get("type") != "apikey":
                raise SyncError(f"上游分组 {group_id}: 已有账号平台或类型发生变化")
            patch = changed_fields(account, desired)
            existing_credentials = account.get("credentials") or {}
            if (existing_credentials.get("base_url") != credentials["base_url"]
                    or existing_credentials.get("model_mapping") != credentials["model_mapping"]
                    or mapping.get("key_digest") != digest
                    or account.get("credentials_status", {}).get("has_api_key") is False):
                # Send only the fields we own; the server merges credentials and
                # does not expose secret values in read responses.
                patch["credentials"] = credentials
            if patch:
                self.target.request("PUT", f"/admin/accounts/{account['id']}", patch)
                self.concurrency_fixed += "concurrency" in patch
                action = "updated"
            else:
                action = "unchanged" if bound_group_ids(account) == [local_group_id] else "rebound"
        ensure_account_binding(self.target, account, local_group_id, f"上游分组 {group_id}")
        if account.get("schedulable") is not True:
            self.target.request("POST", f"/admin/accounts/{account['id']}/schedulable",
                                {"schedulable": True})
            if action == "unchanged":
                action = "updated"
        mapping.update({"local_account_id": account["id"], "upstream_key_id": key["id"],
                        "key_digest": digest})
        self.state.save()
        return account["id"], action

    def load_snapshot(self):
        groups = self.upstream.request("GET", "/groups/available")
        rates = self.upstream.request("GET", "/groups/rates")
        if not isinstance(groups, list) or (rates is not None and not isinstance(rates, dict)):
            raise SyncError("上游分组或专属倍率接口结构无效，本轮停止")
        separator = "&" if "?" in self.config.providers_path else "?"
        providers = self.upstream.request(
            "GET", self.config.providers_path + separator + "range=1h&include_media=false",
            site_path=True)
        # Validate the entire snapshot before any caller can change routing.
        select_usable(groups, rates or {}, providers, self.config, self.upstream.server_time)
        return groups, rates or {}, providers

    def cycle(self, *, inspect_only: bool = False, snapshot=None) -> dict[str, Any]:
        groups, rates, providers = snapshot or self.load_snapshot()
        selected, excluded = select_usable(
            groups, rates or {}, providers, self.config, self.upstream.server_time)
        report: dict[str, Any] = {
            "at": datetime.now(timezone.utc).isoformat(), "dry_run": self.config.dry_run,
            "source": self.config.upstream_base, "target": self.config.target_base,
            "snapshot_generated_at": providers.get("generated_at"),
            "visible_groups": len(groups), "usable_groups": len(selected),
            "usable_models": sum(len(group.models) for group in selected),
            "excluded_reasons": dict(Counter(excluded.values())),
            "excluded": [{"upstream_id": group_id, "reason": reason}
                         for group_id, reason in sorted(excluded.items())],
            "groups": [], "actions": [], "errors": [], "pending": 0,
            "warnings": [],
            "platforms": sorted(self.config.sync_platforms), "sync_top_n": self.config.sync_top_n,
            "sync_top_scope": self.config.sync_top_scope,
            "provider_options": providers.get("platform_tabs", list(PLATFORMS)),
            "target_verified": False,
        }
        for group in selected:
            report["groups"].append({"upstream_id": group.source["id"],
                                     "name": self.name(group.source),
                                     "platform": group.source["platform"],
                                     "source_rate": group.source_rate, "local_rate": group.local_rate,
                                     "models": group.models,
                                     "provider_rank": group.provider_rank, "sync_order": group.sync_order,
                                     "provider_score": group.provider_score,
                                     "marker": self.marker(group.source["id"])})
        source_by_id = {group["id"]: group for group in groups}
        for excluded_group in report["excluded"]:
            source = source_by_id.get(excluded_group["upstream_id"], {})
            excluded_group.update({"name": source.get("name", ""),
                                   "platform": source.get("platform", "")})
        LOG.info("上游可见 %d 组；健康可用 %d 组、%d 个模型绑定；排除 %d 组",
                 len(groups), len(selected), report["usable_models"], len(excluded))
        if inspect_only:
            write_private_json(self.config.report_file, report)
            return report
        if not self.config.has_target_auth():
            raise SyncError("请先在 Web 连接配置中保存管理员访问密钥，或本地管理员邮箱和密码")
        local_groups = self.target.list_all("/admin/groups")
        local_accounts = self.target.list_all("/admin/accounts") if self.config.sync_accounts else []
        managed_groups = self._discover(local_groups, "description", "local_group_id")
        managed_accounts = self._discover(local_accounts, "notes", "local_account_id")
        report["target_verified"] = True
        # Recover mappings from server-side markers after a state-file restore.
        if not self.config.dry_run:
            for group_id, local_group in managed_groups.items():
                mapping = self.state.data["groups"].setdefault(str(group_id), {})
                mapping["local_group_id"] = local_group["id"]
                if group_id in managed_accounts:
                    mapping["local_account_id"] = managed_accounts[group_id]["id"]
            self.state.save()
        selected_ids = {group.source["id"] for group in selected}
        # Disable before creating new resources, so a key-creation rate limit
        # cannot delay removing unhealthy resources from local routing.
        if self.config.disable_unavailable or excluded:
            for group_id in sorted(set(managed_groups) | set(managed_accounts)):
                if group_id in selected_ids:
                    continue
                reason = excluded.get(group_id, "no_longer_accessible")
                if reason == "group_not_selected":
                    continue  # The explicit ID list is an operational scope, as in the CLI.
                if not self.config.disable_unavailable and reason not in {
                        "platform_not_selected", "outside_sync_top_n", "group_not_selected"}:
                    continue
                group = managed_groups.get(group_id)
                account = managed_accounts.get(group_id)
                action = {"upstream_id": group_id, "action": "disable", "reason": reason}
                report["actions"].append(action)
                if self.config.dry_run:
                    continue
                try:
                    # Mark the group inactive first: stops all user keys in the group.
                    if group and group.get("status") != "inactive":
                        self.target.request("PUT", f"/admin/groups/{group['id']}", {"status": "inactive"})
                    if account and account.get("status") != "inactive":
                        self.target.request("PUT", f"/admin/accounts/{account['id']}",
                                            {"status": "inactive"})
                    if account and account.get("schedulable") is not False:
                        self.target.request("POST", f"/admin/accounts/{account['id']}/schedulable",
                                            {"schedulable": False})
                    mapping = self.state.data["groups"].setdefault(str(group_id), {})
                    mapping.update({"last_status": "inactive", "last_reason": reason})
                    self.state.save()
                except SyncError as error:
                    report["errors"].append({"upstream_id": group_id, "error": str(error)})
        sorted_groups = {}
        for index, selected_group in enumerate(selected):
            group_id = selected_group.source["id"]
            group = managed_groups.get(group_id)
            account = managed_accounts.get(group_id)
            payload = self.group_payload(selected_group)
            group_patch = changed_fields(group or {}, payload)
            planned_action = "create" if group is None else (
                "enable" if group.get("status") != "active" else "sync")
            action = {"upstream_id": group_id, "action": planned_action,
                      "group_fields": sorted(group_patch), "model_count": len(selected_group.models)}
            report["actions"].append(action)
            if self.config.dry_run:
                continue
            try:
                key = self._get_key(group_id) if self.config.sync_accounts else None
                if self.config.sync_accounts and key is None:
                    action["result"] = "pending_upstream_key"
                    report["pending"] += 1
                    self._hold_without_account(group, account)
                    continue
                if group is None:
                    create_payload = {key: value for key, value in payload.items() if key != "status"}
                    create_payload.update({"subscription_type": "standard", "is_exclusive": False})
                    group = self.target.request("POST", "/admin/groups", create_payload)
                    self.state.data["groups"].setdefault(str(group_id), {})["local_group_id"] = group["id"]
                    self.state.save()
                    action["group_result"] = "created"
                else:
                    if group.get("platform") != selected_group.source["platform"]:
                        raise SyncError(f"上游分组 {group_id}: 平台发生变化，请人工确认映射")
                    # Restore model restrictions and account credentials before enabling.
                    group_patch.pop("status", None)
                    if group_patch:
                        self.target.request("PUT", f"/admin/groups/{group['id']}", group_patch)
                    action["group_result"] = "updated" if group_patch else "unchanged"
                self._hold_without_account(group, account)
                if self.config.sync_accounts:
                    account_id, account_result = self._sync_account(
                        selected_group, group["id"], account, key)
                    action.update({"account_id": account_id, "account_result": account_result})
                if group.get("status") != "active":
                    self.target.request("PUT", f"/admin/groups/{group['id']}", {"status": "active"})
                    action["group_result"] = "enabled"
                mapping = self.state.data["groups"].setdefault(str(group_id), {})
                mapping.update({"local_group_id": group["id"], "last_status": "active",
                                "last_reason": "healthy_models", "models": selected_group.models,
                                "last_rate": selected_group.local_rate,
                                "name": self.name(selected_group.source),
                                "platform": selected_group.source["platform"],
                                "source_rate": selected_group.source_rate})
                sorted_groups[group["id"]] = group
                self.state.save()
                LOG.info("group=%d 分组=%s 账号=%s 模型=%d", group_id,
                         action["group_result"], action.get("account_result", "disabled"),
                         len(selected_group.models))
            except SyncError as error:
                try:
                    self._hold_without_account(group, account)
                except SyncError as hold_error:
                    report["errors"].append({"upstream_id": group_id, "error": str(hold_error)})
                if isinstance(error, APIError) and error.code == "API_KEY_CREATE_RATE_LIMITED":
                    self.key_creation_blocked = True
                    report["pending"] += 1
                    report["warnings"].append({"upstream_id": group_id, "warning": str(error)})
                    LOG.warning("上游 Key 创建额度用尽，剩余新增留待后续周期；继续更新已有分组")
                    continue
                report["errors"].append({"upstream_id": group_id, "error": str(error)})
                LOG.error("group=%d: %s", group_id, error)
                if isinstance(error, APIError) and error.status in {401, 403, 429}:
                    report["pending"] += len(selected) - index
                    break
        if not self.config.dry_run:
            updates = []
            for rank, selected_group in enumerate(selected, 1):
                mapping = self.state.data["groups"].get(str(selected_group.source["id"]), {})
                local_id = mapping.get("local_group_id")
                current = sorted_groups.get(local_id)
                if current is not None and current.get("sort_order") != rank:
                    updates.append({"id": local_id, "sort_order": rank})
            if updates:
                try:
                    self.target.request("PUT", "/admin/groups/sort-order", {"updates": updates})
                    report["sort_updated"] = len(updates)
                except SyncError as error:
                    report["errors"].append({"error": f"分组排序更新失败：{error}"})
        if not self.config.dry_run and self.config.sync_accounts:
            try:
                local_groups = self.target.list_all("/admin/groups")
                accounts = self.target.list_all("/admin/accounts")
                self._enforce_concurrency(accounts, report)
                self._audit_bindings(local_groups, accounts, report)
            except SyncError as error:
                report["errors"].append({"error": f"同步后的账号检查失败：{error}"})
        report["new_upstream_keys"] = self.new_keys
        report["concurrency_fixed"] = self.concurrency_fixed
        write_private_json(self.config.report_file, report)
        LOG.info("%s完成；错误=%d 待后续同步=%d；报告：%s",
                 "预览" if self.config.dry_run else "同步", len(report["errors"]),
                 report["pending"], self.config.report_file)
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent / "data" / "web")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="单次运行（默认）")
    mode.add_argument("--daemon", action="store_true", help="按配置的间隔持续运行")
    mode.add_argument("--inspect-upstream", action="store_true", help="只读取上游，不访问本地管理接口")
    writes = parser.add_mutually_exclusive_group()
    writes.add_argument("--apply", action="store_true", help="实际创建、更新、停用及恢复资源")
    writes.add_argument("--dry-run", action="store_true", help="只预览，禁止同步资源写入")
    parser.add_argument("--group-id", type=int, action="append", help="只处理指定上游 ID，可重复")
    parser.add_argument("--report", type=Path, help="覆盖报告文件路径")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        from relaydesk.storage import Store
        values = Store(args.data_dir).get("config")
        if not values:
            raise SyncError("请先启动 Web 工作台并保存配置，或用 --data-dir 指定已有数据目录")
        config = Config.from_web(values, args.data_dir.resolve())
        config.dry_run = True
        if args.apply:
            config.dry_run = False
        if args.dry_run or args.inspect_upstream:
            config.dry_run = True
        if args.group_id:
            if any(group_id <= 0 for group_id in args.group_id):
                raise SyncError("--group-id 必须为正整数")
            config.group_ids = set(args.group_id)
        if args.report:
            config.report_file = args.report.resolve()
        stop = threading.Event()
        def stop_handler(signum, frame):
            LOG.info("收到退出信号，本轮结束后退出")
            stop.set()
        signal.signal(signal.SIGTERM, stop_handler)
        signal.signal(signal.SIGINT, stop_handler)
        while not stop.is_set():
            started = time.monotonic()
            exit_code = 0
            try:
                with state_lock(config.state_file):
                    state = State(config)
                    report = Synchronizer(config, state).cycle(inspect_only=args.inspect_upstream)
                    exit_code = 1 if report["errors"] else 0
            except SyncError as error:
                LOG.error("%s", error)
                exit_code = 1
            if not args.daemon:
                return exit_code
            delay = max(1, config.interval - (time.monotonic() - started))
            LOG.info("下一轮约 %.0f 秒后执行", delay)
            stop.wait(delay)
        return 0
    except SyncError as error:
        LOG.error("%s", error)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    import sys
    sys.modules.setdefault("sync_sub2api", sys.modules[__name__])
    raise SystemExit(main())
