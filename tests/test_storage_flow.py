from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageDraw

from wecom_rpa.config import AppConfig, RecipientSelectionConfig, SentinelConfig
from wecom_rpa.forward_flow import ForwardFlow, SelectedRecipient
from wecom_rpa.wecom_window import WindowRect


class ForwardFlowTest(unittest.TestCase):
    def test_run_rejects_non_positive_send_count(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        with self.assertRaisesRegex(ValueError, "发送数量"):
            flow.run(0)

    def test_sentinel_trim_keeps_items_below_boundary(self):
        cfg = AppConfig(
            recipient_selection=RecipientSelectionConfig(
                sentinel=SentinelConfig(enabled=True, names=["boundary"])
            )
        )
        flow = ForwardFlow(cfg, install_stop_hotkey=False)
        plan = flow._build_sentinel_trim_plan(
            [
                SelectedRecipient("bottom"),
                SelectedRecipient("boundary"),
                SelectedRecipient("above"),
            ]
        )
        self.assertTrue(plan.boundary_reached)
        self.assertEqual([item.name for item in plan.send], ["bottom"])
        self.assertEqual([item.name for item in plan.remove], ["boundary", "above"])

    def test_sentinel_trim_matches_name_inside_noisy_ocr_row(self):
        cfg = AppConfig(
            recipient_selection=RecipientSelectionConfig(
                sentinel=SentinelConfig(enabled=True, names=["大小尘"])
            )
        )
        flow = ForwardFlow(cfg, install_stop_hotkey=False)
        plan = flow._build_sentinel_trim_plan(
            [
                SelectedRecipient("测试群聊七"),
                SelectedRecipient("大小尘 测试群聊五"),
                SelectedRecipient("测试群聊三"),
            ]
        )
        self.assertTrue(plan.boundary_reached)
        self.assertEqual([item.name for item in plan.send], ["测试群聊七"])
        self.assertEqual(
            [item.name for item in plan.remove],
            ["大小尘 测试群聊五", "测试群聊三"],
        )

    def test_left_candidate_region_stays_inside_picker_left_column(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        rect = WindowRect(-4, -4, 2888, 1712)
        region = flow._left_candidate_region(rect)
        self.assertLessEqual(region.left + region.width, rect.left + rect.width * 0.50)

    def test_left_checkbox_detection_uses_full_window_column(self):
        with tempfile.TemporaryDirectory() as d:
            flow = ForwardFlow(AppConfig(), screenshot_dir=d, install_stop_hotkey=False)

            class FakeScreen:
                def save_checkpoint(self, *_args, **_kwargs):
                    return Path(d) / "fake.png"

                def find_selected_checkbox_ratios(self, *_args, **_kwargs):
                    return [(0.300, 0.780), (0.420, 0.780)]

            flow.screen = FakeScreen()
            selected = flow._left_selected_checkbox_y_ratios(WindowRect(0, 0, 1600, 900), "probe")
            self.assertEqual(selected, [0.780])

    def test_recipient_candidates_stop_without_blind_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            flow = ForwardFlow(AppConfig(), screenshot_dir=d, install_stop_hotkey=False)

            class FakeScreen:
                def save_checkpoint(self, *_args, **_kwargs):
                    return Path(d) / "fake.png"

                def find_checkbox_outline_ratios(self, *_args, **_kwargs):
                    return []

            flow.screen = FakeScreen()
            with self.assertRaisesRegex(RuntimeError, "足够的会话复选框"):
                flow._recipient_checkbox_points_bottom_to_top(2, WindowRect(0, 0, 1600, 900), 1)

    def test_recipient_list_image_difference_detects_movement_and_stability(self):
        with tempfile.TemporaryDirectory() as d:
            before_path = Path(d) / "before.png"
            moved_path = Path(d) / "moved.png"
            stable_path = Path(d) / "stable.png"
            before = Image.new("RGB", (1600, 900), "white")
            moved = before.copy()
            ImageDraw.Draw(before).rectangle((500, 350, 700, 430), fill="black")
            ImageDraw.Draw(moved).rectangle((500, 500, 700, 580), fill="black")
            before.save(before_path)
            moved.save(moved_path)
            moved.save(stable_path)

            flow = ForwardFlow(AppConfig(), screenshot_dir=d, install_stop_hotkey=False)
            self.assertGreater(flow._recipient_list_image_difference(before_path, moved_path), 1.5)
            self.assertLess(flow._recipient_list_image_difference(moved_path, stable_path), 1.5)

    def test_source_context_menu_candidates_use_same_message_rows(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        flow.source_checkbox_y_ratios = [0.708, 0.527, 0.117]

        candidates = flow._source_context_menu_candidates()

        self.assertEqual(
            candidates,
            [(0.117, 0.75, 0.117), (0.527, 0.75, 0.527), (0.708, 0.75, 0.708)],
        )

    def test_source_context_menu_region_is_local_to_click(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        rect = WindowRect(0, 0, 2000, 1000)

        region = flow._source_context_menu_region(rect, 0.75, 0.50)

        self.assertLess(region.width * region.height, rect.width * rect.height * 0.20)
        self.assertLessEqual(region.left, 1500)
        self.assertGreaterEqual(region.left + region.width, 1500)

    def test_checkbox_scan_region_has_component_padding(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)

        region = flow._expanded_checkbox_scan_region(0.285, 0.330, 0.330, 0.835)

        self.assertLess(region[0], 0.285)
        self.assertLess(region[1], 0.330)
        self.assertGreater(region[0] + region[2], 0.330)
        self.assertGreater(region[1] + region[3], 0.835)


if __name__ == "__main__":
    unittest.main()
