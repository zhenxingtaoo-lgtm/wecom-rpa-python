from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from wecom_rpa.gui import GuiRunOptions, inspect_run_setup, validate_real_send_ready
from wecom_rpa.models import TargetGroup, TargetStatus
from wecom_rpa.storage import StateStore


class GuiSupportTest(unittest.TestCase):
    def test_validate_real_send_ready_requires_both_confirmations(self):
        validate_real_send_ready(dry_run=True, confirm_send=False, confirm_review=False)

        with self.assertRaisesRegex(ValueError, "真实发送确认"):
            validate_real_send_ready(dry_run=False, confirm_send=True, confirm_review=False)

        with self.assertRaisesRegex(ValueError, "真实发送确认"):
            validate_real_send_ready(dry_run=False, confirm_send=False, confirm_review=True)

        validate_real_send_ready(dry_run=False, confirm_send=True, confirm_review=True)

    def test_inspect_run_setup_applies_overrides_without_rewriting_yaml(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            config_path = root / "config.yaml"
            groups_path = root / "groups.csv"
            db_path = root / "state.sqlite3"
            config_text = "max_total_send: 10\nbatch_size: 9\nbatch_interval_sec: 5\ndry_run: true\n"
            config_path.write_text(config_text, encoding="utf-8")
            groups_path.write_text("group_name\nA群\nB群\nA群\n", encoding="utf-8")

            options = GuiRunOptions(
                config_path=config_path,
                groups_path=groups_path,
                db_path=db_path,
                log_file=root / "logs" / "wecom_rpa.log",
                screenshot_dir=root / "screenshots",
                dry_run=False,
                max_total_send=1,
                batch_size=1,
                batch_interval_sec=0.25,
                confirm_real_send=True,
                confirm_source_review=True,
            )

            inspection = inspect_run_setup(options)

            self.assertFalse(inspection.config.dry_run)
            self.assertEqual(inspection.config.max_total_send, 1)
            self.assertEqual(inspection.config.batch_size, 1)
            self.assertEqual(inspection.config.batch_interval_sec, 0.25)
            self.assertEqual(inspection.original_count, 2)
            self.assertEqual(inspection.limited_count, 1)
            self.assertFalse(inspection.has_uncertain)
            self.assertEqual(config_path.read_text(encoding="utf-8"), config_text)

    def test_inspect_run_setup_reports_uncertain_targets(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            config_path = root / "config.yaml"
            groups_path = root / "groups.csv"
            db_path = root / "state.sqlite3"
            config_path.write_text("max_total_send: 5\nbatch_size: 2\ndry_run: true\n", encoding="utf-8")
            groups_path.write_text("group_name\nA群\nB群\n", encoding="utf-8")

            with StateStore(db_path) as store:
                store.upsert_targets([TargetGroup("A群"), TargetGroup("B群")])
                store.set_status("B群", TargetStatus.UNCERTAIN)

            inspection = inspect_run_setup(
                GuiRunOptions(
                    config_path=config_path,
                    groups_path=groups_path,
                    db_path=db_path,
                    log_file=root / "logs" / "wecom_rpa.log",
                    screenshot_dir=root / "screenshots",
                    dry_run=True,
                )
            )

            self.assertTrue(inspection.has_uncertain)
            self.assertEqual(inspection.uncertain_targets, ["B群"])


if __name__ == "__main__":
    unittest.main()
