"""Stable local groups (one per preferred-group rule) whose upstream channel can change
without rebinding users.

Each selector owns one local group and one forwarding account on the rule's platform,
discovered by a source marker. Subclasses only decide *which* upstream channel to route
to; creating, updating, disabling and Key reuse are shared.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field, replace

from provider_ranking import detector_passed, ranked_items
from sync_sub2api import (APIError, SyncError, bound_group_ids, changed_fields, ensure_account_binding, group_label,
                          select_usable)

from .rules import rule_key, rule_label
from .scoring import score_candidates

LOG = logging.getLogger("sub2api-sync")
NAME_LIMIT = 100
# Changing only these never needs the disable-update-enable cycle (prices only affect billing).
COSMETIC_FIELDS = {"name", "description", "concurrency", "model_pricing"}


def display_name(base, source_name):
    """`设置的名称（当前上游分组名）`, trimming the upstream part to fit the name limit."""
    source = str(source_name or "").strip()
    room = NAME_LIMIT - len(base) - 2
    if not source or room < 1:
        return base[:NAME_LIMIT]
    return f"{base}（{source[:room]}）"


@dataclass
class Context:
    now: object
    ranked: list            # provider items of the rule's platform in the hall's default order
    usable: dict            # upstream id -> SelectedGroup passing availability checks
    excluded: dict          # upstream id -> reason
    source_items: dict
    source_groups: dict

    def name(self, group_id, item=None):
        return self.source_groups.get(group_id, {}).get("name", (item or {}).get("code", ""))


@dataclass
class Plan:
    pool: list              # eligible channels, preferred first
    candidates: list
    current: object         # SelectedGroup for the routed channel when still eligible
    keep: bool
    switch_reason: str
    top_n: int
    extras: dict = field(default_factory=dict)
    details: dict = field(default_factory=dict)   # upstream id -> fields stored with the mapping
    notes: dict = field(default_factory=dict)     # upstream id -> extra log text


class StableGroupSelector:
    strategy = ""
    marker_tag = ""

    def __init__(self, engine, rule):
        self.engine = engine
        self.rule = rule
        self.platform = rule["platform"]
        self.label = rule_label(rule)
        self.state = engine.state
        key, data = rule_key(rule), self.state.data
        if key not in data and self.platform == "openai" and self.strategy in data:
            data[key] = data.pop(self.strategy)  # state saved when only the OpenAI groups existed
        self.mapping = data.setdefault(key, {"keys": {}})
        # The marker format predates rules; OpenAI groups keep their existing local resources.
        self.marker = f"[sub2api-{self.marker_tag}:{engine.source_id}:{self.platform}]"

    def plan(self, context, mode, current_id):
        raise NotImplementedError

    def _discover(self, rows, field_name, mapping_key):
        matches = [row for row in rows if self.marker in (row.get(field_name) or "")]
        if len(matches) > 1:
            raise SyncError(f"{self.label}分组存在重复来源标记，请处理后重试")
        mapped = next((row for row in rows if row["id"] == self.mapping.get(mapping_key)), None)
        if mapped is not None and self.marker not in (mapped.get(field_name) or ""):
            raise SyncError(f"{self.label}分组的来源标记被修改，已停止更新")
        row = matches[0] if matches else None
        if row is not None:
            if row.get("platform") != self.platform or (field_name == "notes" and row.get("type") != "apikey"):
                raise SyncError(f"{self.label}分组的平台或转发类型发生变化")
            self.mapping[mapping_key] = row["id"]
        return row

    def _disable(self, group, account, reason):
        if group and group.get("status") != "inactive":
            self.engine.target.request("PUT", f"/admin/groups/{group['id']}", {"status": "inactive"})
            group["status"] = "inactive"
        self.mapping["status"] = "inactive"
        self.mapping["reason"] = reason
        self.state.save()
        if account and account.get("status") != "inactive":
            self.engine.target.request("PUT", f"/admin/accounts/{account['id']}", {"status": "inactive"})
            account["status"] = "inactive"
        if account and account.get("schedulable") is not False:
            self.engine.target.request("POST", f"/admin/accounts/{account['id']}/schedulable", {"schedulable": False})
            account["schedulable"] = False

    def reconcile(self, snapshot, *, mode="ranking"):
        engine, config = self.engine, self.engine.config
        groups, rates, providers = snapshot
        now = engine.upstream.server_time
        # Validate freshness/completeness even when the selector is being disabled.
        candidate_config = replace(config, sync_limits={self.platform: 0})
        usable, excluded = select_usable(groups, rates, providers, candidate_config, now)
        context = Context(now, ranked_items(providers, now, {self.platform}), {row.source["id"]: row for row in usable}, excluded,
                          {item["group_id"]: item for item in providers["items"]}, {group["id"]: group for group in groups})
        current_id = self.mapping.get("upstream_id")
        plan = self.plan(context, mode, current_id)
        result = {"checked_at": now.isoformat(), "mode": mode, "candidates": plan.candidates, "errors": [], "pending": 0,
                  "top_n": plan.top_n, **plan.extras}
        local_groups = engine.target.list_all("/admin/groups")
        accounts = engine.target.list_all("/admin/accounts")
        group = self._discover(local_groups, "description", "local_group_id")
        account = self._discover(accounts, "notes", "local_account_id")
        active = self.rule["enabled"] and config.sync_accounts
        if not active:
            self._disable(group, account, "selector_disabled")
            return self._result(result, "disabled")
        keep, switch_reason, pool = plan.keep, plan.switch_reason, plan.pool
        if keep:
            selected, decision = plan.current, "retained"
        else:
            selected = pool[0] if pool else None
            decision = "selected" if current_id is None else "replaced"
        if selected is None:
            self._disable(group, account, "no_healthy_candidates")
            LOG.warning("%s：前 %d 名暂无符合检测条件的可用渠道，分组已停用", self.label, plan.top_n)
            return self._result(result, "no_candidates")
        result["switch_reason"] = switch_reason or None
        # Stop routing when the source no longer meets this check's selection policy.
        if not keep and group:
            self._disable(group, account, switch_reason or "updating")
        group_id = selected.source["id"]
        try:
            key = engine._get_key(group_id)
        except APIError as error:
            if error.status != 429:
                raise
            engine.key_creation_blocked = True
            key = None
        if key is None:
            # Creation limits must not prevent failover to a candidate with a retained Key.
            for alternative in (row for row in pool if row.source["id"] != group_id):  # preferred first
                # Do not create another Key as a side effect of a per-cycle cap.
                previous_blocked = engine.key_creation_blocked
                engine.key_creation_blocked = True
                try:
                    key = engine._get_key(alternative.source["id"])
                finally:
                    engine.key_creation_blocked = previous_blocked
                if key is not None:
                    selected, group_id, keep, decision = alternative, alternative.source["id"], False, "fallback_existing_key"
                    LOG.warning("%s：Key 创建受限，改用已保留 Key 的可用候选 %s", self.label,
                                group_label(group_id, alternative.source.get("name")))
                    break
        if key is None:
            if group_id == current_id:
                self._disable(group, account, "pending_upstream_key")
            result["pending"] = 1
            self.mapping["reason"] = "pending_upstream_key"
            self.state.save()
            LOG.warning("%s：候选 %s 等待上游 Key，保留已有 Key，后续自动重试", self.label,
                        group_label(group_id, selected.source.get("name")))
            return self._result(result, "pending")
        self.mapping.setdefault("keys", {})[str(group_id)] = key["id"]
        self.state.save()
        name = display_name(self.rule["name"], selected.source.get("name"))
        desired = engine.group_payload(selected, self.mapping.get("model_pricing_synced", False))
        desired.update(name=name, description=self.marker + f"\n上游分组 #{group_id} · {selected.source['name']}")
        desired.pop("status")
        credentials = {"base_url": config.upstream_base, "api_key": key["key"],
                       "model_mapping": {model: model for model in selected.models}}
        digest = hashlib.sha256(key["key"].encode()).hexdigest()
        account_desired = {"name": name, "notes": self.marker, "rate_multiplier": selected.source_rate,
                           "concurrency": config.concurrency, "status": "active"}
        group_patch = changed_fields(group or {}, desired)
        account_patch = changed_fields(account or {}, account_desired)
        old_credentials = (account or {}).get("credentials") or {}
        if (self.mapping.get("key_digest") != digest or self.mapping.get("upstream_id") != group_id
                or old_credentials.get("base_url") != config.upstream_base
                or old_credentials.get("model_mapping") != credentials["model_mapping"]
                or (account or {}).get("credentials_status", {}).get("has_api_key") is False):
            account_patch["credentials"] = credentials
        unbound = account is not None and group is not None and bound_group_ids(account) != [group["id"]]
        if unbound or set(group_patch) - COSMETIC_FIELDS or set(account_patch) - COSMETIC_FIELDS:
            self._disable(group, account, "updating")
        if group is None:
            group = engine.target.request("POST", "/admin/groups", {**desired, "subscription_type": "standard", "is_exclusive": False})
            self.mapping["local_group_id"] = group["id"]
            self.state.save()
            self._disable(group, None, "updating")
        elif group_patch:
            engine.target.request("PUT", f"/admin/groups/{group['id']}", group_patch)
        engine.pricing_updated += "model_pricing" in group_patch
        if account is None:
            account = engine.target.request("POST", "/admin/accounts", {
                **{key: value for key, value in account_desired.items() if key != "status"},
                "platform": self.platform, "type": "apikey", "credentials": credentials,
                "group_ids": [group["id"]], "priority": config.priority, "upstream_billing_probe_enabled": False})
            self.mapping["local_account_id"] = account["id"]
            self.state.save()
        else:
            # _disable may have changed status after the patch was computed.
            if account.get("status") != "active":
                account_patch["status"] = "active"
            if account_patch:
                engine.target.request("PUT", f"/admin/accounts/{account['id']}", account_patch)
                engine.concurrency_fixed += "concurrency" in account_patch
        ensure_account_binding(engine.target, account, group["id"], self.label)
        if account.get("schedulable") is not True:
            engine.target.request("POST", f"/admin/accounts/{account['id']}/schedulable", {"schedulable": True})
        if group.get("status") != "active":
            engine.target.request("PUT", f"/admin/groups/{group['id']}", {"status": "active"})
        self.mapping.pop("baseline_rate", None)
        self.mapping.update(upstream_id=group_id, name=name, source_name=selected.source["name"],
                            source_rate=selected.source_rate, local_rate=selected.local_rate, models=selected.models,
                            key_digest=digest, status="active", reason="healthy_models", provider_rank=selected.provider_rank,
                            model_pricing_synced=bool(desired.get("model_pricing")), **plan.details.get(group_id, {}))
        self.state.save()
        LOG.info("%s：%s %s，大厅排名 %d%s，适用倍率 %g，模型 %d", self.label, "保持" if keep else "选用",
                 group_label(group_id, selected.source.get("name")), selected.provider_rank,
                 plan.notes.get(group_id, ""), selected.source_rate, len(selected.models))
        return self._result(result, decision)

    def _result(self, result, decision):
        public = {key: value for key, value in self.mapping.items() if key not in {"key_digest", "keys"}}
        key_ids = sorted(set(self.mapping.get("keys", {}).values()))
        return {**result, **public, "decision": decision, "retained_keys": len(key_ids), "retained_key_ids": key_ids,
                "strategy": self.strategy, "platform": self.platform, "label": self.label}


class LowCostSelector(StableGroupSelector):
    """Price-led composite score over the hall's top N, with rank-drop hysteresis."""

    strategy = "low_cost"
    marker_tag = "lowcost"

    def plan(self, context, mode, current_id):
        top_n = self.rule["top_n"]
        top = context.ranked[:top_n]
        top_ids = {item["group_id"] for item in top}
        # Case-insensitive substring match on the upstream group name; excluded channels never backfill the top N.
        keywords = [keyword.casefold() for keyword in self.rule["exclude_keywords"]]
        blocked = {group_id for group_id, group in context.source_groups.items()
                   if keywords and any(keyword in (group.get("name") or "").casefold() for keyword in keywords)}
        eligible = {group_id: row for group_id, row in context.usable.items() if group_id not in blocked
                    and (not self.rule["require_detection"] or detector_passed(context.source_items[group_id]))}
        # Score only channels inside the hall's top N that pass every availability gate.
        pool = [eligible[item["group_id"]] for item in top if item["group_id"] in eligible]
        weights = self.rule["weights"]
        scores = score_candidates(((row.source["id"], row.source_rate, context.source_items[row.source["id"]]) for row in pool),
                                  weights, context.now)
        pool.sort(key=lambda row: (-scores[row.source["id"]]["score"], row.provider_rank, row.source["id"]))
        score_rank = {row.source["id"]: index for index, row in enumerate(pool, 1)}
        candidates = []
        for rank, item in enumerate(top, 1):
            group_id = item["group_id"]
            selected = eligible.get(group_id)
            reason = "" if selected else "keyword_excluded" if group_id in blocked else context.excluded.get(group_id, "model_detection_not_passed")
            detail = scores.get(group_id, {})
            candidates.append({"upstream_id": group_id, "name": context.name(group_id, item),
                               "provider_rank": rank, "eligible": selected is not None,
                               "source_rate": selected.source_rate if selected else None, "reason": reason,
                               "score": detail.get("score"), "score_rank": score_rank.get(group_id),
                               "parts": detail.get("parts"), "metrics": detail.get("metrics")})
        candidates.sort(key=lambda row: (row["score_rank"] is None, row["score_rank"] or 0, row["provider_rank"]))
        tolerance = self.rule["rank_tolerance"]
        current = eligible.get(current_id)
        current_rank = score_rank.get(current_id)
        if current_id is None:
            keep, switch_reason = False, ""
        elif current is None:
            # Availability checks switch immediately: closed, unreachable, unhealthy or newly excluded.
            keep, switch_reason = False, "keyword_excluded" if current_id in blocked else "source_unavailable"
        elif mode == "health":
            keep, switch_reason = True, ""
        elif current_id not in top_ids:
            keep, switch_reason = False, "outside_top_ten"
        elif current_rank > 1 + tolerance:
            keep, switch_reason = False, "rank_dropped"
            LOG.info("%s：当前渠道 %s 综合评分降至第 %d 名，超过允许下降的 %d 名，重新选择",
                     self.label, group_label(current_id, context.name(current_id)), current_rank, tolerance)
        else:
            keep, switch_reason = True, ""
        details = {group_id: {"score": detail["score"], "score_rank": score_rank[group_id]} for group_id, detail in scores.items()}
        notes = {group_id: f"，综合评分 {detail['score']}（第 {score_rank[group_id]} 名）" for group_id, detail in scores.items()}
        return Plan(pool, candidates, current, keep, switch_reason, top_n,
                    extras={"rank_tolerance": tolerance, "weights": weights, "current_score_rank": current_rank},
                    details=details, notes=notes)


