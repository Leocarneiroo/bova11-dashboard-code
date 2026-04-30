import os
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import bova11_options_tape_flow as mod


class OptionsTapeFlowHistoryTests(unittest.TestCase):
    def test_merge_date_groups_keeps_history_and_overrides_same_date(self):
        history_groups = [
            {"date": "24/04/2026", "iso": "2026-04-24", "expiries": [{"label": "Old"}]},
            {"date": "25/04/2026", "iso": "2026-04-25", "expiries": [{"label": "Keep"}]},
        ]
        current_groups = [
            {"date": "24/04/2026", "iso": "2026-04-24", "expiries": [{"label": "New"}]},
            {"date": "27/04/2026", "iso": "2026-04-27", "expiries": [{"label": "Latest"}]},
        ]

        merged = mod.merge_date_groups(history_groups, current_groups)

        self.assertEqual(
            [group["date"] for group in merged],
            ["24/04/2026", "25/04/2026", "27/04/2026"],
        )
        self.assertEqual(merged[0]["expiries"][0]["label"], "New")
        self.assertEqual(merged[1]["expiries"][0]["label"], "Keep")

    def test_history_roundtrip_preserves_dashboard_groups(self):
        groups = [
            {
                "date": "24/04/2026",
                "iso": "2026-04-24",
                "expiries": [{"label": "15 Mai Mensal", "summary": {"total_volume": 123}}],
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            history_path = os.path.join(tmpdir, "options_history.json")
            mod.save_options_history(history_path, groups)
            loaded = mod.load_options_history(history_path)

        self.assertEqual(loaded, groups)


if __name__ == "__main__":
    unittest.main()
