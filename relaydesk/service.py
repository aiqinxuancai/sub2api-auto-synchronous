from __future__ import annotations

import copy
import csv
import hashlib
import io
import logging
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sync_sub2api import APIError, Client, Config, State, SyncError, Synchronizer, state_lock
from provider_ranking import PLATFORMS

from .storage import Store
from .configuration import SECRET_FIELDS
from .low_cost import BestSourceSelector, LowCostSelector
from .scoring import DEFAULT_WEIGHTS, WEIGHT_SETTINGS

SECRETS = SECRET_FIELDS
SELECTION_DEFAULTS = {"sync_platforms": ["openai"], "sync_top_n": 0, "sync_top_scope": "per_platform",
                      "low_cost_enabled": False, "low_cost_name": "最低价 OpenAI",
                      "low_cost_score_minutes": 10, "low_cost_health_minutes": 5,
                      "low_cost_require_detection": True, "low_cost_retry_minutes": 1,
                      "low_cost_exclude_keywords": [], "low_cost_top_n": 10, "low_cost_rank_tolerance": 2,
                      **{key: DEFAULT_WEIGHTS[metric] for metric, key in WEIGHT_SETTINGS.items()},
                      "best_source_enabled": False, "best_source_name": "最优源站 OpenAI",
                      "best_source_check_minutes": 10, "best_source_keep_top": 10, "best_source_retry_minutes": 1}
# Stable local groups: selector, switch setting, retry setting and (mode, schedule key, interval setting) cadence.
# A "ranking" check also covers every other check, so it refreshes all schedule entries.
STABLE_GROUPS = {
    "low_cost": {"selector": LowCostSelector, "enabled": "low_cost_enabled", "retry": "low_cost_retry_minutes",
                 "label": "最低价 OpenAI", "cadence": (("ranking", "next_rank", "low_cost_score_minutes"),
                                                      ("health", "next_health", "low_cost_health_minutes"))},
    "best_source": {"selector": BestSourceSelector, "enabled": "best_source_enabled", "retry": "best_source_retry_minutes",
                    "label": "最优源站 OpenAI", "cadence": (("ranking", "next_rank", "best_source_check_minutes"),)},
}
# Settings replaced by the composite score; removed from config.json on startup.
OBSOLETE_SETTINGS = {"low_cost_rank_hours", "low_cost_rate_check_enabled", "low_cost_rate_check_minutes",
                     "low_cost_rate_rise_percent"}
MAX_EXCLUDE_KEYWORDS = 50
MAX_KEYWORD_LENGTH = 50
LOW_COST_MODES = {"ranking", "health"}
# selected：左侧勾选的成员（勾选了管理员也会包含）；all_members：所有成员含以后新增；
# all_employees：所有成员含以后新增，但不包括管理员。
QUOTA_SCOPES = {"selected", "all_members", "all_employees"}
LIVE_SETTINGS = {"sync_enabled", "interval_minutes", "low_cost_score_minutes", "low_cost_health_minutes", "low_cost_retry_minutes",
                 "best_source_check_minutes", "best_source_retry_minutes"}


def default_settings():
    return {
        **copy.deepcopy(SELECTION_DEFAULTS),
        "upstream_base": "https://upstream.example.com", "target_base": "http://127.0.0.1:8080",
        "upstream_email": "", "target_email": "", **{key: "" for key in SECRETS},
        "upstream_auth_mode": "login", "target_auth_mode": "key",
        "interval_minutes": 30, "prefix": "upstream-", "rate_factor": 1.0, "group_ids": [],
        "sync_accounts": True, "auto_create_keys": True, "concurrency": 100, "priority": 50,
        "timeout": 30, "providers_path": "/api/v2/public/providers", "max_new_keys": 50,
        "disable_unavailable": True, "request_interval": 1.1, "sync_enabled": False,
    }


def quota_rule_normalized(rule):
    """Map the legacy scope/include_admins pair onto the explicit three-way scope."""
    rule = dict(rule)
    include_admins = rule.pop("include_admins", None)
    if rule.get("scope") == "all":
        rule["scope"] = "all_members" if include_admins is True else "all_employees"
    return rule


def quota_targets(rule, users):
    scope = quota_rule_normalized(rule)["scope"]
    # 旧版任务载荷（升级前已排队/待重试）在 selected 范围下也会排除管理员，保持原语义。
    legacy_skip_admins = scope == "selected" and "include_admins" in rule and rule["include_admins"] is not True
    if scope == "all_members":
        return list(users)
    if scope == "all_employees":
        return [user for user in users if user.get("role") != "admin"]
    selected = set(rule.get("user_ids") or [])
    return [user for user in users if user["id"] in selected
            and not (legacy_skip_admins and user.get("role") == "admin")]


def utcnow():
    return datetime.now(timezone.utc)


def next_month(now: datetime, zone: str) -> datetime:
    local = now.astimezone(ZoneInfo(zone))
    year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
    return datetime(year, month, 1, tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)