class BestSourceSelector(StableGroupSelector):
    """The hall's default #1 available channel, kept until it leaves the top K."""

    strategy = "best_source"
    marker_tag = "bestsource"

    def plan(self, context, mode, current_id):
        keep_top = self.rule["keep_top"]
        top = context.ranked[:keep_top]
        pool = [context.usable[item["group_id"]] for item in top if item["group_id"] in context.usable]
        candidates = [{"upstream_id": item["group_id"], "name": context.name(item["group_id"], item), "provider_rank": rank,
                       "eligible": item["group_id"] in context.usable,
                       "source_rate": context.usable[item["group_id"]].source_rate if item["group_id"] in context.usable else None,
                       "reason": "" if item["group_id"] in context.usable else context.excluded.get(item["group_id"], "availability_unknown")}
                      for rank, item in enumerate(top, 1)]
        current = context.usable.get(current_id)
        rank_of = {item["group_id"]: rank for rank, item in enumerate(context.ranked, 1)}
        if current_id is None:
            keep, switch_reason = False, ""
        elif current is None:
            keep, switch_reason = False, "source_unavailable"
        elif rank_of.get(current_id, keep_top + 1) > keep_top:
            keep, switch_reason = False, "outside_top_ten"
            LOG.info("%s：当前渠道 %s 跌出默认排序前 %d 名，重新选择", self.label,
                     group_label(current_id, context.name(current_id)), keep_top)
        else:
            keep, switch_reason = True, ""
        return Plan(pool, candidates, current, keep, switch_reason, keep_top,
                    extras={"keep_top": keep_top, "current_provider_rank": rank_of.get(current_id)})
