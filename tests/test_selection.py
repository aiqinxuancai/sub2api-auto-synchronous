"""Ranking, top-N policy and the stable lowest-price group's lifecycle."""

import copy
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from provider_ranking import default_score, detector_passed, ranked_items
from relaydesk.low_cost import BestSourceSelector, LowCostSelector
from relaydesk.scoring import DEFAULT_WEIGHTS, score_candidates
from relaydesk.rules import legacy_rule, new_rule
from sync_sub2api import State, SyncError, Synchronizer, select_usable
import test_sync
from test_sync import iso_after


class RankingTests(unittest.TestCase):
    def test_default_score_normalizes_missing_measurements_and_image_tps(self):
        now = datetime.now(timezone.utc)
        item = {"rate_multiplier": .05, "success_rates": {"1h": 1}, "summary_tps": 40,
                "runtime_trimmed_avg_ttft_ms": 2000, "runtime_trimmed_avg_has_data": True,
                "runtime_trimmed_avg_sample_count": 1, "runtime_cache_1h": {"ready": True, "cache_hit_rate": .92},
                "effective_input_price_per_million_1h": .05}
        self.assertAlmostEqual(default_score(item, {}, now), 100)
        self.assertAlmostEqual(default_score({"success_rates": {"1h": 1}}, {}, now), 4000 / 140)
        item["hall_metrics"] = {"chart": {"image": True}}
        self.assertAlmostEqual(default_score(item, {}, now), 10000 / 120)

    def test_failover_then_health_then_score_then_id(self):
        def item(group_id, health, success, failover=True):
            return {"group_id": group_id, "platform": "openai", "visible_in_hall": True,
                    "detections_enabled": True, "model_cards": [{"health": health, "probe_state": "enabled"}],
                    "success_rates": {"1h": success}, "hall_metrics": {"automatic_failover_enabled": failover}}
        items = [item(1, "healthy", 1, False), item(6, "healthy", .75), item(3, "failed", 1),
                 item(5, "healthy", 1), item(4, "healthy", 1), item(2, "insufficient", 1)]
        self.assertEqual([i["group_id"] for i in ranked_items({"items": items}, datetime.now(timezone.utc))],
                         [4, 5, 6, 2, 3, 1])

    def test_reputation_expiry_and_detector_bonus(self):
        now = datetime.now(timezone.utc)
        item = {"success_rates": {"1h": 1}, "reputation": {
            "popularity_range": "1h", "popularity_share": 1, "stars": 5,
            "popularity_expires_at": iso_after(), "review_expires_at": iso_after()},
            "model_detection": {"applicable": True, "status": "passed",
                "stage_enabled": {"detection": True, "text": False},
                "stages": {"detection": {"state": "completed"}}}}
        base = 4000 / 140
        self.assertAlmostEqual(default_score(item, {}, now), base * 1.6)
        item["reputation"]["review_expires_at"] = iso_after(-30)
        self.assertAlmostEqual(default_score(item, {}, now), base * 1.4)
        item["model_detection"]["stage_enabled"]["text"] = True
        self.assertFalse(detector_passed(item))