def amount(value, name="额度"):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number > 100000000:
            raise ValueError
        return float(number.quantize(Decimal("0.000001")))
    except Exception:
        raise SyncError(f"{name}必须是 0–100000000 之间的金额") from None


def email_address(value):
    value = str(value).strip().lower()
    if len(value) > 254 or not re.fullmatch(r"[^\s@,]+@[^\s@,]+\.[^\s@,]+", value):
        raise SyncError("请填写有效的邮箱地址")
    return value


class MemoryState:
    def __init__(self):
        self.data = {"sessions": {}, "groups": {}}

    def save(self):
        pass


class JobLogHandler(logging.Handler):
    def __init__(self, service):
        super().__init__(logging.INFO)
        self.service = service

    def emit(self, record):
        job_id = getattr(self.service.context, "job_id", None)
        if job_id:
            self.service.log(record.levelname, record.getMessage(), job_id)


class Service:
    def __init__(self, directory: Path, start_scheduler=True):
        self.directory = directory.resolve()
        self.store = Store(self.directory)
        self.context = threading.local()
        self.settings_lock = threading.RLock()
        self.pending_lock = threading.Lock()
        self.pending_kinds = set()
        self.busy = set()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="flow-worker")
        self.stop = threading.Event()
        self.scheduler_thread = None
        self.handler = JobLogHandler(self)
        logging.getLogger("sub2api-sync").addHandler(self.handler)
        logging.getLogger("sub2api-sync").setLevel(logging.INFO)
        recoverable = self.store.recover_jobs()
        if self.store.get("config") is None:
            self._initialize_config()
        values = self.store.get("config")
        migrated = {key: value for key, value in {**copy.deepcopy(SELECTION_DEFAULTS), **values}.items()
                    if key not in OBSOLETE_SETTINGS}
        if migrated != values:
            self.store.put("config", migrated)
        if self.store.get("quota_rule") is None:
            self.store.put("quota_rule", {"enabled": False, "timezone": "Asia/Shanghai",
                                          "amount": 100, "scope": "selected", "user_ids": [],
                                          "next_run_at": None})
        stored_rule = self.store.get("quota_rule")
        if quota_rule_normalized(stored_rule) != stored_rule:
            self.store.put("quota_rule", quota_rule_normalized(stored_rule))
        for job_id in recoverable:
            payload = self.store.job_payload(job_id)
            self.busy.add(job_id)
            self.pending_kinds.add("quota_reset")
            self.store.set_job(job_id, "queued")
            self.pool.submit(self._run_job, job_id, "quota_reset", payload)
        if start_scheduler:
            self.scheduler_thread = threading.Thread(target=self._scheduler, daemon=True)
            self.scheduler_thread.start()

    def close(self):
        self.stop.set()
        if self.scheduler_thread:
            self.scheduler_thread.join(timeout=3)
        self.pool.shutdown(wait=True, cancel_futures=True)
        logging.getLogger("sub2api-sync").removeHandler(self.handler)

    def _initialize_config(self):
        self.store.put("config", default_settings())
        self.store.put("sync_schedule", {"next_run": time.time() + 1800})
        self.log("INFO", "已创建本地配置文件，请在 Web 保存站点连接与同步规则")

    def config(self, values=None):
        values = values or getattr(self.context, "settings", None) or self.store.get("config")
        return Config.from_web(values, self.directory)

    def public_config(self):
        values = self.store.get("config")
        return {**{key: value for key, value in values.items() if key not in SECRETS},
                "secrets_set": {key: bool(values.get(key)) for key in SECRETS}}

    def merge_config(self, patch):
        values = self.store.get("config")
        for key, value in patch.items():
            if key not in values:
                raise SyncError(f"未知配置项：{key}")
            if key in SECRETS and value == "":
                continue
            values[key] = value
        for key in ("interval_minutes", "concurrency", "timeout", "max_new_keys", "priority",
                    "sync_top_n", "low_cost_score_minutes", "low_cost_health_minutes", "low_cost_retry_minutes",
                    "low_cost_top_n", "low_cost_rank_tolerance", *WEIGHT_SETTINGS.values(),
                    "best_source_check_minutes", "best_source_keep_top", "best_source_retry_minutes"):
            if isinstance(values[key], bool) or not isinstance(values[key], int):
                raise SyncError("周期、并发和数量必须为整数")
        if not 1 <= values["interval_minutes"] <= 1440:
            raise SyncError("同步周期应在 1–1440 分钟之间")
        if not 1 <= values["concurrency"] <= 10000 or not 1 <= values["timeout"] <= 120:
            raise SyncError("并发或超时时间超出范围")
        if not 0 <= values["max_new_keys"] <= 1000 or not 0 <= values["priority"] <= 10000:
            raise SyncError("新增 Key 数量或优先级超出范围")
        if not 0 <= values["sync_top_n"] <= 10000:
            raise SyncError("同步前 N 个应在 0–10000 之间，0 表示不限制")
        if values["sync_top_scope"] not in ("per_platform", "global"):
            raise SyncError("同步数量范围无效")
        if not isinstance(values["sync_platforms"], list) or any(
                not isinstance(item, str) or item not in PLATFORMS for item in values["sync_platforms"]):
            raise SyncError("请选择有效的平台")
        values["sync_platforms"] = list(dict.fromkeys(values["sync_platforms"]))
        if not 1 <= values["low_cost_score_minutes"] <= 1440 or not 1 <= values["low_cost_health_minutes"] <= 1440:
            raise SyncError("评分检测与可用性检测间隔应为 1–1440 分钟")
        if not 1 <= values["low_cost_retry_minutes"] <= 1440:
            raise SyncError("失败重试间隔应为 1–1440 分钟")
        if not 1 <= values["low_cost_top_n"] <= 100:
            raise SyncError("最低价候选范围应为前 1–100 名")
        if not 0 <= values["low_cost_rank_tolerance"] <= 50:
            raise SyncError("允许的排名下降应为 0–50 名")
        weights = [values[key] for key in WEIGHT_SETTINGS.values()]
        if any(not 0 <= weight <= 100 for weight in weights) or not sum(weights):
            raise SyncError("评分权重应为 0–100，且至少有一项大于 0")
        if not isinstance(values["low_cost_name"], str) or not 1 <= len(values["low_cost_name"].strip()) <= 100:
            raise SyncError("最低价分组名称需要 1–100 个字符")
        values["low_cost_name"] = values["low_cost_name"].strip()
        if not 1 <= values["best_source_check_minutes"] <= 1440 or not 1 <= values["best_source_retry_minutes"] <= 1440:
            raise SyncError("最优源站检测与重试间隔应为 1–1440 分钟")
        if not 1 <= values["best_source_keep_top"] <= 100:
            raise SyncError("最优源站的保持范围应为前 1–100 名")
        if not isinstance(values["best_source_name"], str) or not 1 <= len(values["best_source_name"].strip()) <= 100:
            raise SyncError("最优源站分组名称需要 1–100 个字符")
        values["best_source_name"] = values["best_source_name"].strip()
        if values["best_source_name"] == values["low_cost_name"]:
            raise SyncError("最优源站与最低价分组需要使用不同的本地分组名称")
        keywords = values["low_cost_exclude_keywords"]
        if not isinstance(keywords, list) or any(not isinstance(item, str) for item in keywords):
            raise SyncError("排除关键字必须为文本列表")
        unique = {}
        for item in keywords:
            item = item.strip()
            if item and item.casefold() not in unique:
                unique[item.casefold()] = item
        if len(unique) > MAX_EXCLUDE_KEYWORDS or any(len(item) > MAX_KEYWORD_LENGTH for item in unique.values()):
            raise SyncError(f"排除关键字最多 {MAX_EXCLUDE_KEYWORDS} 个，每个不超过 {MAX_KEYWORD_LENGTH} 个字符")
        values["low_cost_exclude_keywords"] = list(unique.values())
        if not isinstance(values["group_ids"], list) or any(
                type(item) is not int or item <= 0 for item in values["group_ids"]):
            raise SyncError("分组 ID 必须为正整数列表")
        for key in ("sync_enabled", "sync_accounts", "auto_create_keys", "disable_unavailable",
                    "low_cost_enabled", "low_cost_require_detection", "best_source_enabled"):
            if type(values[key]) is not bool:
                raise SyncError(f"{key} 必须为开关值")
        for key in SECRETS | {"upstream_base", "target_base", "upstream_email", "target_email",
                              "prefix", "providers_path"}:
            if not isinstance(values[key], str):
                raise SyncError("地址与凭据必须为文本")
        if not isinstance(values["upstream_auth_mode"], str) or not isinstance(values["target_auth_mode"], str) or values["upstream_auth_mode"] not in {"login", "session"} or values["target_auth_mode"] not in {"key", "login", "session"}:
            raise SyncError("认证方式无效")
        values["rate_factor"] = amount(values["rate_factor"], "倍率系数")
        values["request_interval"] = amount(values["request_interval"], "请求间隔")
        if values["request_interval"] > 60:
            raise SyncError("请求间隔不能超过 60 秒")
        if not values["providers_path"].startswith("/") or values["providers_path"].startswith("//"):
            raise SyncError("监测接口必须是站点内的路径")
        normalized = self.config(values)
        values["upstream_base"], values["target_base"] = normalized.upstream_base, normalized.target_base
        return values

    def save_config(self, patch):
        with self.settings_lock:
            previous = self.store.get("config")
            values = self.merge_config(patch)
            with self.pending_lock:
                if self.busy and any(values[key] != previous[key] for key in values if key not in LIVE_SETTINGS):
                    raise SyncError("任务执行中，请在完成后修改连接与同步参数")
            self.store.put("config", values)
            if any(previous.get(key) != values[key] for key in ("upstream_base", "upstream_email", "target_base")):
                self.store.put("report", {})
                self.store.put("groups", [])
                self.store.put("users", [])
                self.store.put("connection_upstream", None)
                self.store.put("connection_target", None)
                for kind in STABLE_GROUPS:
                    self.store.put(kind, {})
                    self.store.put(kind + "_schedule", {})
            if previous["target_base"] != values["target_base"]:
                rule = self.store.get("quota_rule")
                rule.update(enabled=False, user_ids=[], next_run_at=None)
                self.store.put("quota_rule", rule)
                self.store.put("quota_retry", None)
            for side in ("upstream", "target"):
                if any(previous[key] != values[key] for key in values if key.startswith(side + "_")):
                    self.store.put("connection_" + side, None)
            if previous["interval_minutes"] != values["interval_minutes"] or (
                    not previous["sync_enabled"] and values["sync_enabled"]):
                self.store.put("sync_schedule", {"next_run": time.time() + values["interval_minutes"] * 60})
            for kind, spec in STABLE_GROUPS.items():
                # Re-check immediately when the group's own policy or its prerequisites change.
                relevant = [key for key in SELECTION_DEFAULTS if key.startswith(kind + "_")] + ["sync_platforms", "sync_accounts"]
                if any(previous.get(key) != values[key] for key in relevant):
                    self.store.put(kind + "_schedule", {entry: time.time() for _, entry, _ in spec["cadence"]})
            self.log("INFO", "连接与同步设置已保存")
        return self.public_config()

    def redact(self, message):
        values = self.store.get("config", {})
        message = str(message)
        for secret in [values.get(key) for key in SECRETS] + getattr(self.context, "secrets", []):
            if secret:
                message = message.replace(secret, "••••••")
        message = re.sub(r"\b(?:adm_sc_|sk-)[A-Za-z0-9_-]{8,}", "••••••", message)
        message = re.sub(r"；报告：[\s\S]*$", "", message)
        return message[:3000]

    def log(self, level, message, job_id=None):
        self.store.log(level, self.redact(message), job_id or getattr(self.context, "job_id", None))

    def submit(self, kind, payload=None, request_key=None):
        if kind not in {"sync", "preview", "connect", "refresh_users", "batch_users", "quota_reset", *STABLE_GROUPS}:
            raise SyncError("未知任务类型")
        payload = copy.deepcopy(payload or {})
        with self.settings_lock, self.pending_lock:
            payload["_settings"] = self.store.get("config")
            singleton = kind in {"sync", "preview", "refresh_users", "quota_reset", *STABLE_GROUPS}
            if singleton and kind in self.pending_kinds:
                raise SyncError("同类任务正在执行，请等待完成")
            job_id, created = self.store.create_job(kind, payload, request_key)
            if not created:
                return job_id
            if singleton:
                self.pending_kinds.add(kind)
            self.busy.add(job_id)
            self.pool.submit(self._run_job, job_id, kind, payload)
        return job_id

    def _run_job(self, job_id, kind, payload):
        self.context.job_id = job_id
        self.context.settings = payload.get("_settings", self.store.get("config"))
        self.context.secrets = [row.get("password") for row in payload.get("users", [])]
        self.context.secrets += [value for key, value in payload.get("settings", {}).items() if key in SECRETS]
        self.store.set_job(job_id, "running")
        self.log("INFO", {"sync": "开始同步可用分组", "preview": "开始同步预览", "connect": "开始连接测试",
                          "refresh_users": "正在读取成员列表", "batch_users": "开始批量创建成员",
                          "low_cost": "开始检测最低价 OpenAI 分组", "best_source": "开始检测最优源站 OpenAI 分组",
                          "quota_reset": "开始重置成员余额"}[kind])
        status, result, job_error = "failed", None, None
        try:
            if kind == "connect":
                result = self._test_connection(payload)
            elif kind in {"sync", "preview"}:
                result = self._sync(kind == "preview")
            elif kind in STABLE_GROUPS:
                result = self._check_stable(kind, payload.get("mode", "ranking"))
            elif kind == "refresh_users":
                result = self._refresh_users()
            elif kind == "batch_users":
                result = self._batch_users(job_id, payload)
            else:
                result = self._reset_balances(job_id, payload)
            status = "partial" if result.get("failed", 0) or result.get("errors") else "success"
            if kind == "quota_reset" and payload.get("period"):
                self._monthly_retry(payload, bool(result.get("retryable")))
            self.log("INFO", "任务完成" if status == "success" else "任务部分完成，请查看明细")
        except SyncError as error:
            status, job_error = "failed", self.redact(error)
            if kind == "connect" and not payload.get("settings"):
                self.store.put("connection_" + payload.get("side", "unknown"), {
                    "ok": False, "tested_at": time.time(), "message": self.redact(error)})
            self.log("ERROR", str(error))
            if kind == "quota_reset" and payload.get("period"):
                self._monthly_retry(payload, True)
        except Exception as error:
            diagnostic = f"执行中断（{type(error).__name__}），请查看配置并重试"
            status, job_error = "failed", diagnostic
            self.log("ERROR", diagnostic)
            logging.getLogger("flow").error("Job %s failed: %s", job_id, type(error).__name__)
        finally:
            with self.pending_lock:
                self.pending_kinds.discard(kind)
                self.busy.discard(job_id)
                self.store.set_job(job_id, status, result=result, error=job_error)
            self.context.job_id = None
            self.context.settings = None
            self.context.secrets = []

    def _test_connection(self, payload):
        side = payload.get("side")
        if side not in ("upstream", "target"):
            raise SyncError("请选择上游或本地连接")
        values = self.merge_config(payload.get("settings", {}))
        config = self.config(values)
        client = Client(config, MemoryState(), side)
        mode = values[side + "_auth_mode"]
        if mode == "login":
            client.access_token, client.refresh_token = "", ""
        started = time.monotonic()
        saved = self.store.get("config")
        matching = all(values[key] == saved[key] for key in values if key.startswith(side + "_"))
        try:
            if side == "upstream":
                groups = client.request("GET", "/groups/available")
                result = {"groups": len(groups), "message": f"连接成功，可读取 {len(groups)} 个分组"}
            else:
                data = client.request("GET", "/admin/groups?page=1&page_size=1")
                result = {"groups": data.get("total", 0), "message": "连接成功，管理员认证有效"}
        except SyncError as error:
            if matching:
                self.store.put("connection_" + side, {"ok": False, "tested_at": time.time(), "message": self.redact(error)})
            raise
        result.update({"side": side, "latency_ms": round((time.monotonic() - started) * 1000),
                       "tested_at": time.time(), "ok": True})
        # An unsaved draft must not overwrite a saved connection's status.
        if matching:
            self.store.put("connection_" + side, result)
        self.log("INFO", result["message"])
        return result

    def _sync(self, preview):
        values = getattr(self.context, "settings", None) or self.store.get("config")
        config = self.config(values)
        config.dry_run = preview
        with state_lock(config.state_file):
            state = State(config)
            engine = Synchronizer(config, state)
            snapshot = engine.load_snapshot()
            stable = {kind: None for kind in STABLE_GROUPS}
            for kind, spec in STABLE_GROUPS.items():
                if not preview and (values[spec["enabled"]] or state.data.get(kind)):
                    fallback = spec["cadence"][-1][0]  # the lightest check when nothing is due
                    stable[kind] = self._reconcile_stable(kind, engine, values, snapshot, self._due_stable_mode(kind) or fallback)
            report = engine.cycle(snapshot=snapshot)
        if not preview:
            self.store.put("report", report)
            self._cache_groups(config, report)
            self.store.put("sync_schedule", {"next_run": time.time() + self.store.get("config")["interval_minutes"] * 60})
        else:
            self.store.put("preview", report)
        return {"usable_groups": report["usable_groups"], "usable_models": report["usable_models"],
                "pending": report["pending"], "errors": report["errors"],
                "warnings": report.get("warnings", []), "preview": preview,
                **stable,
                "new_keys": report.get("new_upstream_keys", 0)}

    def _due_stable_mode(self, kind, now=None):
        """First due check in cadence order; "ranking" comes first because it covers availability too."""
        now = time.time() if now is None else now
        schedule = self.store.get(kind + "_schedule", {})
        return next((mode for mode, entry, _ in STABLE_GROUPS[kind]["cadence"] if schedule.get(entry, 0) <= now), None)

    def _check_stable(self, kind, mode):
        if mode not in {entry[0] for entry in STABLE_GROUPS[kind]["cadence"]}:
            raise SyncError("检测模式无效")
        values = getattr(self.context, "settings", None) or self.store.get("config")
        config = self.config(values)
        with state_lock(config.state_file):
            engine = Synchronizer(config, State(config))
            return self._reconcile_stable(kind, engine, values, engine.load_snapshot(), mode)

    def _reconcile_stable(self, kind, engine, values, snapshot, mode):
        spec = STABLE_GROUPS[kind]
        try:
            result = spec["selector"](engine, values).reconcile(snapshot, mode=mode)
        except SyncError:
            # Keep the previous public report, but show a stopped group after a partial write.
            previous = self.store.get(kind, {})
            mapping = engine.state.data.get(kind, {})
            previous.update({key: value for key, value in mapping.items() if key not in {"keys", "key_digest"}})
            self.store.put(kind, previous)
            raise
        self.store.put(kind, result)
        with self.settings_lock:
            # Intervals can change while work is running; schedule from the latest saved values.
            cadence = self.store.get("config")
            schedule = self.store.get(kind + "_schedule", {})
            now = time.time()
            for check, entry, interval in spec["cadence"]:
                if mode == "ranking" or check == mode:
                    minutes = cadence[interval]
                    if result.get("pending"):
                        minutes = min(minutes, cadence[spec["retry"]])
                    schedule[entry] = now + minutes * 60
            self.store.put(kind + "_schedule", schedule)
        return result

    def _cache_groups(self, config, report):
        state = State(config)
        tagger = Synchronizer(config, state)
        previous = {group["upstream_id"]: group for group in self.store.get("groups", [])}
        rows = {}
        for group in report.get("groups", []):
            group_id = group["upstream_id"]
            mapping = state.data["groups"].get(str(group_id), {})
            rows[group_id] = {**group, "status": "active" if mapping.get("last_status") == "active" else "pending",
                              "local_group_id": mapping.get("local_group_id"),
                              "key_id": mapping.get("upstream_key_id"), "reason": ""}
        exclusions = {group["upstream_id"]: group for group in report.get("excluded", [])}
        for group_id, mapping in state.data["groups"].items():
            group_id = int(group_id)
            if group_id not in rows:
                old = previous.get(group_id, {})
                excluded = exclusions.get(group_id, {})
                rows[group_id] = {"upstream_id": group_id, "name": mapping.get("name") or old.get("name") or excluded.get("name") or f"分组 #{group_id}",
                                  "platform": mapping.get("platform") or old.get("platform") or excluded.get("platform", ""),
                                  "local_rate": mapping.get("last_rate"), "source_rate": mapping.get("source_rate"),
                                  "models": mapping.get("models", []), "status": mapping.get("last_status", "pending"),
                                  "local_group_id": mapping.get("local_group_id"), "key_id": mapping.get("upstream_key_id"),
                                  "reason": excluded.get("reason", mapping.get("last_reason", "")),
                                  "marker": tagger.marker(group_id)}
        for group_id, excluded in exclusions.items():
            if group_id not in rows:
                rows[group_id] = {**excluded, "status": "excluded", "models": [], "local_rate": None,
                                  "source_rate": None, "local_group_id": None, "key_id": None}
        self.store.put("groups", sorted(rows.values(), key=lambda group: (group.get("sync_order", 100000), group["upstream_id"])))

    def _target(self):
        config = self.config()
        return config, Client(config, State(config), "target")

    def _refresh_users(self):
        config, client = self._target()
        with state_lock(config.state_file):
            users = client.list_all("/admin/users")
        clean = [{key: user.get(key) for key in ("id", "email", "username", "role", "balance",
                                                 "concurrency", "status", "allowed_groups", "created_at")}
                 for user in users]
        self.store.put("users", clean)
        self.store.put("users_updated", time.time())
        self.log("INFO", f"已读取 {len(clean)} 位成员")
        return {"count": len(clean)}

    def validate_batch(self, payload):
        rows = payload.get("users")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 200:
            raise SyncError("每批请创建 1–200 位成员")
        balance = amount(payload.get("balance", 0))
        concurrency = payload.get("concurrency", 5)
        if type(concurrency) is not int or not 1 <= concurrency <= 10000:
            raise SyncError("成员并发应在 1–10000 之间")
        seen, users = set(), []
        for row in rows:
            if not isinstance(row, dict):
                raise SyncError("每位成员必须包含邮箱等字段")
            email = email_address(row.get("email", ""))
            if email in seen:
                raise SyncError("批次中有重复邮箱，请先移除")
            seen.add(email)
            password = row.get("password") or secrets.token_urlsafe(15)
            if not isinstance(password, str) or not 8 <= len(password) <= 128:
                raise SyncError("成员密码长度应在 8–128 位之间")
            users.append({"email": email, "username": str(row.get("username", ""))[:80], "password": password})
        allowed_groups = payload.get("allowed_groups", [])
        if not isinstance(allowed_groups, list) or any(type(item) is not int or item <= 0 for item in allowed_groups):
            raise SyncError("成员分组应为有效 ID 列表")
        return {"users": users, "balance": balance, "concurrency": concurrency, "allowed_groups": allowed_groups}

    def _batch_users(self, job_id, payload):
        config, client = self._target()
        created, skipped, failed, credentials = [], [], [], []
        with state_lock(config.state_file):
            existing = {user["email"].lower() for user in client.list_all("/admin/users")}
            for row in payload["users"]:
                email = row["email"]
                if email in existing:
                    skipped.append(email)
                    continue
                try:
                    client.idempotency_key = f"flow-create-{job_id}-{hashlib.sha256(email.encode()).hexdigest()[:16]}"
                    user = client.request("POST", "/admin/users", {
                        **row, "role": "user", "balance": payload["balance"],
                        "concurrency": payload["concurrency"], "allowed_groups": payload["allowed_groups"],
                        "notes": f"[flow-batch:{job_id}]"})
                    created.append({"id": user["id"], "email": email})
                    credentials.append({"email": email, "password": row["password"], "username": row["username"]})
                    self.store.save_artifact(job_id, credentials)
                    self.log("INFO", f"已创建成员 {email}")
                except SyncError as error:
                    failed.append({"email": email, "error": str(error)})
                    self.log("ERROR", f"成员 {email} 创建失败：{error}")
                    if isinstance(error, APIError) and error.status in {401, 403, 429}:
                        failed.extend({"email": pending["email"], "error": "前序请求被拒绝，尚未创建"}
                                      for pending in payload["users"][payload["users"].index(row) + 1:])
                        break
        self._refresh_users()
        return {"created": created, "skipped": skipped, "failures": failed, "failed": len(failed),
                "total": len(payload["users"]), "export_available": bool(credentials)}

    def save_quota_rule(self, patch, persist=True):
        with self.settings_lock:
            return self._quota_rule(patch, persist)

    def _quota_rule(self, patch, persist):
        rule = quota_rule_normalized(self.store.get("quota_rule"))
        if set(patch) - {"enabled", "timezone", "amount", "scope", "user_ids", "include_admins"}:
            raise SyncError("额度规则包含未知字段")
        if "include_admins" in patch and type(patch["include_admins"]) is not bool:
            raise SyncError("启用状态必须为开关值")
        # 兼容旧版页面提交的 scope=all + include_admins。
        patch = quota_rule_normalized(patch)
        previous = copy.deepcopy(rule)
        rule.update(patch)
        if type(rule["enabled"]) is not bool:
            raise SyncError("启用状态必须为开关值")
        try:
            ZoneInfo(rule["timezone"])
        except (ZoneInfoNotFoundError, TypeError, ValueError):
            raise SyncError("请选择有效的 IANA 时区") from None
        rule["amount"] = amount(rule["amount"])
        if not isinstance(rule["scope"], str) or rule["scope"] not in QUOTA_SCOPES:
            raise SyncError("请选择有效的重置范围")
        if not isinstance(rule["user_ids"], list) or any(type(item) is not int or item <= 0 for item in rule["user_ids"]):
            raise SyncError("成员 ID 列表无效")
        rule["user_ids"] = sorted(set(rule["user_ids"]))
        if rule["enabled"] and rule["scope"] == "selected" and not rule["user_ids"]:
            raise SyncError("请至少选择一位需要重置额度的成员")
        if rule["enabled"] and (not previous["enabled"] or previous["timezone"] != rule["timezone"]):
            rule["next_run_at"] = next_month(utcnow(), rule["timezone"]).isoformat()
        if not rule["enabled"]:
            rule["next_run_at"] = None
        if persist:
            self.store.put("quota_rule", rule)
            self.store.put("quota_retry", None)
            self.log("INFO", "每月余额重置规则已启用" if rule["enabled"] else "每月余额重置规则已停用")
        return rule

    def _reset_balances(self, job_id, payload):
        rule = payload["rule"]
        period = payload.get("period") or f"manual-{job_id}"
        config, client = self._target()
        succeeded, skipped, failures = [], [], []
        retryable = False
        with state_lock(config.state_file):
            users = client.list_all("/admin/users")
            selected = quota_targets(rule, users)
            for user in selected:
                user_id = user["id"]
                if not self.store.claim_receipt(period, user_id, job_id):
                    skipped.append(user_id)
                    continue
                try:
                    client.idempotency_key = f"flow-reset-{period}-{user_id}"
                    if rule["amount"] == 0:
                        client.request("PUT", f"/admin/users/{user_id}", {"balance": 0})
                    else:
                        client.request("POST", f"/admin/users/{user_id}/balance", {
                            "balance": rule["amount"], "operation": "set",
                            "notes": f"Flow 余额重置 · {period}"})
                    self.store.receipt(period, user_id, "success", job_id)
                    succeeded.append(user_id)
                    self.log("INFO", f"成员 {user['email']} 余额已重置为 ${rule['amount']:g}")
                except SyncError as error:
                    # Definite HTTP rejection may be retried; uncertain delivery must be reviewed.
                    definite = isinstance(error, APIError) and error.status < 500
                    retryable = retryable or definite
                    self.store.receipt(period, user_id, "failed" if definite else "uncertain", job_id, str(error))
                    failures.append({"user_id": user_id, "error": str(error)})
                    self.log("ERROR", f"成员 {user_id} 额度重置失败：{error}")
        self._refresh_users()
        return {"period": period, "amount": rule["amount"], "succeeded": succeeded,
                "skipped": skipped, "failures": failures, "failed": len(failures), "total": len(selected),
                "retryable": retryable}

    def _monthly_retry(self, payload, needed):
        with self.settings_lock:
            self.store.put("quota_retry", {"period": payload["period"], "rule": payload["rule"],
                                           "target_base": self.config().target_base,
                                           "retry_at": time.time() + 300} if needed else None)

    def _scheduler(self):
        while not self.stop.wait(5):
            try:
                self.tick()
            except Exception as error:
                self.log("ERROR", f"调度暂未完成（{type(error).__name__}），稍后自动重试")

    def tick(self, now=None):
        with self.settings_lock:
            self._tick(now)

    def _tick(self, now=None):
        now = now or utcnow()
        rule = self.store.get("quota_rule")
        retry = self.store.get("quota_retry")
        if retry and retry["retry_at"] <= now.timestamp():
            period = now.astimezone(ZoneInfo(retry["rule"]["timezone"])).strftime("%Y-%m")
            self.store.put("quota_retry", None)
            if rule["enabled"] and period == retry["period"] and self.config().target_base == retry["target_base"]:
                try:
                    self.submit("quota_reset", {"rule": retry["rule"], "period": period})
                except SyncError:
                    retry["retry_at"] = now.timestamp() + 300
                    self.store.put("quota_retry", retry)
        if rule["enabled"] and rule.get("next_run_at") and datetime.fromisoformat(rule["next_run_at"]) <= now:
            period = now.astimezone(ZoneInfo(rule["timezone"])).strftime("%Y-%m")
            key = f"monthly-reset:{period}"
            job_id = self.submit("quota_reset", {"rule": rule, "period": period}, key)
            rule["next_run_at"] = next_month(now, rule["timezone"]).isoformat()
            self.store.put("quota_rule", rule)
            self.log("INFO", f"本月余额重置已排入任务 · {period}", job_id)
        values = self.store.get("config")
        for kind, spec in STABLE_GROUPS.items():
            schedule = self.store.get(kind + "_schedule", {})
            should_check = values[spec["enabled"]] and "openai" in values["sync_platforms"] and values["sync_accounts"]
            needs_disable = self.store.get(kind, {}).get("status") == "active" and not should_check
            if not (should_check or needs_disable):
                continue
            mode = self._due_stable_mode(kind, now.timestamp())
            if not mode:
                continue
            with self.pending_lock:
                idle = not self.busy
            if not idle:
                continue  # remote work is serial; the next tick tries again
            try:
                self.submit(kind, {"mode": mode})
            except SyncError:
                continue
            # Successful jobs replace these retries with the configured detection cadence.
            retry_at = now.timestamp() + values[spec["retry"]] * 60
            for check, entry, _ in spec["cadence"]:
                if mode == "ranking" or check == mode:
                    schedule[entry] = retry_at
            self.store.put(kind + "_schedule", schedule)
        scheduled = self.store.get("sync_schedule", {"next_run": time.time() + 1800})
        if values["sync_enabled"] and scheduled["next_run"] <= now.timestamp():
            try:
                self.submit("sync")
                self.store.put("sync_schedule", {"next_run": now.timestamp() + values["interval_minutes"] * 60})
            except SyncError:
                pass

    def overview(self):
        config = self.public_config()
        report = self.store.get("report", {})
        groups = self.store.get("groups", [])
        states = {status: sum(group["status"] == status for group in groups) for status in ("active", "inactive", "pending")}
        stable = {kind: self.store.get(kind, {}) for kind in STABLE_GROUPS}
        key_ids = {group["key_id"] for group in groups if group.get("key_id")}
        stable_models = 0
        for status in stable.values():
            if status.get("local_group_id") and status.get("status") in states:
                states[status["status"]] += 1
            key_ids.update(status.get("retained_key_ids", []))
            stable_models += len(status.get("models", [])) if status.get("status") == "active" else 0
        return {"config": config, "stats": {**states, "visible": report.get("visible_groups", 0),
                                             "models": sum(len(group.get("models", [])) for group in groups if group["status"] == "active") +
                                                       stable_models,
                                             "excluded": len(report.get("excluded", [])),
                                             "keys": len(key_ids)},
                "report": {key: report.get(key) for key in ("at", "pending", "warnings", "errors", "usable_groups", "usable_models")},
                "next_sync": self.store.get("sync_schedule", {}).get("next_run"),
                "provider_options": self.store.get("report", {}).get("provider_options", list(PLATFORMS)),
                **stable, **{kind + "_schedule": self.store.get(kind + "_schedule", {}) for kind in STABLE_GROUPS},
                "low_cost_weight_defaults": dict(DEFAULT_WEIGHTS),
                "connections": {side: self.store.get("connection_" + side) for side in ("upstream", "target")},
                "jobs": self.store.jobs(10), "quota_rule": self.store.get("quota_rule"),
                "server_time": time.time(), "timezone": "Asia/Shanghai"}

    def export_csv(self, job_id):
        artifact = self.store.artifact(job_id)
        if artifact is None:
            raise SyncError("该任务没有可下载的成员凭据")
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["email", "password", "username"])
        writer.writeheader()
        for row in artifact:
            # Spreadsheet formula injection protection for arbitrary display names.
            writer.writerow({key: "'" + value if value.startswith(("=", "+", "-", "@")) else value
                             for key, value in row.items()})
        self.log("INFO", "已导出批次成员凭据", job_id)
        return "\ufeff" + output.getvalue()
