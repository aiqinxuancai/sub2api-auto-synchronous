"""AIHub providers default (1h, best) ordering, without personal preferences.

Ported from the provider hall ranking policy v6. Missing measurements have zero
weight contribution, but remain in the denominator, just as in the hall UI.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

PLATFORMS = ("openai", "deepseek", "zhipu", "kimi", "anthropic", "grok", "gemini")
PLATFORM_LABELS = {"openai": "OpenAI", "deepseek": "DeepSeek", "zhipu": "智谱", "kimi": "Kimi",
                   "anthropic": "Anthropic", "grok": "Grok", "gemini": "Gemini"}
DEFAULT_WEIGHTS = dict(multiplier=0, effective_price=40, user_speed=20,
                       cache_hit_rate=20, success_rate=40, tps=20)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def visible(item):
    value = item.get("visible_in_hall")
    return value is True if value is not None else item.get("available") is True


def health_bucket(item):
    enabled = item.get("detections_enabled")
    probe = item.get("probe_state")
    skipped = probe is None and item.get("probe_skipped") is True
    if enabled is False or (enabled is None and (skipped or probe in {
            "disabled", "inactive", "unavailable"})):
        return 1
    health = []
    for card in item.get("model_cards") or []:
        state = card.get("probe_state") if enabled is not None else (
            probe if probe in {"disabled", "inactive", "unavailable"} else card.get("probe_state"))
        health.append("insufficient" if skipped or state not in (None, "enabled") else card.get("health"))
    return 2 if "healthy" in health else 0 if "failed" in health else 1


def detector_passed(item):
    detection = item.get("model_detection") or {}
    if (item.get("detections_enabled") is False or detection.get("applicable") is not True
            or detection.get("technical_failure") or detection.get("status") != "passed"):
        return False
    presentation = detection.get("presentation")
    if presentation is not None:
        if "detector" not in (presentation.get("modes") or []):
            return False
    else:
        targets = detection.get("presentation_targets") or []
        required = detection.get("required_targets", 0)
        if (type(required) is int and required > 0 and len(targets) == required
                and len({target.get("name") for target in targets}) == required):
            if not any(not target.get("result", {}).get("presentation") or
                       "detector" in target["result"]["presentation"].get("modes", []) for target in targets):
                return False
    stages = detection.get("stages") or {}

    def completed(name):
        stage = stages.get(name) or {}
        return (stage.get("published_state") or stage.get("state")) == "completed"

    enabled = detection.get("stage_enabled")
    if enabled is not None:
        if enabled.get("detection") is not True or not completed("detection"):
            return False
        if enabled.get("text") is False:
            return True
        if enabled.get("text") is not True:
            return False
    if (presentation or {}).get("text", {}).get("result") is not True:
        return False
    return not detection.get("stages") or all(completed(name) for name in ("detection", "text"))


def _future(value, now):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.replace(tzinfo=dt.tzinfo or timezone.utc) > now
    except (TypeError, ValueError, AttributeError):
        return False


def default_score(item, snapshot, now):
    settings = snapshot.get("display_ranking") or {}
    weights = settings.get("weights") or DEFAULT_WEIGHTS
    image = (item.get("hall_metrics") or {}).get("chart", {}).get("image") is True
    cache = item.get("range_cache" if item.get("summary_range") else "runtime_cache_1h") or {}
    latency = item.get("runtime_trimmed_avg_ttft_ms")
    if (image or item.get("runtime_trimmed_avg_has_data") is not True
            or not finite(item.get("runtime_trimmed_avg_sample_count"))
            or item["runtime_trimmed_avg_sample_count"] < 1
            or not finite(latency) or latency <= 0):
        latency = None
    values = {
        "multiplier": (item.get("rate_multiplier"), lambda x: (.3 - x) / .25 * 100),
        "effective_price": (item.get("effective_input_price_per_million" if item.get("summary_range")
                                     else "effective_input_price_per_million_1h"), lambda x: (.3 - x) / .25 * 100),
        "user_speed": (latency, lambda x: (50000 - x) / 48000 * 100),
        "cache_hit_rate": (cache.get("cache_hit_rate") if cache.get("ready") and not cache.get("stale") else None,
                           lambda x: (x - .65) / .27 * 100),
        "success_rate": ((item.get("success_rates") or {}).get("1h"), lambda x: (x - .5) / .5 * 100),
        "tps": (item.get("summary_tps"), lambda x: x / 40 * 100),
    }
    total = score = 0
    for metric, (value, normalize) in values.items():
        weight = weights.get(metric, 0)
        if not finite(weight) or not 0 <= weight <= 100:
            return 0
        if image and metric == "tps":
            continue
        total += weight
        if finite(value):
            score += min(100, max(0, normalize(value))) * weight
    if not total:
        return 0
    reputation = item.get("reputation") or {}
    policy = snapshot.get("reputation_settings") or {}
    popularity = reputation.get("popularity_share")
    stars = reputation.get("stars")
    if (not finite(popularity) or not 0 <= popularity <= 1
            or reputation.get("popularity_range", "1h") != "1h"
            or not _future(reputation.get("popularity_expires_at"), now)):
        popularity = 0
    exponent = policy.get("review_exponent", 1)
    review = (stars / 5) ** exponent if (finite(stars) and 0 <= stars <= 5
              and finite(exponent) and .5 <= exponent <= 3
              and _future(reputation.get("review_expires_at"), now)) else 0
    bonus = 1 + (settings.get("detection_bonus_percent", 20) / 100 if detector_passed(item) else 0)
    bonus += policy.get("popularity_bonus_percent", settings.get("popularity_bonus_percent", 20)) / 100 * popularity
    bonus += policy.get("review_bonus_percent", settings.get("review_bonus_percent", 20)) / 100 * review
    return score / total * bonus


def ranked_items(snapshot, now, platforms=None):
    tabs = snapshot.get("platform_tabs") or PLATFORMS
    allowed = set(tabs) if platforms is None else set(tabs) & set(platforms)
    items = [item for item in snapshot["items"] if visible(item) and item.get("platform") in allowed]

    def order(item):
        failover = (item.get("hall_metrics") or {}).get("automatic_failover_enabled")
        return (failover is False, -health_bucket(item), -default_score(item, snapshot, now), item["group_id"])

    return sorted(items, key=order)


def rank_metadata(snapshot, now):
    counters = {}
    result = {}
    for index, item in enumerate(ranked_items(snapshot, now), 1):
        platform = item["platform"]
        counters[platform] = counters.get(platform, 0) + 1
        result[item["group_id"]] = {"provider_rank": counters[platform], "sync_order": index,
                                    "provider_score": round(default_score(item, snapshot, now), 6)}
    return result