class SelectionIntegrationTests(unittest.TestCase):
    setUp = test_sync.SyncIntegrationTests.setUp
    tearDown = test_sync.SyncIntegrationTests.tearDown
    cycle = test_sync.SyncIntegrationTests.cycle

    def all_healthy(self):
        self.fake.healthy_group_ids = {group["id"] for group in self.fake.groups}
        self.fake.provider_overrides = {7: {"success_rates": {"1h": .7}},
                                        8: {"success_rates": {"1h": 1}},
                                        9: {"success_rates": {"1h": .8}}}

    def test_ranked_key_creation_and_owned_local_sort_order(self):
        self.all_healthy()
        self.config.max_new_keys = 1
        report = self.cycle()
        self.assertEqual([row["upstream_id"] for row in report["groups"]], [8, 9, 7])
        self.assertEqual(self.fake.keys[0]["group_id"], 8)
        self.assertEqual(report["pending"], 2)
        self.config.max_new_keys = 10
        self.cycle()
        self.assertEqual([row["group_id"] for row in self.fake.keys], [8, 9, 7])
        state = State(self.config).data["groups"]
        for rank, source_id in enumerate((8, 9, 7), 1):
            row = next(row for row in self.fake.local_groups if row["id"] == state[str(source_id)]["local_group_id"])
            self.assertEqual(row["sort_order"], rank)
        self.assertNotIn("sort_order", self.fake.local_groups[0])

    def test_top_n_shrink_expand_reuses_resources_and_keys(self):
        self.all_healthy()
        self.cycle()
        previous = copy.deepcopy(State(self.config).data["groups"])
        self.config.sync_limits = {"openai": 1}
        self.config.disable_unavailable = False
        report = self.cycle()
        self.assertEqual([row["upstream_id"] for row in report["groups"]], [8])
        self.assertEqual(len(self.fake.keys), 3)
        for source_id in (7, 9):
            self.assertEqual(State(self.config).data["groups"][str(source_id)]["last_status"], "inactive")
        self.config.sync_limits = {"openai": 0}
        self.cycle()
        for source_id in (7, 8, 9):
            mapping = State(self.config).data["groups"][str(source_id)]
            self.assertEqual(mapping["last_status"], "active")
            self.assertEqual(mapping["local_group_id"], previous[str(source_id)]["local_group_id"])
        self.assertEqual(len(self.fake.keys), 3)

    def test_default_openai_and_other_platform_policy(self):
        self.all_healthy()
        self.fake.groups[1]["platform"] = "gemini"
        report = self.cycle()
        self.assertEqual({row["platform"] for row in report["groups"]}, {"openai"})
        self.config.sync_limits = {"gemini": 0}
        self.config.disable_unavailable = False
        self.cycle()
        self.assertEqual(State(self.config).data["groups"]["7"]["last_status"], "inactive")
        self.assertEqual(State(self.config).data["groups"]["8"]["last_status"], "active")
        self.config.sync_limits = {}
        self.cycle()
        self.assertTrue(all(row.get("last_status") == "inactive" for row in State(self.config).data["groups"].values()))
        self.assertEqual(len(self.fake.keys), 3)

    def test_each_platform_has_its_own_top_n(self):
        self.all_healthy()
        self.fake.groups[0]["platform"] = "gemini"
        self.config.sync_limits = {"openai": 1, "gemini": 1}
        report = self.cycle()
        self.assertEqual({row["upstream_id"] for row in report["groups"]}, {7, 8})
        self.config.sync_limits = {"openai": 0, "gemini": 1}
        report = self.cycle()
        self.assertEqual({row["upstream_id"] for row in report["groups"]}, {7, 8, 9})
        self.assertEqual(report["sync_limits"], {"openai": 0, "gemini": 1})

    def test_cap_before_access_and_expiry_checks_does_not_backfill(self):
        self.all_healthy()
        self.config.sync_limits = {"openai": 1}
        self.fake.provider_overrides[8]["probe_expires_at"] = iso_after(-30)
        report = self.cycle()
        self.assertEqual(report["usable_groups"], 0)
        self.assertEqual(report["excluded_reasons"], {"outside_sync_top_n": 2, "probe_expired": 1})

    def test_hidden_groups_are_excluded_even_with_healthy_models(self):
        self.fake.provider_overrides[7] = {"visible_in_hall": False}
        report = self.cycle()
        self.assertEqual(report["usable_groups"], 0)
        self.assertEqual(report["excluded"][0]["reason"], "not_visible_in_hall")

    def test_fatal_error_pending_count_uses_rank_order(self):
        self.all_healthy()
        self.fake.deny_groups = True
        report = self.cycle()
        self.assertEqual(report["actions"][0]["upstream_id"], 8)
        self.assertEqual(report["pending"], 3)


