"""Price-led composite score used to pick the OpenAI channel for the stable local group.

Every metric is normalised to 0–1 within the current candidate pool, then combined
with user weights into a 0–100 score. Missing measurements count as neutral (0.5)
so a channel is neither rewarded nor punished for data the hall has not collected.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

METRICS = ("price", "rating", "popularity", "cache", "latency", "success")
DEFAULT_WEIGHTS = {"price": 40, "rating": 15, "popularity": 15, "cache": 10, "latency": 10, "success": 10}
WEIGHT_SETTINGS = {metric: f"low_cost_weight_{metric}" for metric in METRICS}
NEUTRAL = .5
# Keeps a free (0×) channel from making every other price score collapse to zero.
PRICE_SMOOTHING = .01


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _future(value, now):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    return moment.replace(tzinfo=moment.tzinfo or timezone.utc) > now


def _clamp(value):
    return min(1.0, max(0.0, value))


def raw_metrics(item, rate, now):
    """Measurements as shown in the provider hall; None when absent, stale or expired."""
    reputation = item.get("reputation") or {}
    stars = reputation.get("stars")
    if not (_finite(stars) and 0 <= stars <= 5 and _future(reputation.get("review_expires_at"), now)):
        stars = None
    popularity = reputation.get("popularity_share")
    if not (_finite(popularity) and 0 <= popularity <= 1 and _future(reputation.get("popularity_expires_at"), now)):
        popularity = None
    cache = item.get("range_cache" if item.get("summary_range") else "runtime_cache_1h") or {}
    cache_rate = cache.get("cache_hit_rate") if cache.get("ready") and not cache.get("stale") else None
    if not (_finite(cache_rate) and 0 <= cache_rate <= 1):
        cache_rate = None
    latency = None
    if item.get("runtime_trimmed_avg_has_data") is True and (item.get("runtime_trimmed_avg_sample_count") or 0) >= 1:
        latency = item.get("runtime_trimmed_avg_ttft_ms")
    if not (_finite(latency) and latency > 0):
        latency = item.get("avg_ttft_ms")
    if not (_finite(latency) and latency > 0):
        latency = None
    success = (item.get("success_rates") or {}).get("1h")
    if not (_finite(success) and 0 <= success <= 1):
        success = None
    return {"rate": rate, "stars": stars, "popularity": popularity, "cache_hit_rate": cache_rate,
            "latency_ms": latency, "success_rate": success}


def score_candidates(candidates, weights, now):
    """candidates: iterable of (group_id, applicable_rate, provider_item). Returns {group_id: details}."""
    rows = [(group_id, raw_metrics(item, rate, now)) for group_id, rate, item in candidates]
    if not rows:
        return {}
    best_rate = min(row["rate"] for _, row in rows)
    popularity = [row["popularity"] for _, row in rows if row["popularity"] is not None]
    top_popularity = max(popularity, default=0)
    latencies = [row["latency_ms"] for _, row in rows if row["latency_ms"] is not None]
    best_latency = min(latencies, default=None)
    total_weight = sum(weights[metric] for metric in METRICS) or 1
    result = {}
    for group_id, row in rows:
        parts = {
            "price": (best_rate + PRICE_SMOOTHING) / (row["rate"] + PRICE_SMOOTHING),
            "rating": NEUTRAL if row["stars"] is None else row["stars"] / 5,
            # Usage share spans orders of magnitude; compare on a log scale against the pool leader.
            "popularity": NEUTRAL if top_popularity <= 0 else
            math.log1p((row["popularity"] or 0) * 1000) / math.log1p(top_popularity * 1000),
            "cache": NEUTRAL if row["cache_hit_rate"] is None else _clamp((row["cache_hit_rate"] - .5) / .5),
            "latency": NEUTRAL if row["latency_ms"] is None else best_latency / row["latency_ms"],
            "success": NEUTRAL if row["success_rate"] is None else _clamp((row["success_rate"] - .8) / .2),
        }
        score = sum(weights[metric] * parts[metric] for metric in METRICS) / total_weight * 100
        result[group_id] = {"score": round(score, 2), "parts": {key: round(value, 4) for key, value in parts.items()},
                            "metrics": row}
    return result
