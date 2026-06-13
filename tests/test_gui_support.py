from pathlib import Path
import tempfile
import unittest

import json
from unittest import mock

from wecom_rpa.gui import GuiRunOptions, WeComRpaApp, compute_gui_layout, inspect_run_setup, validate_real_send_ready, write_run_snapshot


class GuiSupportTest(unittest.TestCase):
    def test_stop_button_only_requests_worker_stop(self):
        app = WeComRpaApp.__new__(WeComRpaApp)
        app.worker = mock.Mock()
        app.worker.is_alive.return_value = True
        app.status_var = mock.Mock()
        app.progress_var = mock.Mock()
        app.stop_button = mock.Mock()
        app.stop_controller = mock.Mock()

        with mock.patch("wecom_rpa.gui.terminate_active_powershell") as terminate:
            app._request_stop()

        app.stop_controller.request_stop.assert_called_once_with()
        app.stop_button.configure.assert_called_once_with(state="disabled")
        terminate.assert_called_once_with()

    def test_validate_real_send_ready_requires_both_confirmations(self):
        validate_real_send_ready(dry_run=True, confirm_send=False, confirm_review=False)
        with self.assertRaises(ValueError):
            validate_real_send_ready(dry_run=False, confirm_send=True, confirm_review=False)
        validate_real_send_ready(dry_run=False, confirm_send=True, confirm_review=True)

    def test_compute_gui_layout_fits_high_dpi_logical_work_area(self):
        layout = compute_gui_layout(screen_width=1440, screen_height=852, tk_scaling=2.0)
        self.assertLessEqual(layout.width, 1400)
        self.assertLessEqual(layout.height, 820)

    def test_inspection_uses_gui_send_count_and_batch_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            config_path = root / "config.yaml"
            config_path.write_text("batch_size: 9\nbatch_interval_sec: 5\ndry_run: true\n", encoding="utf-8")
            inspection = inspect_run_setup(
                GuiRunOptions(
                    config_path=config_path,
                    log_file=root / "logs" / "wecom_rpa.log",
                    screenshot_dir=root / "screenshots",
                    send_count=20,
                    dry_run=True,
                    batch_size=8,
                    batch_interval_sec=0.25,
                )
            )
            self.assertEqual(inspection.send_count, 20)
            self.assertEqual(inspection.batch_count, 3)
            self.assertEqual(inspection.config.batch_size, 8)
            self.assertEqual(inspection.config.batch_interval_sec, 0.25)

    def test_inspection_rejects_non_positive_send_count(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            config_path = root / "config.yaml"
            config_path.write_text("batch_size: 9\ndry_run: true\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "发送数量"):
                inspect_run_setup(
                    GuiRunOptions(
                        config_path=config_path,
                        log_file=root / "run.log",
                        screenshot_dir=root / "screenshots",
                        send_count=0,
                    )
                )

    def test_run_snapshot_records_effective_parameters(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            config_path = root / "config.yaml"
            config_path.write_text("batch_size: 9\ndry_run: true\n", encoding="utf-8")
            options = GuiRunOptions(
                config_path=config_path,
                log_file=root / "logs" / "wecom_rpa.log",
                screenshot_dir=root / "screenshots",
                send_count=12,
            )
            inspection = inspect_run_setup(options)
            snapshot = write_run_snapshot(options, inspection)
            payload = json.loads(snapshot.read_text(encoding="utf-8"))
            self.assertEqual(payload["send_count"], 12)
            self.assertEqual(payload["batch_count"], 2)
            self.assertEqual(payload["effective_config"]["batch_size"], 9)


if __name__ == "__main__":
    unittest.main()