class LowCostTests(unittest.TestCase):
    setUp = test_sync.SyncIntegrationTests.setUp
    tearDown = test_sync.SyncIntegrationTests.tearDown

    def check(self, mode="ranking", **settings):
        engine = Synchronizer(self.config, State(self.config))
        self.fake.healthy_group_ids = getattr(self, "healthy", {7, 8, 9})
        rule = legacy_rule("low_cost", {"low_cost_enabled": True, **settings})
        return LowCostSelector(engine, rule).reconcile(engine.load_snapshot(), mode=mode)

    def test_lowest_applicable_rate_zero_override_and_stable_retention(self):
        self.fake.rates = {"7": 0, "8": .01}
        first = self.check()
        self.assertEqual(first["upstream_id"], 7)
        self.assertEqual(first["local_rate"], 0)
        self.fake.rates = {"7": 2, "8": .001}
        second = self.check()
        self.assertEqual(second["decision"], "retained")
        self.assertEqual(second["upstream_id"], 7)
        self.assertEqual(second["source_rate"], 2)
        self.assertEqual(first["local_group_id"], second["local_group_id"])
        self.assertEqual(len(self.fake.keys), 1)

    def test_selector_account_created_without_group_is_assigned(self):
        self.fake.ignore_account_groups_on_create = True
        result = self.check()
        self.assertEqual(result["status"], "active")
        self.assertEqual(self.fake.accounts[0]["group_ids"], [result["local_group_id"]])

    def test_sync_fixes_concurrency_of_selector_accounts_even_when_disabled(self):
        first = self.check()
        account = next(row for row in self.fake.accounts if row["id"] == first["local_account_id"])
        self.assertEqual(account["concurrency"], 100)
        self.config.concurrency = 9
        self.check(low_cost_enabled=False)
        self.assertEqual(account["status"], "inactive")
        Synchronizer(self.config, State(self.config)).cycle()
        self.assertEqual(account["concurrency"], 9)

    def test_group_and_account_names_show_current_upstream(self):
        self.check()
        self.assertEqual(self.fake.local_groups[-1]["name"], "最低价 OpenAI（Alpha）")
        self.assertEqual(self.fake.accounts[0]["name"], "最低价 OpenAI（Alpha）")
        # A rename alone updates in place without a disable/enable cycle.
        self.fake.groups[0]["name"] = "Alpha-Pro"
        self.fake.requests.clear()
        renamed = self.check("health")
        self.assertEqual(renamed["decision"], "retained")
        self.assertEqual(self.fake.local_groups[-1]["name"], "最低价 OpenAI（Alpha-Pro）")
        self.assertEqual(self.fake.accounts[0]["name"], "最低价 OpenAI（Alpha-Pro）")
        self.assertFalse(any(body.get("status") == "inactive" or body.get("schedulable") is False
                             for _, _, body in self.fake.mutations() if isinstance(body, dict)))
        self.healthy = {8, 9}
        self.check("health")
        self.assertEqual(self.fake.local_groups[-1]["name"], "最低价 OpenAI（Bad）")
        long_name = self.check("health", low_cost_name="x" * 99)
        self.assertEqual(long_name["name"], "x" * 99)

    def test_health_failover_disable_recover_same_alias_and_reuse_renamed_key(self):
        first = self.check()
        self.assertEqual(first["upstream_id"], 7)
        self.healthy = {8, 9}
        second = self.check("health")
        self.assertEqual(second["upstream_id"], 8)
        self.assertEqual(second["local_group_id"], first["local_group_id"])
        self.assertEqual(len(self.fake.accounts), 1)
        self.healthy = set()
        off = self.check("health")
        self.assertEqual(off["status"], "inactive")
        self.assertFalse(self.fake.accounts[0]["schedulable"])
        self.fake.keys[0]["name"] = "renamed"
        self.healthy = {7}
        recovered = self.check("health")
        self.assertEqual(recovered["upstream_id"], 7)
        self.assertEqual(recovered["local_group_id"], first["local_group_id"])
        self.assertTrue(self.fake.accounts[0]["schedulable"])
        self.assertEqual(len(self.fake.keys), 2)
        self.assertFalse(any(method == "DELETE" for method, _, _ in self.fake.requests))

    def test_only_ranking_check_replaces_healthy_current_outside_top_ten(self):
        first = self.check()
        for group_id in range(20, 30):
            self.fake.groups.append({"id": group_id, "name": f"Rank {group_id}", "platform": "openai",
                                     "status": "active", "rate_multiplier": .1 + (group_id - 20) / 100})
            self.fake.provider_overrides[group_id] = {"success_rates": {"1h": 1}}
        self.healthy = {group["id"] for group in self.fake.groups}
        retained = self.check("health")
        self.assertEqual(retained["upstream_id"], 7)
        self.assertGreater(retained["provider_rank"], 10)
        replaced = self.check("ranking")
        self.assertEqual(replaced["upstream_id"], 20)
        self.assertEqual(replaced["local_group_id"], first["local_group_id"])

    def test_outside_top_ten_is_disabled_while_replacement_waits_for_key(self):
        self.check()
        for group_id in range(20, 30):
            self.fake.groups.append({"id": group_id, "name": f"Rank {group_id}", "platform": "openai",
                                     "status": "active", "rate_multiplier": .1})
            self.fake.provider_overrides[group_id] = {"success_rates": {"1h": 1}}
        self.healthy = {group["id"] for group in self.fake.groups}
        self.fake.hourly_key_limit = True
        result = self.check("ranking")
        self.assertEqual(result["status"], "inactive")
        self.assertEqual(result["pending"], 1)
        self.assertEqual(len(self.fake.keys), 1)

    def test_detection_gate_and_regular_top_n_independence(self):
        self.fake.provider_overrides[7] = {"model_detection": {"status": "suspected"}}
        self.config.sync_limits = {"openai": 1}
        selected = self.check()
        self.assertEqual(selected["upstream_id"], 8)
        self.assertNotIn("7", State(self.config).data["groups"])
        self.assertEqual(self.fake.local_groups[-1]["model_allowlist"]["models"], ["model-a"])

    def test_exclude_keywords_skip_matching_names_case_insensitively(self):
        selected = self.check(low_cost_exclude_keywords=["ALP"])
        self.assertEqual(selected["upstream_id"], 8)
        reasons = {row["upstream_id"]: row["reason"] for row in selected["candidates"]}
        self.assertEqual(reasons[7], "keyword_excluded")
        self.assertFalse(next(row for row in selected["candidates"] if row["upstream_id"] == 7)["eligible"])

    def test_new_exclude_keyword_replaces_current_even_in_health_mode(self):
        first = self.check()
        self.assertEqual(first["upstream_id"], 7)
        replaced = self.check("health", low_cost_exclude_keywords=["alpha"])
        self.assertEqual(replaced["upstream_id"], 8)
        self.assertEqual(replaced["decision"], "replaced")
        self.assertEqual(replaced["local_group_id"], first["local_group_id"])
        self.assertEqual(len(self.fake.accounts), 1)

    def test_excluding_every_candidate_disables_group(self):
        self.check()
        off = self.check(low_cost_exclude_keywords=["a", "u"])
        self.assertEqual(off["status"], "inactive")
        self.assertEqual(off["reason"], "no_healthy_candidates")
        self.assertFalse(self.fake.accounts[0]["schedulable"])
        self.assertFalse(any(method == "DELETE" for method, _, _ in self.fake.requests))

    def test_configurable_candidate_top_n(self):
        self.fake.rates = {"7": .5, "8": .01}
        narrow = self.check(low_cost_top_n=1)
        self.assertEqual(narrow["upstream_id"], 7)
        self.assertEqual(narrow["top_n"], 1)
        self.assertEqual([row["upstream_id"] for row in narrow["candidates"]], [7])
        # Leaving a narrowed range replaces the channel on the next ranking check.
        widened = self.check(low_cost_top_n=3)
        self.assertEqual(widened["decision"], "retained")
        self.fake.rates = {"7": .5, "8": .01, "9": .3}
        self.healthy = {8, 9}
        moved = self.check("ranking", low_cost_top_n=2)
        self.assertEqual(moved["upstream_id"], 8)
        self.assertEqual(moved["local_group_id"], narrow["local_group_id"])

    def test_rank_drop_beyond_tolerance_switches_on_score_check(self):
        self.fake.rates = {"7": .5, "8": .6, "9": .7}
        first = self.check()
        self.assertEqual((first["upstream_id"], first["score_rank"]), (7, 1))
        self.fake.rates = {"7": .9, "8": .6, "9": .7}
        kept = self.check(low_cost_rank_tolerance=2)
        self.assertEqual((kept["decision"], kept["current_score_rank"]), ("retained", 3))
        switched = self.check(low_cost_rank_tolerance=1)
        self.assertEqual((switched["upstream_id"], switched["decision"], switched["switch_reason"]), (8, "replaced", "rank_dropped"))
        self.assertEqual(switched["local_group_id"], first["local_group_id"])
        self.assertEqual(len(self.fake.accounts), 1)

    def test_zero_tolerance_always_follows_the_top_score(self):
        self.fake.rates = {"7": .5, "8": .6}
        self.check()
        self.fake.rates = {"7": .61, "8": .6}
        self.assertEqual(self.check(low_cost_rank_tolerance=0)["upstream_id"], 8)

    def test_availability_check_ignores_rank_but_switches_when_unavailable(self):
        self.fake.rates = {"7": .5, "8": .6, "9": .7}
        self.check()
        self.fake.rates = {"7": .9, "8": .6, "9": .7}
        self.assertEqual(self.check("health", low_cost_rank_tolerance=0)["decision"], "retained")
        self.healthy = {8, 9}
        moved = self.check("health", low_cost_rank_tolerance=0)
        self.assertEqual((moved["upstream_id"], moved["switch_reason"]), (8, "source_unavailable"))

    def test_quality_can_outweigh_a_small_price_gap(self):
        review = {"review_expires_at": iso_after(), "popularity_expires_at": iso_after()}
        self.fake.rates = {"7": .10, "8": .11, "9": 5}
        self.fake.provider_overrides[7] = {"reputation": {**review, "stars": 2.5, "popularity_share": .001},
                                           "success_rates": {"1h": .85}, "avg_ttft_ms": 20000}
        self.fake.provider_overrides[8] = {"reputation": {**review, "stars": 4.8, "popularity_share": .12},
                                           "success_rates": {"1h": 1}, "avg_ttft_ms": 8000,
                                           "runtime_cache_1h": {"ready": True, "stale": False, "cache_hit_rate": .95}}
        chosen = self.check()
        self.assertEqual(chosen["upstream_id"], 8)
        price_only = {**{f"low_cost_weight_{key}": 0 for key in DEFAULT_WEIGHTS}, "low_cost_weight_price": 100}
        cheapest = self.check(low_cost_rank_tolerance=0, **price_only)
        self.assertEqual(cheapest["upstream_id"], 7)

    def test_stale_or_incomplete_snapshots_never_mutate(self):
        self.check()
        self.fake.requests.clear()
        self.fake.stale = True
        with self.assertRaises(SyncError):
            self.check("health")
        self.assertEqual(self.fake.mutations(), [])

    def test_lowest_group_key_is_reused_by_normal_sync(self):
        self.check()
        Synchronizer(self.config, State(self.config)).cycle()
        self.assertEqual(len(self.fake.keys), 3)
        self.assertEqual(len(self.fake.accounts), 4)
        self.assertEqual(self.fake.local_groups[0]["name"], "Manual")

    def test_key_limit_disables_failed_source_and_recovers_later(self):
        self.check()
        self.healthy = {8}
        self.fake.hourly_key_limit = True
        result = self.check("health")
        self.assertEqual(result["pending"], 1)
        self.assertEqual(result["status"], "inactive")
        self.assertEqual(len(self.fake.keys), 1)
        self.fake.hourly_key_limit = False
        result = self.check("health")
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["upstream_id"], 8)

    def test_platform_list_does_not_affect_alias_but_account_sync_does(self):
        self.check()
        self.config.sync_limits = {"gemini": 0}
        self.assertEqual(self.check("health")["status"], "active")
        self.config.sync_accounts = False
        result = self.check("health")
        self.assertEqual(result["status"], "inactive")
        self.assertEqual(self.fake.local_groups[0]["status"], "active")
        self.assertEqual(len(self.fake.keys), 1)

    def test_marker_tamper_stops_before_writes(self):
        self.check()
        self.fake.local_groups[-1]["description"] = "manual ownership"
        self.fake.requests.clear()
        with self.assertRaises(SyncError):
            self.check()
        self.assertEqual(self.fake.mutations(), [])

    def test_repeat_without_changes_makes_no_mutations(self):
        self.check()
        self.fake.requests.clear()
        self.check()
        self.assertEqual(self.fake.mutations(), [])

    def test_creation_limit_fails_over_using_another_retained_key(self):
        self.healthy = {9}
        self.check()
        self.healthy = {8}
        self.check("health")
        self.healthy = {7, 9}
        self.fake.hourly_key_limit = True
        result = self.check("health")
        self.assertEqual(result["upstream_id"], 9)
        self.assertEqual(result["decision"], "fallback_existing_key")
        self.assertEqual(result["status"], "active")
        self.assertEqual(len(self.fake.keys), 2)

    def test_partial_update_stays_disabled_and_recovers_without_new_alias(self):
        first = self.check()
        self.fake.rates["7"] = .3
        engine = Synchronizer(self.config, State(self.config))
        original = engine.target.request

        def reject_account_update(method, path, *args, **kwargs):
            if method == "PUT" and path.startswith("/admin/accounts/"):
                raise SyncError("Simulated rejected credentials update")
            return original(method, path, *args, **kwargs)

        with patch.object(engine.target, "request", side_effect=reject_account_update):
            selector = LowCostSelector(engine, legacy_rule("low_cost", {"low_cost_enabled": True}))
            with self.assertRaises(SyncError):
                selector.reconcile(engine.load_snapshot(), mode="health")
        self.assertEqual(self.fake.local_groups[-1]["status"], "inactive")
        recovered = self.check("health")
        self.assertEqual(recovered["status"], "active")
        self.assertEqual(recovered["local_group_id"], first["local_group_id"])
        self.assertEqual(len(self.fake.keys), 1)


