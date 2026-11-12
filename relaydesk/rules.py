"""Preferred groups ("优选分组") the user adds per platform and strategy.

Each rule owns one local group with a fixed ID whose upstream channel is switched
automatically. A platform can have at most one rule per strategy; the rule's key
`strategy:platform` names its state, schedule and source marker.
"""

from __future__ import annotations

import copy

from provider_ranking import PLATFORM_LABELS, PLATFORMS
from sync_sub2api import SyncError

from .scoring import DEFAULT_WEIGHTS, METRICS

STRATEGY_LABELS = {"low_cost": "最低价", "best_source": "最优源站"}
DEFAULTS = {
    "low_cost": {"score_minutes": 10, "health_minutes": 5, "retry_minutes": 1, "require_detection": True,
                 "exclude_keywords": [], "top_n": 10, "rank_tolerance": 2, "weights": dict(DEFAULT_WEIGHTS)},
    "best_source": {"check_minutes": 10, "keep_top": 10, "retry_minutes": 1},
}
# Detection cadence: (mode, schedule entry, interval field). A "ranking" check also covers every other check.
CADENCE = {"low_cost": (("ranking", "next_rank", "score_minutes"), ("health", "next_health", "health_minutes")),
           "best_source": (("ranking", "next_rank", "check_minutes"),)}
# Intervals may change while a job runs; any other change waits until work is idle.
LIVE_FIELDS = {"score_minutes", "health_minutes", "retry_minutes", "check_minutes"}
RANGES = {"score_minutes": (1, 1440, "评分检测间隔应为 1–1440 分钟"),
          "health_minutes": (1, 1440, "可用性检测间隔应为 1–1440 分钟"),
          "check_minutes": (1, 1440, "检测间隔应为 1–1440 分钟"),
          "retry_minutes": (1, 1440, "失败重试间隔应为 1–1440 分钟"),
          "top_n": (1, 100, "候选范围应为前 1–100 名"),
          "rank_tolerance": (0, 50, "允许的排名下降应为 0–50 名"),
          "keep_top": (1, 100, "保持范围应为前 1–100 名")}
MAX_EXCLUDE_KEYWORDS = 50
MAX_KEYWORD_LENGTH = 50
# Flat settings of the two fixed OpenAI groups that rules replaced.
LEGACY_FIELDS = {
    "low_cost": {"enabled": "low_cost_enabled", "name": "low_cost_name", "score_minutes": "low_cost_score_minutes",
                 "health_minutes": "low_cost_health_minutes", "retry_minutes": "low_cost_retry_minutes",
                 "require_detection": "low_cost_require_detection", "exclude_keywords": "low_cost_exclude_keywords",
                 "top_n": "low_cost_top_n", "rank_tolerance": "low_cost_rank_tolerance"},
    "best_source": {"enabled": "best_source_enabled", "name": "best_source_name",
                    "check_minutes": "best_source_check_minutes", "keep_top": "best_source_keep_top",
                    "retry_minutes": "best_source_retry_minutes"},
}
LEGACY_SETTINGS = ({key for fields in LEGACY_FIELDS.values() for key in fields.values()}
                   | {f"low_cost_weight_{metric}" for metric in METRICS})


def rule_key(rule) -> str:
    return f"{rule['strategy']}:{rule['platform']}"


def rule_label(rule) -> str:
    return f"{STRATEGY_LABELS[rule['strategy']]} {PLATFORM_LABELS.get(rule['platform'], rule['platform'])}"


def new_rule(strategy, platform, **fields):
    rule = {"strategy": strategy, "platform": platform, "enabled": False,
            "name": f"{STRATEGY_LABELS[strategy]} {PLATFORM_LABELS[platform]}", **copy.deepcopy(DEFAULTS[strategy])}
    rule.update(fields)
    return rule


def legacy_rule(strategy, values):
    """The fixed OpenAI group described by flat `low_cost_*` / `best_source_*` settings."""
    rule = new_rule(strategy, "openai")
    rule.update({field: copy.deepcopy(values[key]) for field, key in LEGACY_FIELDS[strategy].items() if key in values})
    if strategy == "low_cost":
        rule["weights"] = {metric: values.get(f"low_cost_weight_{metric}", DEFAULT_WEIGHTS[metric]) for metric in METRICS}
    return rule


def stable_rules(values):
    """Rules from Web settings; settings saved before rules existed describe the two OpenAI groups."""
    if "stable_groups" in values:
        return values["stable_groups"]
    return [legacy_rule(strategy, values) for strategy in DEFAULTS]


def normalize_rules(rules):
    """Validate user rules, fill defaults for omitted fields and normalize keywords."""
    if not isinstance(rules, list) or len(rules) > len(DEFAULTS) * len(PLATFORMS):
        raise SyncError("优选分组格式无效")
    result, keys, names = [], set(), set()
    for raw in rules:
        if not isinstance(raw, dict) or raw.get("strategy") not in DEFAULTS or raw.get("platform") not in PLATFORMS:
            raise SyncError("优选分组需要选择有效的平台与策略")
        rule = new_rule(raw["strategy"], raw["platform"])
        label = rule_label(rule)
        unknown = set(raw) - set(rule)
        if unknown:
            raise SyncError(f"{label}：包含未知字段 {', '.join(sorted(unknown))}")
        rule.update(copy.deepcopy(raw))
        if rule_key(rule) in keys:
            raise SyncError(f"「{label}」已存在，每个平台的同一策略只能添加一次")
        keys.add(rule_key(rule))
        if type(rule["enabled"]) is not bool:
            raise SyncError(f"{label}：启用状态必须为开关值")
        if not isinstance(rule["name"], str) or not 1 <= len(rule["name"].strip()) <= 100:
            raise SyncError(f"{label}：本地分组名称需要 1–100 个字符")
        rule["name"] = rule["name"].strip()
        if rule["name"] in names:
            raise SyncError(f"{label}：本地分组名称与其他优选分组重复")
        names.add(rule["name"])
        for field, (low, high, message) in RANGES.items():
            if field in rule and (type(rule[field]) is not int or not low <= rule[field] <= high):
                raise SyncError(f"{label}：{message}")
        if rule["strategy"] == "low_cost":
            _normalize_low_cost(rule, label)
        result.append(rule)
    return result


def _normalize_low_cost(rule, label):
    if type(rule["require_detection"]) is not bool:
        raise SyncError(f"{label}：模型检测要求必须为开关值")
    weights = rule["weights"]
    if (not isinstance(weights, dict) or set(weights) != set(METRICS)
            or any(type(value) is not int or not 0 <= value <= 100 for value in weights.values())
            or not sum(weights.values())):
        raise SyncError(f"{label}：评分权重应为 0–100 的整数，且至少有一项大于 0")
    keywords = rule["exclude_keywords"]
    if not isinstance(keywords, list) or any(not isinstance(item, str) for item in keywords):
        raise SyncError(f"{label}：排除关键字必须为文本列表")
    unique = {}
    for item in keywords:
        item = item.strip()
        if item and item.casefold() not in unique:
            unique[item.casefold()] = item
    if len(unique) > MAX_EXCLUDE_KEYWORDS or any(len(item) > MAX_KEYWORD_LENGTH for item in unique.values()):
        raise SyncError(f"{label}：排除关键字最多 {MAX_EXCLUDE_KEYWORDS} 个，每个不超过 {MAX_KEYWORD_LENGTH} 个字符")
    rule["exclude_keywords"] = list(unique.values())


def without_live_fields(rules):
    return [{key: value for key, value in rule.items() if key not in LIVE_FIELDS} for rule in rules]
