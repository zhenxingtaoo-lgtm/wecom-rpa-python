from pathlib import Path
import tempfile
import unittest
from unittest import mock

from PIL import Image, ImageDraw

from wecom_rpa.config import AppConfig, RecipientSelectionConfig, SentinelConfig
from wecom_rpa.forward_flow import ForwardFlow, SelectedRecipient
from wecom_rpa.screen import OcrLine, Region
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

    def test_left_candidate_region_follows_vertically_stretched_picker(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        rect = WindowRect(0, 0, 1920, 1080)
        flow._last_recipient_picker_rect = Region(left=360, top=40, width=1200, height=1030)

        region = flow._left_candidate_region(rect)

        self.assertLessEqual(region.top, rect.top + rect.height * 0.20)
        self.assertGreater(region.top + region.height, rect.top + rect.height * 0.95)
        self.assertLessEqual(region.top + region.height, 1070)

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

    def test_left_checkbox_verification_uses_clicked_column_instead_of_avatar_blue(self):
        with tempfile.TemporaryDirectory() as d:
            flow = ForwardFlow(AppConfig(), screenshot_dir=d, install_stop_hotkey=False)
            flow._last_recipient_checkbox_x_ratio = 0.244

            class FakeScreen:
                def save_checkpoint(self, *_args, **_kwargs):
                    return Path(d) / "fake.png"

                def find_selected_checkbox_ratios(self, *_args, **_kwargs):
                    return [
                        (0.275, 0.292),
                        (0.280, 0.527),
                        (0.244, 0.879),
                    ]

            flow.screen = FakeScreen()

            selected = flow._left_selected_checkbox_y_ratios(WindowRect(0, 0, 1926, 1026), "probe")

            self.assertEqual(selected, [0.879])

    def test_sentinel_ocr_uses_clicked_row_y_when_local_blue_checkbox_detection_fails(self):
        with tempfile.TemporaryDirectory() as d:
            flow = ForwardFlow(AppConfig(), screenshot_dir=d, install_stop_hotkey=False)
            rect = WindowRect(0, 0, 1600, 900)
            region = flow._left_candidate_region(rect)
            selected_y = [0.800, 0.700]

            class FakeScreen:
                def save_checkpoint(self, *_args, **_kwargs):
                    return Path(d) / "fake.png"

                def find_selected_checkbox_ratios(self, *_args, **_kwargs):
                    return []

                def ocr_lines(self, *_args, **_kwargs):
                    return [
                        OcrLine("底部群", 100, round(selected_y[0] * rect.height - region.top - 10), 80, 20),
                        OcrLine("上方群", 100, round(selected_y[1] * rect.height - region.top - 10), 80, 20),
                    ]

            flow.screen = FakeScreen()

            selected = flow._read_left_selected_recipients(rect, selected_y)

            self.assertEqual([item.name for item in selected], ["底部群", "上方群"])

    def test_recipient_candidates_stop_without_blind_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            flow = ForwardFlow(AppConfig(), screenshot_dir=d, install_stop_hotkey=False)
            flow._last_recipient_picker_rect = Region(left=420, top=84, width=1080, height=900)

            class FakeScreen:
                def save_checkpoint(self, *_args, **_kwargs):
                    return Path(d) / "fake.png"

                def find_checkbox_outline_ratios(self, *_args, **_kwargs):
                    return []

            flow.screen = FakeScreen()
            with self.assertRaisesRegex(RuntimeError, "足够的会话复选框"):
                flow._recipient_checkbox_points_bottom_to_top(2, WindowRect(0, 0, 1600, 900), 1)

    def test_recipient_candidates_use_detected_scaled_checkbox_column(self):
        with tempfile.TemporaryDirectory() as d:
            flow = ForwardFlow(AppConfig(), screenshot_dir=d, install_stop_hotkey=False)
            flow._last_recipient_picker_rect = Region(left=640, top=220, width=1280, height=980)

            class FakeScreen:
                def save_checkpoint(self, *_args, **_kwargs):
                    return Path(d) / "fake.png"

                def find_checkbox_outline_ratios(self, *_args, **_kwargs):
                    return [
                        (0.359, 0.368),
                        (0.359, 0.414),
                        (0.359, 0.458),
                        (0.359, 0.504),
                        (0.359, 0.549),
                        (0.359, 0.594),
                        (0.359, 0.639),
                        (0.359, 0.685),
                        (0.359, 0.729),
                    ]

            flow.screen = FakeScreen()

            selected = flow._recipient_checkbox_points_bottom_to_top(9, WindowRect(-2, -2, 2564, 1384), 1)

            self.assertEqual(
                selected,
                [
                    (0.359, 0.729),
                    (0.359, 0.685),
                    (0.359, 0.639),
                    (0.359, 0.594),
                    (0.359, 0.549),
                    (0.359, 0.504),
                    (0.359, 0.458),
                    (0.359, 0.414),
                    (0.359, 0.368),
                ],
            )
            self.assertAlmostEqual(flow._recipient_checkbox_x_ratio(WindowRect(-2, -2, 2564, 1384)), 0.359)

    def test_final_send_button_uses_ocr_detected_button_location(self):
        with tempfile.TemporaryDirectory() as d:
            flow = ForwardFlow(AppConfig(), screenshot_dir=d, install_stop_hotkey=False)
            rect = WindowRect(-2, -2, 2564, 1384)
            region = flow._final_send_button_region(rect)

            class FakeScreen:
                def __init__(self):
                    self.saved_regions = []

                def save_checkpoint(self, *_args, **kwargs):
                    self.saved_regions.append(kwargs.get("region"))
                    return Path(d) / "send_button_region.png"

                def ocr_lines(self, *, image_path):
                    return [
                        OcrLine("分别发送", 280, 120, 75, 35),
                        OcrLine("取消", 470, 120, 40, 27),
                    ]

                def image_size(self, _image_path):
                    return (region.width, region.height)

            fake_screen = FakeScreen()
            flow.screen = fake_screen

            ratio = flow._detect_final_send_button_ratio(rect)

            expected_x = ((region.left - rect.left) + 280 + 75 / 2) / rect.width
            expected_y = ((region.top - rect.top) + 120 + 35 / 2) / rect.height
            self.assertAlmostEqual(ratio[0], expected_x, places=3)
            self.assertAlmostEqual(ratio[1], expected_y, places=3)
            self.assertEqual(fake_screen.saved_regions, [region])

    def test_final_send_button_falls_back_to_full_window_when_region_misses(self):
        with tempfile.TemporaryDirectory() as d:
            flow = ForwardFlow(AppConfig(), screenshot_dir=d, install_stop_hotkey=False)
            rect = WindowRect(-2, -2, 2564, 1384)

            class FakeScreen:
                def __init__(self):
                    self.saved_regions = []
                    self.calls = 0

                def save_checkpoint(self, *_args, **kwargs):
                    self.saved_regions.append(kwargs.get("region"))
                    self.calls += 1
                    path = Path(d) / f"send_button_{self.calls}.png"
                    Image.new("RGB", (200, 120), "white").save(path)
                    return path

                def ocr_lines(self, *, image_path):
                    if image_path.name == "send_button_1.png":
                        return []
                    return [
                        OcrLine("分别发送给", 1309, 378, 91, 30),
                        OcrLine("分别发送", 1357, 976, 75, 35),
                    ]

                def image_size(self, image_path):
                    if image_path.name == "send_button_1.png":
                        region = flow._final_send_button_region(rect)
                        return (region.width, region.height)
                    return (2564, 1384)

            fake_screen = FakeScreen()
            flow.screen = fake_screen

            ratio = flow._detect_final_send_button_ratio(rect)

            self.assertAlmostEqual(ratio[0], (1357 + 75 / 2) / 2564, places=3)
            self.assertAlmostEqual(ratio[1], (976 + 35 / 2) / 1384, places=3)
            self.assertEqual(fake_screen.saved_regions[0], flow._final_send_button_region(rect))
            self.assertEqual(fake_screen.saved_regions[1], Region(rect.left, rect.top, rect.width, rect.height))

    def test_fast_path_reuses_cached_window_rect_after_first_batch(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        rect = WindowRect(10, 20, 1600, 900)
        flow._fast_path.ready = True
        flow._fast_path.window_rect = rect
        flow.window.locate = mock.Mock(side_effect=AssertionError("should not locate again"))
        flow.window.activate = mock.Mock(return_value=True)
        flow._sleep = mock.Mock()

        self.assertEqual(flow._locate_or_reuse_window(2), rect)
        flow.window.locate.assert_not_called()

    def test_fast_path_reuses_cached_recipient_checkbox_points(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        flow._fast_path.ready = True
        flow._fast_path.window_rect = WindowRect(0, 0, 1600, 900)
        flow._fast_path.recipient_checkbox_points_bottom_to_top = [
            (0.30, 0.80),
            (0.30, 0.74),
            (0.30, 0.68),
        ]
        flow._save_window_checkpoint = mock.Mock(side_effect=AssertionError("should not scan checkboxes again"))

        selected = flow._recipient_checkbox_points_bottom_to_top(2, flow._fast_path.window_rect, 2)

        self.assertEqual(selected, [(0.30, 0.80), (0.30, 0.74)])
        flow._save_window_checkpoint.assert_not_called()

    def test_fast_path_reuses_cached_final_send_button(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        rect = WindowRect(0, 0, 1600, 900)
        flow._fast_path.ready = True
        flow._fast_path.final_send_button_ratio = (0.56, 0.78)
        flow._detect_final_send_button_ratio = mock.Mock(side_effect=AssertionError("should not run OCR again"))
        flow.window.click_screen = mock.Mock(return_value=True)

        self.assertTrue(flow._click_final_send_button(rect))

        flow._detect_final_send_button_ratio.assert_not_called()
        flow.window.click_screen.assert_called_once_with(*rect.relative_point(0.56, 0.78))

    def test_preflight_reaches_final_send_button_without_clicking_it_and_caches_coordinates(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        rect = WindowRect(0, 0, 1600, 900)
        flow._locate_or_reuse_window = mock.Mock(return_value=rect)
        flow._assert_exact_source_selection = mock.Mock()
        flow._open_recipient_picker_from_source = mock.Mock()
        flow._scroll_recipient_picker_to_bottom = mock.Mock()
        flow._assert_recipient_picker_still_open = mock.Mock()
        flow._recipient_checkbox_points_bottom_to_top = mock.Mock(return_value=[(0.30, 0.80), (0.30, 0.74)])
        flow._click_recipient_checkbox = mock.Mock(return_value=True)
        flow._left_selected_checkbox_y_ratios = mock.Mock(return_value=[0.80, 0.74])
        flow._detect_final_send_button_ratio = mock.Mock(return_value=(0.56, 0.78))
        flow._click_final_send_button = mock.Mock(side_effect=AssertionError("preflight must not send"))
        flow._cancel_recipient_picker = mock.Mock()
        flow._sleep = mock.Mock()

        cache = flow.preflight_first_batch_until_final_send_button(2)

        self.assertTrue(cache.ready)
        self.assertEqual(cache.available_from_batch, 1)
        self.assertEqual(cache.window_rect, rect)
        self.assertEqual(cache.recipient_checkbox_points_bottom_to_top, [(0.30, 0.80), (0.30, 0.74)])
        self.assertEqual(cache.final_send_button_ratio, (0.56, 0.78))
        flow._scroll_recipient_picker_to_bottom.assert_called_once_with(rect, 1, require_scrollbar_thumb=True)
        flow._click_final_send_button.assert_not_called()
        flow._cancel_recipient_picker.assert_called_once_with(rect)

    def test_preflight_cache_can_be_reused_from_first_real_batch(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        rect = WindowRect(0, 0, 1600, 900)
        cache = ForwardFlow(AppConfig(), install_stop_hotkey=False).preflight_cache_from_values(
            window_rect=rect,
            recipient_checkbox_points_bottom_to_top=[(0.30, 0.80)],
            recipient_scroll_drag=((790, 300), (790, 760)),
            recipient_scroll_track=(790, 760),
            final_send_button_ratio=(0.56, 0.78),
        )

        flow.apply_preflight_cache(cache)

        self.assertTrue(flow._can_use_fast_path(1))

    def test_cancel_recipient_picker_clicks_cancel_button_even_when_escape_succeeds(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        rect = WindowRect(0, 0, 1600, 900)
        flow._fast_path.final_send_button_ratio = (0.56, 0.78)
        flow.screen.save_checkpoint = mock.Mock(return_value=Path("cancel.png"))
        flow.window.send_keys = mock.Mock(return_value=True)
        flow.window.click_screen = mock.Mock(return_value=True)
        flow._sleep = mock.Mock()

        flow._cancel_recipient_picker(rect)

        flow.window.send_keys.assert_not_called()
        flow.window.click_screen.assert_called_once_with(*rect.relative_point(0.685, 0.78))

    def test_recipient_picker_open_detection_falls_back_to_wide_region(self):
        with tempfile.TemporaryDirectory() as d:
            flow = ForwardFlow(AppConfig(), screenshot_dir=d, install_stop_hotkey=False)
            rect = WindowRect(0, 0, 1600, 900)
            flow._sleep = mock.Mock()

            class FakeScreen:
                def __init__(self):
                    self.saved_regions = []

                def save_checkpoint(self, name, *, region=None):
                    self.saved_regions.append(region)
                    path = Path(d) / f"{name}.png"
                    Image.new("RGB", (120, 60), "white").save(path)
                    return path

                def ocr_lines(self, *, image_path):
                    if image_path.name.endswith("_wide.png"):
                        return [OcrLine("发送给", 10, 10, 40, 20)]
                    return []

            class FakeWindow:
                def click_relative(self, *_args):
                    return True

            fake_screen = FakeScreen()
            flow.screen = fake_screen
            flow.window = FakeWindow()

            flow._open_recipient_picker_from_source(rect, 1)

            self.assertIn(flow._recipient_picker_title_region(rect), fake_screen.saved_regions)
            self.assertIn(flow._recipient_picker_wide_region(rect), fake_screen.saved_regions)

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

    def test_recipient_list_compare_region_is_smaller_than_window(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        rect = WindowRect(-4, -4, 2888, 1712)

        region = flow._recipient_list_compare_region(rect)

        self.assertLess(region.width, rect.width // 3)
        self.assertLess(region.height, rect.height)

    def test_recipient_scrollbar_drag_spans_track(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        rect = WindowRect(-4, -4, 2888, 1712)

        candidates = flow._recipient_scrollbar_drag_candidates(rect)

        self.assertEqual(len(candidates), 4)
        for start, end in candidates:
            self.assertEqual(start[0], end[0])
            self.assertGreater(end[1] - start[1], rect.height // 3)
            self.assertGreater(start[1], rect.top + rect.height * 0.27)

    def test_detects_recipient_scrollbar_thumb_from_window_screenshot(self):
        with tempfile.TemporaryDirectory() as d:
            image_path = Path(d) / "scrollbar.png"
            image = Image.new("RGB", (1920, 1080), (248, 249, 250))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((942, 654, 951, 921), radius=4, fill=(168, 173, 178))
            image.save(image_path)

            flow = ForwardFlow(AppConfig(), screenshot_dir=d, install_stop_hotkey=False)
            thumb = flow._detect_recipient_scrollbar_thumb(image_path, WindowRect(0, 0, 1920, 1080))

            self.assertIsNotNone(thumb)
            assert thumb is not None
            self.assertAlmostEqual(thumb[0], 946.5 / 1920, places=3)
            self.assertAlmostEqual(thumb[1], 654 / 1080, places=3)
            self.assertAlmostEqual(thumb[2], 922 / 1080, places=3)

    def test_detects_recipient_scrollbar_thumb_when_picker_is_wide(self):
        with tempfile.TemporaryDirectory() as d:
            image_path = Path(d) / "wide_picker_scrollbar.png"
            image = Image.new("RGB", (2888, 1712), (248, 249, 250))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((1872, 532, 1884, 896), radius=4, fill=(168, 173, 178))
            image.save(image_path)

            flow = ForwardFlow(AppConfig(), screenshot_dir=d, install_stop_hotkey=False)
            picker_rect = Region(left=429, top=68, width=2459, height=1610)
            thumb = flow._detect_recipient_scrollbar_thumb(
                image_path,
                WindowRect(0, 0, 2888, 1712),
                picker_rect=picker_rect,
            )

            self.assertIsNotNone(thumb)
            assert thumb is not None
            self.assertAlmostEqual(thumb[0], 1878 / 2888, places=3)
            self.assertAlmostEqual(thumb[1], 532 / 1712, places=3)
            self.assertAlmostEqual(thumb[2], 897 / 1712, places=3)

    def test_recipient_scrollbar_drag_starts_at_detected_thumb_top_and_ends_at_track_bottom(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        rect = WindowRect(0, 0, 1920, 1080)
        thumb = (0.493, 0.270, 0.300)

        candidates = flow._recipient_scrollbar_drag_candidates(rect, thumb=thumb)

        self.assertEqual(len(candidates), 1)
        start, end = candidates[0]
        self.assertEqual(start[0], end[0])
        self.assertAlmostEqual(start[1], round(rect.height * 0.270), delta=2)
        self.assertEqual(end[1], round(rect.height * 0.855))

    def test_recipient_scrollbar_drag_uses_detected_picker_bottom(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        rect = WindowRect(0, 0, 1920, 1080)
        picker_rect = Region(left=520, top=160, width=900, height=810)
        thumb = (0.493, 0.270, 0.300)

        candidates = flow._recipient_scrollbar_drag_candidates(rect, thumb=thumb, picker_rect=picker_rect)

        self.assertEqual(len(candidates), 1)
        start, end = candidates[0]
        self.assertAlmostEqual(start[1], round(rect.height * 0.270), delta=2)
        self.assertEqual(end[1], picker_rect.top + picker_rect.height)

    def test_scroll_recipient_picker_reveals_scrollbar_before_detection(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        rect = WindowRect(0, 0, 1920, 1080)
        events = []
        flow._save_recipient_list_checkpoint = mock.Mock(return_value=Path("before.png"))
        flow._save_window_checkpoint = mock.Mock(return_value=Path("scrollbar.png"))
        flow._reveal_recipient_scrollbar = mock.Mock(side_effect=lambda *_args: events.append("reveal"))
        flow._detect_recipient_scrollbar_thumb = mock.Mock(
            side_effect=lambda *_args, **_kwargs: events.append("detect") or (0.493, 0.620, 0.855)
        )
        flow._sleep = mock.Mock()

        flow._scroll_recipient_picker_to_bottom(rect, 1, require_scrollbar_thumb=True)

        self.assertEqual(events, ["reveal", "detect"])
        self.assertIsNotNone(flow._fast_path.recipient_scroll_drag)
        self.assertEqual(flow._fast_path.recipient_scroll_track, rect.relative_point(0.494, 0.855))

    def test_scroll_recipient_picker_uses_detected_picker_bottom_as_drag_end_without_track_click(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        rect = WindowRect(0, 0, 1920, 1080)
        picker_rect = Region(left=520, top=160, width=900, height=810)
        flow._save_recipient_list_checkpoint = mock.Mock(return_value=Path("before.png"))
        flow._save_window_checkpoint = mock.Mock(return_value=Path("scrollbar.png"))
        flow._detect_recipient_picker_rect = mock.Mock(return_value=picker_rect)
        flow._reveal_recipient_scrollbar = mock.Mock()
        flow._detect_recipient_scrollbar_thumb = mock.Mock(return_value=(0.493, 0.620, 0.900))
        flow.window.click_screen = mock.Mock(return_value=True)
        flow._sleep = mock.Mock()

        flow._scroll_recipient_picker_to_bottom(rect, 1, require_scrollbar_thumb=True)

        self.assertEqual(flow._fast_path.recipient_scroll_track, (826, 970))
        flow.window.click_screen.assert_not_called()

    def test_scroll_recipient_picker_requires_detected_scrollbar_during_preflight(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        rect = WindowRect(0, 0, 1920, 1080)
        flow._save_recipient_list_checkpoint = mock.Mock(return_value=Path("before.png"))
        flow._save_window_checkpoint = mock.Mock(return_value=Path("scrollbar.png"))
        flow._reveal_recipient_scrollbar = mock.Mock(return_value=(700, 600))
        flow._detect_recipient_scrollbar_thumb = mock.Mock(return_value=None)
        flow._sleep = mock.Mock()

        with self.assertRaisesRegex(RuntimeError, "screenshot=scrollbar.png"):
            flow._scroll_recipient_picker_to_bottom(rect, 1, require_scrollbar_thumb=True)

    def test_scroll_recipient_picker_accepts_stable_bottom_when_detected_thumb_ratio_is_below_old_fixed_threshold(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        rect = WindowRect(-4, -4, 2888, 1712)
        list_paths = [Path(f"list_{index}.png") for index in range(7)]
        scrollbar_paths = [Path(f"scrollbar_{index}.png") for index in range(7)]
        flow._save_recipient_list_checkpoint = mock.Mock(side_effect=list_paths)
        flow._save_window_checkpoint = mock.Mock(side_effect=scrollbar_paths)
        flow._detect_recipient_scrollbar_thumb = mock.Mock(
            side_effect=[
                (0.494, 0.311, 0.523),
                (0.494, 0.638, 0.846),
                (0.494, 0.638, 0.846),
                (0.494, 0.638, 0.846),
                (0.494, 0.638, 0.846),
                (0.494, 0.638, 0.846),
                (0.494, 0.638, 0.846),
            ]
        )
        flow._recipient_list_image_difference = mock.Mock(side_effect=[17.2, 0.0, 0.0, 0.0, 0.0, 0.0])
        flow.window.drag_screen = mock.Mock(return_value=True)
        flow.window.click_screen = mock.Mock(return_value=True)
        flow._reveal_recipient_scrollbar = mock.Mock(return_value=(1050, 960))
        flow._sleep = mock.Mock()

        flow._scroll_recipient_picker_to_bottom(rect, 1, require_scrollbar_thumb=True)

        self.assertEqual(flow._fast_path.recipient_scroll_track, rect.relative_point(0.494, 0.855))
        self.assertIsNotNone(flow._fast_path.recipient_scroll_drag)

    def test_scroll_recipient_picker_continues_after_bottom_drag_without_stable_list(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        rect = WindowRect(0, 0, 1920, 1080)
        list_paths = [Path(f"list_{index}.png") for index in range(10)]
        scrollbar_paths = [Path(f"scrollbar_{index}.png") for index in range(10)]
        flow._save_recipient_list_checkpoint = mock.Mock(side_effect=list_paths)
        flow._save_window_checkpoint = mock.Mock(side_effect=scrollbar_paths)
        flow._detect_recipient_scrollbar_thumb = mock.Mock(
            side_effect=[
                (0.494, 0.300, 0.700),
                *((0.494, 0.560, 0.830) for _ in range(9)),
            ]
        )
        flow._recipient_list_image_difference = mock.Mock(side_effect=[18.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        flow.window.drag_screen = mock.Mock(return_value=True)
        flow.window.click_screen = mock.Mock(return_value=True)
        flow._reveal_recipient_scrollbar = mock.Mock(return_value=(700, 600))
        flow._sleep = mock.Mock()

        flow._scroll_recipient_picker_to_bottom(rect, 1, require_scrollbar_thumb=True)

        self.assertEqual(flow._fast_path.recipient_scroll_track, rect.relative_point(0.494, 0.855))
        self.assertIsNotNone(flow._fast_path.recipient_scroll_drag)

    def test_fast_path_scroll_uses_cached_scrollbar_coordinates_without_detection(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        rect = WindowRect(0, 0, 1920, 1080)
        flow._fast_path.ready = True
        flow._fast_path.available_from_batch = 1
        flow._fast_path.window_rect = rect
        flow._fast_path.recipient_scroll_drag = ((946, 650), (946, 800))
        flow._fast_path.recipient_scroll_track = (946, 923)
        flow.window.drag_screen = mock.Mock(return_value=True)
        flow.window.click_screen = mock.Mock(return_value=True)
        flow._save_recipient_list_checkpoint = mock.Mock(return_value=Path("fast.png"))
        flow._detect_recipient_scrollbar_thumb = mock.Mock(side_effect=AssertionError("should use cached coordinates"))
        flow._reveal_recipient_scrollbar = mock.Mock(side_effect=AssertionError("should use cached coordinates"))
        flow._sleep = mock.Mock()

        flow._scroll_recipient_picker_to_bottom(rect, 1)

        flow.window.drag_screen.assert_called_once_with(946, 650, 946, 800, duration=0.25)
        flow.window.click_screen.assert_not_called()
        flow._detect_recipient_scrollbar_thumb.assert_not_called()

    def test_source_context_menu_candidates_use_same_message_rows(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        flow.source_checkbox_y_ratios = [0.708, 0.527, 0.117]

        candidates = flow._source_context_menu_candidates()

        self.assertEqual(
            candidates,
            [
                (0.117, 0.90, 0.117),
                (0.117, 0.86, 0.117),
                (0.117, 0.94, 0.117),
                (0.117, 0.75, 0.117),
                (0.527, 0.90, 0.527),
                (0.527, 0.86, 0.527),
                (0.527, 0.94, 0.527),
                (0.527, 0.75, 0.527),
                (0.708, 0.90, 0.708),
                (0.708, 0.86, 0.708),
                (0.708, 0.94, 0.708),
                (0.708, 0.75, 0.708),
            ],
        )

    def test_source_checkbox_detection_excludes_title_and_toolbar_blue_items(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)

        selected = flow._select_source_checkbox_column(
            [
                (0.300, 0.043),
                (0.263, 0.126),
                (0.263, 0.307),
                (0.263, 0.527),
                (0.263, 0.708),
                (0.271, 0.824),
                (0.285, 0.825),
                (0.312, 0.825),
                (0.320, 0.826),
            ]
        )

        self.assertEqual(
            selected,
            [(0.263, 0.126), (0.263, 0.307), (0.263, 0.527), (0.263, 0.708)],
        )

    def test_reselect_source_messages_clicks_remaining_recorded_rows(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        flow.source_checkbox_x_ratio = 0.263
        flow.source_checkbox_y_ratios = [0.708, 0.527, 0.307, 0.126]
        flow.window.click_screen = mock.Mock(return_value=True)
        flow.screen.save_checkpoint = mock.Mock(return_value=Path("probe.png"))
        flow._enter_multiselect_from_source = mock.Mock(return_value=0.126)
        flow._verify_source_messages_selected = mock.Mock(return_value=True)
        rect = WindowRect(0, 0, 2000, 1000)

        flow._reselect_source_messages(rect)

        self.assertEqual(
            [call.args for call in flow.window.click_screen.call_args_list],
            [
                rect.relative_point(0.263, 0.708),
                rect.relative_point(0.263, 0.527),
                rect.relative_point(0.263, 0.307),
            ],
        )

    def test_record_exact_source_selection_rejects_changed_count(self):
        flow = ForwardFlow(
            AppConfig(),
            real_send_allowed=True,
            install_stop_hotkey=False,
        )
        flow.source_checkbox_y_ratios = [0.708, 0.527, 0.307, 0.126]
        flow._source_selected_checkbox_ratios_in_window = mock.Mock(
            return_value=[(0.263, 0.708), (0.263, 0.527), (0.263, 0.307)]
        )
        flow._source_selected_checkbox_ratios_from_fullscreen = mock.Mock(return_value=[])

        with self.assertRaisesRegex(RuntimeError, "检查阶段记录的 4 个"):
            flow._record_exact_source_selection(
                Path("probe.png"),
                rect=WindowRect(0, 0, 2000, 1000),
            )

    def test_source_context_menu_region_is_local_to_click(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        rect = WindowRect(0, 0, 2000, 1000)

        region = flow._source_context_menu_region(rect, 0.75, 0.50)

        self.assertLess(region.width * region.height, rect.width * rect.height * 0.20)
        self.assertLessEqual(region.left, 1500)
        self.assertGreaterEqual(region.left + region.width, 1500)

    def test_source_context_menu_region_covers_popup_around_right_bubble(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        rect = WindowRect(-2, -2, 2564, 1384)
        click_x, click_y = rect.relative_point(0.90, 0.643)

        region = flow._source_context_menu_region(rect, 0.90, 0.643)

        self.assertLessEqual(region.left, click_x - round(rect.width * 0.20))
        self.assertGreaterEqual(region.left + region.width, min(rect.right, click_x + round(rect.width * 0.10)))
        self.assertLessEqual(region.top, click_y - round(rect.height * 0.20))
        self.assertGreaterEqual(region.top + region.height, min(rect.bottom, click_y + round(rect.height * 0.30)))

    def test_checkbox_scan_region_has_component_padding(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)

        region = flow._expanded_checkbox_scan_region(0.285, 0.330, 0.330, 0.835)

        self.assertLess(region[0], 0.285)
        self.assertLess(region[1], 0.330)
        self.assertGreater(region[0] + region[2], 0.330)
        self.assertGreater(region[1] + region[3], 0.835)

    def test_wide_recipient_checkbox_bounds_include_nine_visible_rows(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        rect = WindowRect(-3, -3, 1926, 1014)

        min_y, max_y = flow._recipient_checkbox_y_bounds(rect)
        rows = flow._recipient_checkbox_rows_bottom_to_top(9, rect)

        self.assertLessEqual(min_y, 0.285)
        self.assertGreaterEqual(max_y, 0.876)
        self.assertEqual(len(rows), 9)
        self.assertAlmostEqual(rows[0], 0.876, places=3)
        self.assertAlmostEqual(rows[-1], 0.285, places=3)

    def test_wide_recipient_checkbox_bounds_include_1920_150_percent_column(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        rect = WindowRect(0, 0, 1920, 1080)

        min_x, max_x = flow._recipient_checkbox_x_bounds(rect)
        min_y, max_y = flow._recipient_checkbox_y_bounds(rect)

        self.assertLessEqual(min_x, 0.246)
        self.assertGreaterEqual(max_x, 0.246)
        self.assertLessEqual(min_y, 0.269)
        self.assertGreaterEqual(max_y, 0.825)

    def test_recipient_candidates_use_picker_left_quarter_and_prefer_checkbox_left_of_avatar(self):
        with tempfile.TemporaryDirectory() as d:
            flow = ForwardFlow(AppConfig(), screenshot_dir=d, install_stop_hotkey=False)
            flow._last_recipient_picker_rect = Region(left=420, top=84, width=1080, height=900)
            captured_scan_regions = []

            class FakeScreen:
                def save_checkpoint(self, *_args, **_kwargs):
                    return Path(d) / "fake.png"

                def find_checkbox_outline_ratios(self, *_args, **kwargs):
                    captured_scan_regions.append(kwargs["scan_region_ratio"])
                    row_y = [0.285, 0.358, 0.432, 0.506, 0.580, 0.654, 0.728, 0.802, 0.876]
                    checkbox_column = [(0.244, y) for y in row_y]
                    avatar_like = [(0.272, y) for y in row_y]
                    outside_left_quarter = [(0.420, y) for y in row_y]
                    return checkbox_column + avatar_like + outside_left_quarter

            flow.screen = FakeScreen()

            selected = flow._recipient_checkbox_points_bottom_to_top(3, WindowRect(0, 0, 1920, 1080), 1)

            self.assertEqual(selected, [(0.244, 0.876), (0.244, 0.802), (0.244, 0.728)])
            self.assertTrue(captured_scan_regions)
            scan_left, _scan_top, scan_width, _scan_height = captured_scan_regions[0]
            self.assertLessEqual(scan_left, 420 / 1920)
            self.assertLessEqual(scan_left + scan_width, (420 + 1080 * 0.25) / 1920 + 0.020)

    def test_recipient_candidates_reject_avatar_column_before_clicking(self):
        with tempfile.TemporaryDirectory() as d:
            image_path = Path(d) / "recipient_candidates.png"
            image = Image.new("RGB", (1920, 1080), (246, 248, 251))
            draw = ImageDraw.Draw(image)
            row_y = [0.285, 0.358, 0.432, 0.506, 0.580, 0.654, 0.728, 0.802, 0.876]
            checkbox_column = [(0.244, y) for y in row_y[-3:]]
            avatar_column = [(0.272, y) for y in row_y]
            for x_ratio, y_ratio in checkbox_column:
                x = round(x_ratio * 1920)
                y = round(y_ratio * 1080)
                draw.rectangle((x - 5, y - 5, x + 5, y + 5), fill=(252, 253, 254))
            for x_ratio, y_ratio in avatar_column:
                x = round(x_ratio * 1920)
                y = round(y_ratio * 1080)
                draw.rectangle((x - 5, y - 5, x + 5, y + 5), fill=(70, 88, 145))
            image.save(image_path)

            flow = ForwardFlow(AppConfig(), screenshot_dir=d, install_stop_hotkey=False)
            flow._last_recipient_picker_rect = Region(left=420, top=84, width=1080, height=900)

            class FakeScreen:
                def save_checkpoint(self, *_args, **_kwargs):
                    return image_path

                def find_checkbox_outline_ratios(self, *_args, **_kwargs):
                    return checkbox_column + avatar_column

            flow.screen = FakeScreen()

            selected = flow._recipient_checkbox_points_bottom_to_top(3, WindowRect(0, 0, 1920, 1080), 1)

            self.assertEqual(selected, list(reversed(checkbox_column)))

    def test_recipient_candidates_choose_real_nine_row_column_over_left_noise(self):
        with tempfile.TemporaryDirectory() as d:
            flow = ForwardFlow(AppConfig(), screenshot_dir=d, install_stop_hotkey=False)
            flow._last_recipient_picker_rect = Region(left=514, top=121, width=1489, height=1331)
            raw_points = [
                (0.2092, 0.3078),
                (0.2444, 0.3079),
                (0.2444, 0.3740),
                (0.2092, 0.3911),
                (0.2444, 0.4401),
                (0.2092, 0.4745),
                (0.2444, 0.5063),
                (0.2092, 0.5578),
                (0.2444, 0.5724),
                (0.2444, 0.6386),
                (0.2092, 0.6411),
                (0.2444, 0.7047),
                (0.2092, 0.7245),
                (0.2444, 0.7708),
                (0.2092, 0.8078),
                (0.2444, 0.8370),
                (0.2261, 0.8903),
                (0.2092, 0.8911),
            ]

            class FakeScreen:
                def save_checkpoint(self, *_args, **_kwargs):
                    return Path(d) / "fake.png"

                def find_checkbox_outline_ratios(self, *_args, **_kwargs):
                    return raw_points

            flow.screen = FakeScreen()

            selected = flow._recipient_checkbox_points_bottom_to_top(9, WindowRect(0, 0, 2568, 1512), 1)

            self.assertEqual(len(selected), 9)
            self.assertTrue(all(abs(x - 0.2444) < 0.001 for x, _y in selected))

    def test_recipient_candidates_require_picker_rect_to_avoid_background_matches(self):
        with tempfile.TemporaryDirectory() as d:
            flow = ForwardFlow(AppConfig(), screenshot_dir=d, install_stop_hotkey=False)
            scanned = False

            class FakeScreen:
                def save_checkpoint(self, *_args, **_kwargs):
                    return Path(d) / "fake.png"

                def find_checkbox_outline_ratios(self, *_args, **_kwargs):
                    nonlocal scanned
                    scanned = True
                    return [(0.246, 0.876), (0.246, 0.802), (0.246, 0.728)]

            flow.screen = FakeScreen()

            with self.assertRaises(RuntimeError):
                flow._recipient_checkbox_points_bottom_to_top(3, WindowRect(0, 0, 1920, 1080), 1)

            self.assertFalse(scanned)

    def test_recipient_picker_rect_detection_allows_left_shifted_dialog(self):
        with tempfile.TemporaryDirectory() as d:
            image_path = Path(d) / "left_shifted_picker.png"
            image = Image.new("RGB", (1926, 1026), (238, 242, 247))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((276, 92, 1648, 934), radius=10, fill=(248, 249, 251), outline=(205, 209, 214))
            draw.rectangle((314, 130, 924, 178), fill=(239, 241, 245))
            image.save(image_path)
            flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)

            picker = flow._detect_recipient_picker_rect(image_path, WindowRect(0, 0, 1926, 1026))

            self.assertIsNotNone(picker)
            assert picker is not None
            self.assertLess(picker.left, 330)

    def test_recipient_picker_rect_detection_scans_full_page_width(self):
        with tempfile.TemporaryDirectory() as d:
            image_path = Path(d) / "very_wide_picker.png"
            image = Image.new("RGB", (1926, 1026), (238, 242, 247))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((24, 92, 1902, 934), radius=10, fill=(248, 249, 251), outline=(205, 209, 214))
            draw.rectangle((64, 130, 900, 178), fill=(239, 241, 245))
            image.save(image_path)
            flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)

            picker = flow._detect_recipient_picker_rect(image_path, WindowRect(0, 0, 1926, 1026))

            self.assertIsNotNone(picker)
            assert picker is not None
            self.assertLess(picker.left, 80)

    def test_recipient_picker_rect_detection_refines_left_edge_away_from_background_list(self):
        with tempfile.TemporaryDirectory() as d:
            image_path = Path(d) / "picker_over_light_background.png"
            image = Image.new("RGB", (2888, 1712), (238, 242, 247))
            draw = ImageDraw.Draw(image)
            draw.rectangle((120, 0, 640, 1712), fill=(232, 238, 246))
            draw.rounded_rectangle((720, 296, 2168, 1418), radius=10, fill=(248, 249, 251), outline=(188, 193, 199))
            draw.rectangle((770, 348, 1400, 412), fill=(239, 241, 245))
            image.save(image_path)
            flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)

            picker = flow._detect_recipient_picker_rect(image_path, WindowRect(0, 0, 2888, 1712))

            self.assertIsNotNone(picker)
            assert picker is not None
            self.assertGreater(picker.left, 650)
            self.assertLess(picker.left, 780)

    def test_recipient_picker_rect_detection_uses_top_edge_before_search_box_edge(self):
        with tempfile.TemporaryDirectory() as d:
            image_path = Path(d) / "picker_top_edge.png"
            image = Image.new("RGB", (2888, 1712), (238, 242, 247))
            draw = ImageDraw.Draw(image)
            draw.rectangle((120, 0, 640, 1712), fill=(232, 238, 246))
            draw.rounded_rectangle((430, 296, 2450, 1418), radius=10, fill=(248, 249, 251), outline=(188, 193, 199))
            draw.rectangle((642, 348, 1400, 412), fill=(239, 241, 245))
            image.save(image_path)
            flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)

            picker = flow._detect_recipient_picker_rect(image_path, WindowRect(0, 0, 2888, 1712))

            self.assertIsNotNone(picker)
            assert picker is not None
            self.assertGreater(picker.left, 380)
            self.assertLess(picker.left, 500)

    def test_left_candidate_region_includes_selected_checkbox_column_and_rows(self):
        flow = ForwardFlow(AppConfig(), install_stop_hotkey=False)
        rect = WindowRect(-3, -3, 1926, 1014)
        region = flow._left_candidate_region(rect)
        checkbox_x = rect.left + rect.width * 0.275

        self.assertLessEqual(region.left, checkbox_x)
        self.assertGreaterEqual(region.left + region.width, rect.left + rect.width * 0.430)
        self.assertLessEqual(region.top, rect.top + rect.height * 0.285)
        self.assertGreaterEqual(region.top + region.height, rect.top + rect.height * 0.876)


if __name__ == "__main__":
    unittest.main()