if __name__ == "__main__":
    unittest.main()


class BestSourceTests(unittest.TestCase):
    setUp = test_sync.SyncIntegrationTests.setUp
    tearDown = test_sync.SyncIntegrationTests.tearDown

    def check(self, selector=BestSourceSelector, **settings):
        engine = Synchronizer(self.config, State(self.config))
        self.fake.healthy_group_ids = getattr(self, "healthy", {7, 8, 9})
        options = {"best_source_enabled": True, "low_cost_enabled": True, **settings}
        return selector(engine, legacy_rule(selector.strategy, options)).reconcile(engine.load_snapshot(), mode="ranking")

    def test_selects_default_first_and_keeps_it_inside_top_k(self):
        first = self.check()
        self.assertEqual((first["upstream_id"], first["provider_rank"], first["decision"]), (7, 1, "selected"))
        self.assertIn("[sub2api-bestsource:", self.fake.local_groups[-1]["description"])
        self.fake.provider_overrides[9] = {"success_rates": {"1h": 1}}  # 9 overtakes 7 in the default order
        kept = self.check(best_source_keep_top=10)
        self.assertEqual((kept["upstream_id"], kept["decision"], kept["current_provider_rank"]), (7, "retained", 2))
        moved = self.check(best_source_keep_top=1)
        self.assertEqual((moved["upstream_id"], moved["switch_reason"]), (9, "outside_top_ten"))
        self.assertEqual(moved["local_group_id"], first["local_group_id"])
        self.assertEqual(len(self.fake.accounts), 1)

    def test_switches_immediately_when_current_is_unavailable(self):
        self.assertEqual(self.check()["upstream_id"], 7)
        self.healthy = {8, 9}
        moved = self.check()
        self.assertEqual((moved["upstream_id"], moved["switch_reason"]), (8, "source_unavailable"))
        self.healthy = set()
        off = self.check()
        self.assertEqual((off["status"], off["reason"]), ("inactive", "no_healthy_candidates"))
        self.assertFalse(any(method == "DELETE" for method, _, _ in self.fake.requests))

    def test_skips_unavailable_first_place_when_selecting(self):
        self.healthy = {8, 9}
        self.assertEqual(self.check()["upstream_id"], 8)

    def test_coexists_with_low_cost_group_and_shares_upstream_key(self):
        best = self.check()
        cheap = self.check(LowCostSelector)
        self.assertEqual(best["upstream_id"], cheap["upstream_id"])
        self.assertNotEqual(best["local_group_id"], cheap["local_group_id"])
        self.assertEqual(len(self.fake.accounts), 2)
        self.assertEqual(len(self.fake.keys), 1)

    def test_rule_for_another_platform_owns_its_own_group_and_account(self):
        self.fake.groups[1]["platform"] = "deepseek"
        engine = Synchronizer(self.config, State(self.config))
        self.fake.healthy_group_ids = {7, 8, 9}
        deepseek = BestSourceSelector(engine, new_rule("best_source", "deepseek", enabled=True))
        result = deepseek.reconcile(engine.load_snapshot(), mode="ranking")
        self.assertEqual((result["upstream_id"], result["platform"], result["label"]), (8, "deepseek", "最优源站 DeepSeek"))
        group = next(row for row in self.fake.local_groups if row["id"] == result["local_group_id"])
        self.assertEqual(group["platform"], "deepseek")
        self.assertIn(f"[sub2api-bestsource:{engine.source_id}:deepseek]", group["description"])
        self.assertEqual(self.fake.accounts[0]["platform"], "deepseek")
        openai = self.check()
        self.assertEqual(openai["upstream_id"], 7)
        self.assertNotEqual(openai["local_group_id"], result["local_group_id"])
        self.assertTrue({"best_source:deepseek", "best_source:openai"} <= set(State(self.config).data))

    def test_state_from_fixed_openai_groups_moves_under_rule_key(self):
        first = self.check()
        state = State(self.config)
        state.data["best_source"] = state.data.pop("best_source:openai")
        state.save()
        again = self.check()
        self.assertEqual(again["local_group_id"], first["local_group_id"])
        self.assertEqual(again["decision"], "retained")
        data = State(self.config).data
        self.assertNotIn("best_source", data)
        self.assertEqual(data["best_source:openai"]["local_group_id"], first["local_group_id"])
        self.assertEqual(len(self.fake.keys), 1)

    def test_preferred_group_prices_follow_the_hall_without_stopping_routing(self):
        self.config.price_sync_platforms = {"openai"}
        self.fake.model_prices = {"model-a": {"input_per_million": 1, "output_per_million": 4}}
        first = self.check()
        group = next(row for row in self.fake.local_groups if row["id"] == first["local_group_id"])
        self.assertEqual(group["model_pricing"][0]["input_price"], 1e-6)
        self.fake.model_prices["model-a"]["input_per_million"] = 2
        self.fake.requests.clear()
        again = self.check()
        self.assertEqual(again["decision"], "retained")
        self.assertEqual(group["model_pricing"][0]["input_price"], 2e-6)
        self.assertFalse(any(body.get("status") == "inactive" or body.get("schedulable") is False
                             for _, _, body in self.fake.mutations()))
        self.config.price_sync_platforms = set()
        self.check()
        self.assertFalse(group["model_pricing"])

    def test_preferred_group_description_shows_hall_stats_without_stopping_routing(self):
        first = self.check()
        group = next(row for row in self.fake.local_groups if row["id"] == first["local_group_id"])
        self.assertNotIn("供应商大厅", group["description"])  # nothing measured yet
        self.fake.provider_overrides.setdefault(first["upstream_id"], {}).update(user_latency_ms=2500, summary_tps=41)
        self.fake.requests.clear()
        again = self.check()
        self.assertEqual(again["decision"], "retained")
        self.assertTrue(group["description"].endswith("\n供应商大厅 1h：延迟 2.5s · 缓存 — · TPS 41 · 成功率 —"))
        self.assertFalse(any(body.get("status") == "inactive" or body.get("schedulable") is False
                             for _, _, body in self.fake.mutations()))

    def test_disabled_selector_stops_routing(self):
        self.check()
        off = self.check(best_source_enabled=False)
        self.assertEqual((off["status"], off["reason"]), ("inactive", "selector_disabled"))
        self.assertFalse(self.fake.accounts[0]["schedulable"])


