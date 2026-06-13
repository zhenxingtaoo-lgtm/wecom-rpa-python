from pathlib import Path
import tempfile
import unittest

from wecom_rpa.config import load_config


class ConfigTest(unittest.TestCase):
    def test_load_config_example(self):
        cfg = load_config("config/config.example.yaml")
        self.assertTrue(cfg.dry_run)
        self.assertEqual(cfg.batch_size, 9)
        self.assertEqual(cfg.recipient_selection.mode, "bottom_of_picker")
        self.assertIsNone(cfg.ocr.model_root)

    def test_send_count_is_not_a_config_field(self):
        cfg = load_config("config/config.example.yaml")
        self.assertFalse(hasattr(cfg, "max_total_send"))

    def test_reject_invalid_recipient_selection_mode(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.yaml"
            path.write_text("batch_size: 9\ndry_run: true\nrecipient_selection:\n  mode: search_by_name\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "bottom_of_picker"):
                load_config(path)

    def test_reject_batch_size_over_9(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.yaml"
            path.write_text("batch_size: 10\ndry_run: true\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "batch_size"):
                load_config(path)

    def test_allow_real_send_requires_explicit_override(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.yaml"
            path.write_text("batch_size: 9\ndry_run: false\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "dry_run=false"):
                load_config(path)
            self.assertFalse(load_config(path, allow_real_send=True).dry_run)


if __name__ == "__main__":
    unittest.main()
