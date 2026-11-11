"""A stable local OpenAI group whose upstream can change without rebinding users."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import replace

from provider_ranking import detector_passed, ranked_items
from sync_sub2api import APIError, SyncError, changed_fields, select_usable

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
        top = ranked_items(providers, now, {"openai"})[:10]
        top_ids = {item["group_id"] for item in top}
        source_items = {item["group_id"]: item for item in providers["items"]}
        eligible = {group.source["id"]: group for group in usable if not self.settings["low_cost_require_detection"]
                    or detector_passed(source_items[group.source["id"]])}
        candidates = []
        source_groups = {group["id"]: group for group in groups}
        for rank, item in enumerate(top, 1):
            group_id = item["group_id"]
            selected = eligible.get(group_id)
            candidates.append({"upstream_id": group_id, "name": source_groups.get(group_id, {}).get("name", item.get("code", "")),
                               "provider_rank": rank, "eligible": selected is not None,
                               "source_rate": selected.source_rate if selected else None,
                               "reason": "" if selected else excluded.get(group_id, "model_detection_not_passed")})
        result = {"checked_at": now.isoformat(), "mode": mode, "candidates": candidates, "errors": [], "pending": 0}
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
        keep = current is not None and (mode == "health" or current_id in top_ids)
        if keep:
            selected, decision = current, "retained"
        else:
            pool = [eligible[item["group_id"]] for item in top if item["group_id"] in eligible]
            selected = min(pool, key=lambda row: (row.source_rate, row.provider_rank, row.source["id"])) if pool else None
            decision = "selected" if current_id is None else "replaced"
        if selected is None:
            self._disable(group, account, "no_healthy_candidates")
            LOG.warning("最低价 OpenAI：前 10 名暂无符合检测条件的可用渠道，分组已停用")
            return self._result(result, "no_candidates")
        # Stop routing when the source no longer meets this check's selection policy.
        if not keep and group:
            self._disable(group, account, "source_unavailable" if current is None else "outside_top_ten")
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
            fallback = sorted((eligible[item["group_id"]] for item in top
                               if item["group_id"] in eligible and item["group_id"] != group_id),
                              key=lambda row: (row.source_rate, row.provider_rank))
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
        self.mapping.update(upstream_id=group_id, name=self.settings["low_cost_name"], source_name=selected.source["name"],
                            source_rate=selected.source_rate, local_rate=selected.local_rate, models=selected.models,
                            key_digest=digest, status="active", reason="healthy_models", provider_rank=selected.provider_rank)
        self.state.save()
        LOG.info("最低价 OpenAI：%s #%d，排名 %d，适用倍率 %g，模型 %d", "保持" if keep else "选用",
                 group_id, selected.provider_rank, selected.source_rate, len(selected.models))
        return self._result(result, decision)

    def _result(self, result, decision):
        public = {key: value for key, value in self.mapping.items() if key not in {"key_digest", "keys"}}
        key_ids = sorted(set(self.mapping.get("keys", {}).values()))
        return {**result, **public, "decision": decision, "retained_keys": len(key_ids), "retained_key_ids": key_ids}