class ScoringTests(unittest.TestCase):
    now = datetime(2026, 11, 11, tzinfo=timezone.utc)

    def test_missing_measurements_are_neutral(self):
        scores = score_candidates([(1, .1, {}), (2, .1, {})], DEFAULT_WEIGHTS, self.now)
        self.assertEqual(scores[1]["score"], scores[2]["score"])
        parts = scores[1]["parts"]
        self.assertEqual(parts["price"], 1)
        self.assertEqual({parts[key] for key in ("rating", "popularity", "cache", "latency", "success")}, {.5})

    def test_free_channel_does_not_zero_other_price_scores(self):
        scores = score_candidates([(1, 0, {}), (2, .01, {})], {**dict.fromkeys(DEFAULT_WEIGHTS, 0), "price": 1}, self.now)
        self.assertEqual(scores[1]["parts"]["price"], 1)
        self.assertEqual(scores[2]["parts"]["price"], .5)

    def test_expired_reputation_and_stale_cache_are_ignored(self):
        item = {"reputation": {"stars": 5, "review_expires_at": "2026-11-10T00:00:00Z", "popularity_share": .5,
                               "popularity_expires_at": "2026-11-10T00:00:00Z"},
                "runtime_cache_1h": {"ready": True, "stale": True, "cache_hit_rate": 1}}
        metrics = score_candidates([(1, .1, item)], DEFAULT_WEIGHTS, self.now)[1]["metrics"]
        self.assertIsNone(metrics["stars"])
        self.assertIsNone(metrics["popularity"])
        self.assertIsNone(metrics["cache_hit_rate"])
