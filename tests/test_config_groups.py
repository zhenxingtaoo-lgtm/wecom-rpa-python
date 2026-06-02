from pathlib import Path
import tempfile
import unittest

from wecom_rpa.config import load_config
from wecom_rpa.groups import limit_groups, load_groups_csv, split_batches


class ConfigGroupsTest(unittest.TestCase):
    def test_load_config_example(self):
        cfg = load_config("config/config.example.yaml")
        self.assertTrue(cfg.dry_run)
        self.assertEqual(cfg.batch_size, 9)
        self.assertEqual(cfg.max_total_send, 100)
        self.assertEqual(cfg.recipient_selection.mode, "bottom_of_picker")
        self.assertTrue(cfg.recipient_selection.allow_staff_and_bots)
        self.assertEqual(cfg.recipient_selection.selection_direction, "bottom_to_top")
        self.assertFalse(cfg.recipient_selection.sentinel.enabled)
        self.assertTrue(cfg.recipient_selection.sentinel.stop_on_detection_failure)
        self.assertEqual(cfg.recipient_selection.sentinel.selected_list_region_ratio, [0.670, 0.220, 0.285, 0.760])
        self.assertEqual(cfg.ocr.engine, "paddleocr")
        self.assertEqual(cfg.ocr.fallback, "windows")
        self.assertEqual(cfg.source_selection.checkbox_x_ratio, 0.322)
        self.assertEqual(cfg.source_selection.checkbox_y_ratios, [0.547, 0.790])
        self.assertEqual(cfg.source_selection.forward_button_ratio, [0.478, 0.901])

    def test_reject_invalid_recipient_selection_mode(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text(
                "max_total_send: 10\nbatch_size: 9\ndry_run: true\nrecipient_selection:\n  mode: bad\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "recipient_selection.mode"):
                load_config(p)

    def test_reject_batch_size_over_9(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text("max_total_send: 10\nbatch_size: 10\ndry_run: true\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "batch_size"):
                load_config(p)

    def test_reject_enabled_sentinel_without_names(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text(
                "max_total_send: 10\n"
                "batch_size: 9\n"
                "dry_run: true\n"
                "recipient_selection:\n"
                "  sentinel:\n"
                "    enabled: true\n"
                "    names: []\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "sentinel.names"):
                load_config(p)

    def test_reject_no_dry_run(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text("max_total_send: 10\nbatch_size: 9\ndry_run: false\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "dry_run=false"):
                load_config(p)

    def test_allow_real_send_requires_explicit_override(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text("max_total_send: 11\nbatch_size: 9\ndry_run: false\n", encoding="utf-8")
            cfg = load_config(p, allow_real_send=True)
            self.assertFalse(cfg.dry_run)
            self.assertEqual(cfg.max_total_send, 11)

    def test_groups_dedup_limit_and_batches(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "groups.csv"
            p.write_text("group_name\n A群 \nB群\nA群\n\nC群\n", encoding="utf-8")
            groups = load_groups_csv(p)
            self.assertEqual([g.group_name for g in groups], ["A群", "B群", "C群"])
            limited = limit_groups(groups, 2)
            self.assertEqual([g.group_name for g in limited], ["A群", "B群"])
            batches = split_batches(groups, 2)
            self.assertEqual([b.batch_no for b in batches], [1, 2])
            self.assertEqual([len(b.targets) for b in batches], [2, 1])


if __name__ == "__main__":
    unittest.main()
