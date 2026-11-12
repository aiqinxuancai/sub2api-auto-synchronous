"""Per-member usage captured right before a balance reset."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from sync_sub2api import SyncError

# Sub2API's model distribution, filtered by member; it reads usage_logs directly.
USAGE_PATH = "/admin/dashboard/models"
LOOKBACK_DAYS = 92
MODELS_KEPT = 20
COUNTS = ("requests", "input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens", "total_tokens")
COSTS = ("cost", "actual_cost")


def usage_window(timezone_name: str, period: str | None, last_reset: float | None, now: datetime) -> tuple[date, date]:
    """Calendar days since the member's last reset; a first summary covers the month being closed."""
    zone = ZoneInfo(timezone_name)
    today = now.astimezone(zone).date()
    if last_reset:
        since = datetime.fromtimestamp(last_reset, zone).date()
    elif period and not period.startswith("manual-"):
        year, month = map(int, period.split("-"))
        since = (date(year, month, 1) - timedelta(days=1)).replace(day=1)
    else:
        since = (today - timedelta(days=1)).replace(day=1)
    return min(max(since, today - timedelta(days=LOOKBACK_DAYS)), today), today


def _count(value) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0 else 0


def _cost(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0 else 0.0


def usage_totals(models) -> tuple[dict, list[dict]]:
    """Totals across all models and the busiest models by spend, the remainder folded into one row."""
    if not isinstance(models, list):
        raise SyncError("用量接口返回结构无法识别")
    rows = []
    for item in models:
        if not isinstance(item, dict):
            continue
        name = item.get("model") if isinstance(item.get("model"), str) and item.get("model").strip() else "未知模型"
        rows.append({"model": name[:200], **{key: _count(item.get(key)) for key in COUNTS},
                     **{key: _cost(item.get(key)) for key in COSTS}})
    rows.sort(key=lambda row: (-row["actual_cost"], -row["requests"], row["model"]))
    totals = {key: sum(row[key] for row in rows) for key in COUNTS + COSTS}
    kept, rest = rows[:MODELS_KEPT], rows[MODELS_KEPT:]
    if rest:
        kept.append({"model": f"其他 {len(rest)} 个模型", "other": True,
                     **{key: sum(row[key] for row in rest) for key in COUNTS + COSTS}})
    return totals, kept


def member_usage(client, user_id: int, since: date, until: date, timezone_name: str) -> tuple[dict, list[dict]]:
    query = urlencode({"user_id": user_id, "start_date": since.isoformat(), "end_date": until.isoformat(),
                       "timezone": timezone_name})
    data = client.request("GET", f"{USAGE_PATH}?{query}")
    return usage_totals(data.get("models") if isinstance(data, dict) else None)
