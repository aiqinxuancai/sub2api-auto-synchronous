"""A stable local OpenAI group whose upstream can change without rebinding users."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import replace

from provider_ranking import detector_passed, ranked_items
from sync_sub2api import APIError, SyncError, changed_fields, select_usable

from .scoring import DEFAULT_WEIGHTS, WEIGHT_SETTINGS, score_candidates

LOG = logging.getLogger("sub2api-sync")


class LowCostSelector:
    def __init__(self, engine, settings):
        self.engine = engine
        self.settings = settings
        self.state = engine.state
        self.mapping = self.state.data.setdefault("low_cost", {"keys": {}})
        self.marker = f"[sub2api-lowcost:{engine.source_id}:openai]"

    def _discover(self, rows, field, mapping_key):
        matches = [row for row in rows if self.marker in (row.get(field) or "")]
        if len(matches) > 1:
            raise SyncError("最低价分组存在重复来源标记，请处理后重试")
        mapped = next((row for row in rows if row["id"] == self.mapping.get(mapping_key)), None)
        if mapped is not None and self.marker not in (mapped.get(field) or ""):
            raise SyncError("最低价分组的来源标记被修改，已停止更新")
        row = matches[0] if matches else None
        if row is not None:
            if row.get("platform") != "openai" or (field == "notes" and row.get("type") != "apikey"):
                raise SyncError("最低价分组的平台或转发类型发生变化")
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
        candidate_config = replace(config, sync_platforms={"openai"}, sync_top_n=0)
        usable, excluded = select_usable(groups, rates, providers, candidate_config, now)
        top_n = self.settings.get("low_cost_top_n") or 10
        top = ranked_items(providers, now, {"openai"})[:top_n]
        top_ids = {item["group_id"] for item in top}
        source_items = {item["group_id"]: item for item in providers["items"]}
        source_groups = {group["id"]: group for group in groups}
        # Case-insensitive substring match on the upstream group name; excluded channels never backfill the top N.
        keywords = [keyword.casefold() for keyword in self.settings.get("low_cost_exclude_keywords") or []]
        blocked = {group_id for group_id, group in source_groups.items()
                   if keywords and any(keyword in (group.get("name") or "").casefold() for keyword in keywords)}
        eligible = {group.source["id"]: group for group in usable if group.source["id"] not in blocked
                    and (not self.settings["low_cost_require_detection"] or detector_passed(source_items[group.source["id"]]))}
        # Score only channels inside the hall's top N that pass every availability gate.
        pool = [eligible[item["group_id"]] for item in top if item["group_id"] in eligible]
        weights = {metric: self.settings.get(key, DEFAULT_WEIGHTS[metric]) for metric, key in WEIGHT_SETTINGS.items()}
        scores = score_candidates(((row.source["id"], row.source_rate, source_items[row.source["id"]]) for row in pool),
                                  weights, now)
        pool.sort(key=lambda row: (-scores[row.source["id"]]["score"], row.provider_rank, row.source["id"]))
        score_rank = {row.source["id"]: index for index, row in enumerate(pool, 1)}
        candidates = []
        for rank, item in enumerate(top, 1):
            group_id = item["group_id"]
            selected = eligible.get(group_id)
            reason = "" if selected else "keyword_excluded" if group_id in blocked else excluded.get(group_id, "model_detection_not_passed")
            detail = scores.get(group_id, {})
            candidates.append({"upstream_id": group_id, "name": source_groups.get(group_id, {}).get("name", item.get("code", "")),
                               "provider_rank": rank, "eligible": selected is not None,
                               "source_rate": selected.source_rate if selected else None, "reason": reason,
                               "score": detail.get("score"), "score_rank": score_rank.get(group_id),
                               "parts": detail.get("parts"), "metrics": detail.get("metrics")})
        candidates.sort(key=lambda row: (row["score_rank"] is None, row["score_rank"] or 0, row["provider_rank"]))
        tolerance = self.settings.get("low_cost_rank_tolerance", 2)
        result = {"checked_at": now.isoformat(), "mode": mode, "candidates": candidates, "errors": [], "pending": 0,
                  "top_n": top_n, "rank_tolerance": tolerance, "weights": weights}
        local_groups = engine.target.list_all("/admin/groups")
        accounts = engine.target.list_all("/admin/accounts")
        group = self._discover(local_groups, "description", "local_group_id")
        account = self._discover(accounts, "notes", "local_account_id")
        active = (self.settings["low_cost_enabled"] and "openai" in config.sync_platforms and config.sync_accounts)
        if not active:
            self._disable(group, account, "selector_disabled")
            return self._result(result, "disabled")
        current_id = self.mapping.get("upstream_id")
        current = eligible.get(current_id)
        current_rank = score_rank.get(current_id)
        result["current_score_rank"] = current_rank
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
            LOG.info("最低价 OpenAI：当前渠道 #%d 综合评分降至第 %d 名，超过允许下降的 %d 名，重新选择",
                     current_id, current_rank, tolerance)
        else:
            keep, switch_reason = True, ""
        if keep:
            selected, decision = current, "retained"
        else:
            selected = pool[0] if pool else None
            decision = "selected" if current_id is None else "replaced"
        if selected is None:
            self._disable(group, account, "no_healthy_candidates")
            LOG.warning("最低价 OpenAI：前 %d 名暂无符合检测条件的可用渠道，分组已停用", top_n)
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
            fallback = [row for row in pool if row.source["id"] != group_id]  # best score first
            for alternative in fallback:
                # Do not create a more expensive Key as a side effect of a per-cycle cap.
                previous_blocked = engine.key_creation_blocked
                engine.key_creation_blocked = True
                try:
                    key = engine._get_key(alternative.source["id"])
                finally:
                    engine.key_creation_blocked = previous_blocked
                if key is not None:
                    selected, group_id, keep, decision = alternative, alternative.source["id"], False, "fallback_existing_key"
                    LOG.warning("最低价 OpenAI：Key 创建受限，改用已保留 Key 的可用候选 #%d", group_id)
                    break
        if key is None:
            if group_id == current_id:
                self._disable(group, account, "pending_upstream_key")
            result["pending"] = 1
            self.mapping["reason"] = "pending_upstream_key"
            self.state.save()
            LOG.warning("最低价 OpenAI：候选 #%d 等待上游 Key，保留已有 Key，后续自动重试", group_id)
            return self._result(result, "pending")
        self.mapping.setdefault("keys", {})[str(group_id)] = key["id"]
        self.state.save()
        desired = engine.group_payload(selected)
        desired.update(name=self.settings["low_cost_name"], description=self.marker + f"\n上游分组 #{group_id} · {selected.source['name']}")
        desired.pop("status")
        credentials = {"base_url": config.upstream_base, "api_key": key["key"],
                       "model_mapping": {model: model for model in selected.models}}
        digest = hashlib.sha256(key["key"].encode()).hexdigest()
        account_desired = {"name": self.settings["low_cost_name"], "notes": self.marker,
                           "rate_multiplier": selected.source_rate, "status": "active"}
        group_patch = changed_fields(group or {}, desired)
        account_patch = changed_fields(account or {}, account_desired)
        old_credentials = (account or {}).get("credentials") or {}
        if (self.mapping.get("key_digest") != digest or self.mapping.get("upstream_id") != group_id
                or old_credentials.get("base_url") != config.upstream_base
                or old_credentials.get("model_mapping") != credentials["model_mapping"]
                or (account or {}).get("credentials_status", {}).get("has_api_key") is False):
            account_patch["credentials"] = credentials
        bound = (account or {}).get("group_ids")
        if bound is None and isinstance((account or {}).get("groups"), list):
            bound = [row["id"] for row in account["groups"]]
        if group and bound != [group["id"]]:
            account_patch["group_ids"] = [group["id"]]
        if group_patch or account_patch:
            self._disable(group, account, "updating")
        if group is None:
            group = engine.target.request("POST", "/admin/groups", {**desired, "subscription_type": "standard", "is_exclusive": False})
            self.mapping["local_group_id"] = group["id"]
            self.state.save()
            self._disable(group, None, "updating")
        elif group_patch:
            engine.target.request("PUT", f"/admin/groups/{group['id']}", group_patch)
        if account is None:
            account = engine.target.request("POST", "/admin/accounts", {
                **{key: value for key, value in account_desired.items() if key != "status"},
                "platform": "openai", "type": "apikey", "credentials": credentials,
                "group_ids": [group["id"]], "concurrency": config.concurrency,
                "priority": config.priority, "upstream_billing_probe_enabled": False})
            self.mapping["local_account_id"] = account["id"]
            self.state.save()
        else:
            # _disable may have changed status after the patch was computed.
            if account.get("status") != "active":
                account_patch["status"] = "active"
            if account_patch:
                engine.target.request("PUT", f"/admin/accounts/{account['id']}", account_patch)
        if account.get("schedulable") is not True:
            engine.target.request("POST", f"/admin/accounts/{account['id']}/schedulable", {"schedulable": True})
        if group.get("status") != "active":
            engine.target.request("PUT", f"/admin/groups/{group['id']}", {"status": "active"})
        self.mapping.pop("baseline_rate", None)
        self.mapping.update(upstream_id=group_id, name=self.settings["low_cost_name"], source_name=selected.source["name"],
                            source_rate=selected.source_rate, local_rate=selected.local_rate, models=selected.models,
                            score=scores.get(group_id, {}).get("score"), score_rank=score_rank.get(group_id),
                            key_digest=digest, status="active", reason="healthy_models", provider_rank=selected.provider_rank)
        self.state.save()
        detail = scores.get(group_id)
        LOG.info("最低价 OpenAI：%s #%d，大厅排名 %d，综合评分 %s（第 %s 名），适用倍率 %g，模型 %d", "保持" if keep else "选用",
                 group_id, selected.provider_rank, detail["score"] if detail else "—", score_rank.get(group_id, "—"),
                 selected.source_rate, len(selected.models))
        return self._result(result, decision)

    def _result(self, result, decision):
        public = {key: value for key, value in self.mapping.items() if key not in {"key_digest", "keys"}}
        key_ids = sorted(set(self.mapping.get("keys", {}).values()))
        return {**result, **public, "decision": decision, "retained_keys": len(key_ids), "retained_key_ids": key_ids}
