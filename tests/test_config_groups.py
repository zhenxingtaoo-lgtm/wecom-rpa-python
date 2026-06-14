import unittest

from wecom_rpa.config import build_runtime_config


class ConfigTest(unittest.TestCase):
    def test_build_runtime_config_defaults(self):
        cfg = build_runtime_config(dry_run=True)
        self.assertTrue(cfg.dry_run)
        self.assertEqual(cfg.batch_size, 9)
        self.assertEqual(cfg.recipient_selection.mode, "bottom_of_picker")
        self.assertIsNone(cfg.ocr.model_root)

    def test_send_count_is_not_a_config_field(self):
        cfg = build_runtime_config(dry_run=True)
        self.assertFalse(hasattr(cfg, "max_total_send"))

    def test_reject_batch_size_over_9(self):
        with self.assertRaisesRegex(ValueError, "batch_size"):
            build_runtime_config(dry_run=True, batch_size=10)

    def test_allow_real_send_requires_explicit_override(self):
        with self.assertRaisesRegex(ValueError, "dry_run=false"):
            build_runtime_config(dry_run=False)
        self.assertFalse(build_runtime_config(dry_run=False, allow_real_send=True).dry_run)

    def test_enabled_sentinel_requires_name(self):
        with self.assertRaisesRegex(ValueError, "哨兵"):
            build_runtime_config(dry_run=True, sentinel_enabled=True)

        cfg = build_runtime_config(
            dry_run=True,
            sentinel_enabled=True,
            sentinel_names=["大小尘"],
        )
        self.assertEqual(cfg.recipient_selection.sentinel.names, ["大小尘"])


if __name__ == "__main__":
    unittest.main()
