"""Usage window and model totals recorded before balance resets."""

import unittest
from datetime import date, datetime, timezone

from relaydesk.usage import MODELS_KEPT, usage_totals, usage_window
from sync_sub2api import SyncError

ZONE = "Asia/Shanghai"


def at(*args):
    return datetime(*args, tzinfo=timezone.utc)


class UsageWindowTests(unittest.TestCase):
    def test_monthly_reset_closes_the_previous_month(self):
        # 2026-02-01 00:00:05 in Shanghai.
        self.assertEqual(usage_window(ZONE, "2026-02", None, at(2026, 1, 31, 16, 0, 5)), (date(2026, 1, 1), date(2026, 2, 1)))
        # A catch-up run days later still covers the month before the period.
        self.assertEqual(usage_window(ZONE, "2026-04", None, at(2026, 4, 3)), (date(2026, 3, 1), date(2026, 4, 3)))
        self.assertEqual(usage_window(ZONE, "2026-01", None, at(2026, 1, 1, 0, 0)), (date(2025, 12, 1), date(2026, 1, 1)))

    def test_manual_reset_covers_the_current_month_or_the_last_one_on_the_first(self):
        self.assertEqual(usage_window(ZONE, "manual-abc", None, at(2026, 3, 12, 4)), (date(2026, 3, 1), date(2026, 3, 12)))
        self.assertEqual(usage_window(ZONE, None, None, at(2026, 3, 1, 4)), (date(2026, 2, 1), date(2026, 3, 1)))

    def test_window_starts_on_the_last_reset_day_and_looks_back_at_most_92_days(self):
        last = at(2026, 3, 5, 18).timestamp()  # 03-06 02:00 in Shanghai
        self.assertEqual(usage_window(ZONE, "manual-x", last, at(2026, 3, 20, 4)), (date(2026, 3, 6), date(2026, 3, 20)))
        self.assertEqual(usage_window(ZONE, "2026-04", last, at(2026, 3, 31, 16)), (date(2026, 3, 6), date(2026, 4, 1)))
        self.assertEqual(usage_window(ZONE, "manual-x", at(2025, 1, 1).timestamp(), at(2026, 3, 20, 4)),
                         (date(2025, 12, 18), date(2026, 3, 20)))
        # A clock that moved backwards never yields a window ending before it starts.
        self.assertEqual(usage_window(ZONE, "manual-x", at(2026, 5, 1).timestamp(), at(2026, 3, 20, 4)),
                         (date(2026, 3, 20), date(2026, 3, 20)))


class UsageTotalsTests(unittest.TestCase):
    def test_totals_rank_models_by_spend_and_fold_the_long_tail(self):
        models = [{"model": f"m{index:02d}", "requests": 1, "total_tokens": 10, "cost": 1, "actual_cost": index / 10}
                  for index in range(MODELS_KEPT + 3)]
        totals, kept = usage_totals(models)
        self.assertEqual(totals["requests"], MODELS_KEPT + 3)
        self.assertEqual(totals["total_tokens"], (MODELS_KEPT + 3) * 10)
        self.assertAlmostEqual(totals["actual_cost"], sum(index / 10 for index in range(MODELS_KEPT + 3)))
        self.assertEqual(kept[0]["model"], f"m{MODELS_KEPT + 2:02d}")
        self.assertEqual(len(kept), MODELS_KEPT + 1)
        self.assertEqual(kept[-1]["model"], "其他 3 个模型")
        self.assertTrue(kept[-1]["other"])
        self.assertEqual(kept[-1]["requests"], 3)

    def test_invalid_measurements_count_as_zero(self):
        totals, kept = usage_totals([{"model": "", "requests": True, "total_tokens": float("nan"),
                                      "actual_cost": -3, "cost": "9"}, "noise",
                                     {"model": "gpt-5", "requests": 2, "actual_cost": 1.5}])
        self.assertEqual([row["model"] for row in kept], ["gpt-5", "未知模型"])
        self.assertEqual((totals["requests"], totals["total_tokens"], totals["actual_cost"], totals["cost"]), (2, 0, 1.5, 0))
        self.assertEqual(usage_totals([]), ({key: 0 for key in totals}, []))
        with self.assertRaises(SyncError):
            usage_totals(None)


if __name__ == "__main__":
    unittest.main()
