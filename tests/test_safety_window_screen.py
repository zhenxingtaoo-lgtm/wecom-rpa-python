from pathlib import Path
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wecom_rpa.safety import StopController, assert_batch_selection_count
from wecom_rpa.screen import OcrLine, ScreenInspector
from wecom_rpa.wecom_window import WeComWindow, WindowRect
from wecom_rpa import powershell


class SafetyWindowScreenTest(unittest.TestCase):
    def test_stop_controller_manual_request(self):
        stop = StopController("ctrl+alt+q")
        self.assertFalse(stop.should_stop())
        stop.request_stop()
        self.assertTrue(stop.should_stop())
        stop.reset()
        self.assertFalse(stop.should_stop())

    def test_send_limit_and_batch_count_validation(self):
        assert_batch_selection_count(9, 9)
        with self.assertRaisesRegex(ValueError, "batch_size"):
            assert_batch_selection_count(1, 10)
        with self.assertRaisesRegex(ValueError, "选择数量"):
            assert_batch_selection_count(3, 2)

    def test_window_rect_relative_point_and_anchor(self):
        rect = WindowRect(left=100, top=200, width=800, height=600)
        self.assertEqual(rect.relative_point(0.5, 0.25), (500, 350))
        window = WeComWindow("企业微信", anchors={"send_button": [0.9, 0.8], "search_box": {"x_ratio": 0.5, "y_ratio": 0.2}})
        self.assertEqual(window.anchor_point("send_button", rect), (820, 680))
        self.assertEqual(window.anchor_point("search_box", rect), (500, 320))
        self.assertIsNone(window.anchor_point("missing", rect))

    def test_powershell_locator_prefers_main_window_without_topmost(self):
        script = WeComWindow("企业微信")._powershell_locator_script()

        self.assertIn("MainWindowHandle", script)
        self.assertIn("IsMainHandle", script)
        self.assertNotIn("[IntPtr](-1)", script)

    def test_powershell_locator_maximizes_window(self):
        script = WeComWindow("企业微信")._powershell_locator_script()

        self.assertIn("WorkingArea", script)
        self.assertIn("ShowWindow($h, 3)", script)
        self.assertNotIn("SetWindowPos($h", script)

    def test_scroll_chat_to_bottom_validates_repeats(self):
        window = WeComWindow("企业微信")
        with self.assertRaisesRegex(ValueError, "repeats"):
            window.scroll_chat_to_bottom(repeats=0)

    def test_screen_capture_safely_degrades_or_saves_png(self):
        with tempfile.TemporaryDirectory() as d:
            inspector = ScreenInspector(Path(d) / "screenshots")
            path = inspector.save_checkpoint("hello/world")
            self.assertTrue(path.exists())
            self.assertTrue(path.suffix in {".png", ".txt"})

    def test_mss_black_capture_falls_back_to_powershell(self):
        with tempfile.TemporaryDirectory() as d:
            inspector = ScreenInspector(Path(d) / "screenshots")
            fallback = Path(d) / "fallback.png"
            path = Path(d) / "capture.png"

            from PIL import Image

            Image.new("RGB", (8, 8), (12, 34, 56)).save(fallback)

            with mock.patch.object(inspector, "_capture_png_via_pyautogui", return_value=False), mock.patch.object(
                inspector, "_capture_png_via_powershell", side_effect=lambda target, _region: fallback.replace(target) or True
            ):
                inspector._save_checked_capture(Image.new("RGB", (8, 8), (0, 0, 0)), path, None, backend="mss")

            self.assertTrue(path.exists())
            self.assertFalse(inspector._is_nearly_black(path))

    def test_find_template_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            inspector = ScreenInspector(Path(d) / "screenshots", template_dir=Path(d) / "templates")
            self.assertIsNone(inspector.find_template("missing.png"))

    def test_selected_checkbox_detection_handles_high_dpi_screenshot(self):
        with tempfile.TemporaryDirectory() as d:
            from PIL import Image, ImageDraw

            image_path = Path(d) / "high_dpi.png"
            image = Image.new("RGB", (2880, 1800), (245, 247, 250))
            draw = ImageDraw.Draw(image)
            draw.rectangle((900, 1160, 935, 1195), fill=(0, 120, 230))
            draw.rectangle((900, 1510, 935, 1545), fill=(0, 120, 230))
            image.save(image_path)

            points = ScreenInspector(Path(d) / "screenshots").find_selected_checkbox_ratios(image_path)

            self.assertEqual(len(points), 2)
            self.assertAlmostEqual(points[0][0], 917.5 / 2880, places=3)
            self.assertAlmostEqual(points[0][1], 1177.5 / 1800, places=3)
            self.assertAlmostEqual(points[1][1], 1527.5 / 1800, places=3)

    def test_selected_checkbox_detection_limits_scan_region(self):
        with tempfile.TemporaryDirectory() as d:
            from PIL import Image, ImageDraw

            image_path = Path(d) / "region.png"
            image = Image.new("RGB", (1000, 1000), (245, 247, 250))
            draw = ImageDraw.Draw(image)
            draw.rectangle((100, 400, 119, 419), fill=(0, 120, 230))
            draw.rectangle((800, 400, 819, 419), fill=(0, 120, 230))
            image.save(image_path)

            points = ScreenInspector(Path(d) / "screenshots").find_selected_checkbox_ratios(
                image_path,
                scan_region_ratio=(0.70, 0.30, 0.25, 0.30),
            )

            self.assertEqual(len(points), 1)
            self.assertAlmostEqual(points[0][0], 809.5 / 1000, places=3)

    def test_source_checkbox_filter_excludes_select_to_here_marker(self):
        with tempfile.TemporaryDirectory() as d:
            from PIL import Image, ImageDraw

            image_path = Path(d) / "select_to_here.png"
            image = Image.new("RGB", (1440, 900), (245, 247, 250))
            draw = ImageDraw.Draw(image)
            blue = (20, 120, 235)
            checkbox_x = 380
            for center_y in (110, 430, 680):
                draw.rectangle(
                    (checkbox_x - 10, center_y - 10, checkbox_x + 10, center_y + 10),
                    fill=blue,
                )
            # Simulate the blue arrow label immediately to the right of the top marker.
            for offset in range(0, 95, 15):
                draw.rectangle(
                    (checkbox_x + 20 + offset, 101, checkbox_x + 28 + offset, 119),
                    fill=blue,
                )
            image.save(image_path)

            inspector = ScreenInspector(Path(d) / "screenshots")
            raw_points = inspector.find_selected_checkbox_ratios(image_path)
            filtered = inspector.filter_source_checkbox_marker_points(image_path, raw_points)

            self.assertEqual(len(filtered), 2)
            self.assertTrue(all(y_ratio > 0.30 for _x_ratio, y_ratio in filtered))

    def test_source_checkbox_filter_removes_all_marker_text_fragments(self):
        with tempfile.TemporaryDirectory() as d:
            from PIL import Image, ImageDraw

            image_path = Path(d) / "select_to_here_fragments.png"
            image = Image.new("RGB", (1440, 900), (245, 247, 250))
            draw = ImageDraw.Draw(image)
            blue = (20, 120, 235)
            draw.rectangle((370, 100, 390, 120), fill=blue)
            draw.rectangle((410, 101, 420, 119), fill=blue)
            draw.rectangle((445, 101, 455, 119), fill=blue)
            draw.rectangle((480, 101, 490, 119), fill=blue)
            draw.rectangle((370, 500, 390, 520), fill=blue)
            image.save(image_path)

            inspector = ScreenInspector(Path(d) / "screenshots")
            raw_points = inspector.find_selected_checkbox_ratios(image_path)
            filtered = inspector.filter_source_checkbox_marker_points(image_path, raw_points)

            self.assertEqual(len(filtered), 1)
            self.assertAlmostEqual(filtered[0][1], 510 / 900, places=2)

    def test_checkbox_outline_detection_handles_picker_rows(self):
        with tempfile.TemporaryDirectory() as d:
            from PIL import Image, ImageDraw

            image_path = Path(d) / "picker_rows.png"
            image = Image.new("RGB", (996, 856), (255, 255, 255))
            draw = ImageDraw.Draw(image)
            for top in (27, 127, 227):
                draw.rectangle((30, top, 61, top + 31), outline=(170, 170, 170), width=2)
            image.save(image_path)

            points = ScreenInspector(Path(d) / "screenshots").find_checkbox_outline_ratios(image_path)

            self.assertEqual(len(points), 3)
            self.assertAlmostEqual(points[0][0], 45.5 / 996, places=3)
            self.assertAlmostEqual(points[0][1], 42.5 / 856, places=3)
            self.assertAlmostEqual(points[2][1], 242.5 / 856, places=3)

    def test_windows_ocr_json_maps_to_lines(self):
        with tempfile.TemporaryDirectory() as d:
            inspector = ScreenInspector(Path(d) / "screenshots")
            fake = '[{"Text":"大 小 尘","Left":1,"Top":2,"Width":30,"Height":10}]'.encode("utf-8")

            class Result:
                returncode = 0
                stdout = fake
                stderr = b""

            with mock.patch("wecom_rpa.screen.powershell_exe", return_value=Path("powershell.exe")), mock.patch(
                "wecom_rpa.screen.run_powershell", return_value=Result()
            ):
                lines = inspector._ocr_lines_via_windows_ocr(Path("fake.png"))

            self.assertEqual(lines, [OcrLine("大 小 尘", 1, 2, 30, 10)])

    def test_hidden_subprocess_kwargs_hide_windows_console(self):
        if not hasattr(powershell.subprocess, "STARTUPINFO"):
            self.skipTest("Windows-only subprocess startup flags")

        with mock.patch.object(powershell.os, "name", "nt"):
            kwargs = powershell.hidden_subprocess_kwargs()

        self.assertIn("creationflags", kwargs)
        self.assertIn("startupinfo", kwargs)

    def test_windows_path_does_not_use_wslpath(self):
        with mock.patch.object(powershell.shutil, "which", side_effect=AssertionError("wslpath should not be used")):
            self.assertEqual(powershell.windows_path(Path("C:/tmp/a.ps1")), str(Path("C:/tmp/a.ps1")))

    def test_foreground_check_does_not_reactivate_active_window(self):
        window = WeComWindow("企业微信")
        window._last_hwnd = 123
        with mock.patch.object(window, "_is_last_window_foreground", return_value=True), mock.patch.object(
            window, "_activate_last_window", return_value=True
        ) as activate:
            self.assertTrue(window._ensure_foreground())
        activate.assert_not_called()

    def test_click_uses_pyautogui_without_powershell_when_available(self):
        window = WeComWindow("企业微信")
        with mock.patch.object(window, "_ensure_foreground", return_value=True), mock.patch.object(
            window, "_click_point_via_pyautogui", return_value=True
        ), mock.patch.object(window, "_click_point_via_powershell", return_value=True) as powershell_click:
            self.assertTrue(window.click_screen(100, 200))
        powershell_click.assert_not_called()

    def test_drag_uses_pyautogui(self):
        window = WeComWindow("企业微信")
        fake_position = mock.Mock(x=30, y=40)
        fake_pyautogui = mock.Mock()
        fake_pyautogui.position.return_value = fake_position

        with mock.patch.object(window, "_ensure_foreground", return_value=True), mock.patch.dict(
            "sys.modules", {"pyautogui": fake_pyautogui}
        ):
            self.assertTrue(window.drag_screen(10, 20, 30, 40, duration=0.25))

        self.assertEqual(
            fake_pyautogui.moveTo.call_args_list,
            [
                mock.call(10, 20, duration=0.08),
                mock.call(30, 40, duration=0.25),
            ],
        )
        fake_pyautogui.mouseDown.assert_called_once_with(button="left")
        fake_pyautogui.mouseUp.assert_called_once_with(button="left")

    def test_powershell_exe_does_not_fallback_to_wsl_mount(self):
        with mock.patch.object(powershell.shutil, "which", return_value=None), mock.patch.object(
            powershell.Path, "exists", side_effect=AssertionError("/mnt/c fallback should not be checked")
        ):
            self.assertIsNone(powershell.powershell_exe())

    def test_parse_paddleocr_result_maps_to_lines(self):
        inspector = ScreenInspector("screenshots")
        raw = [
            [
                ([[10, 20], [70, 20], [70, 40], [10, 40]], ("大小尘", 0.98)),
                ([[10, 50], [40, 50], [40, 70], [10, 70]], ("1", 0.92)),
            ]
        ]

        lines = inspector._parse_paddleocr_result(raw)

        self.assertEqual(
            lines,
            [
                OcrLine("大小尘", 10, 20, 60, 20, 0.98),
                OcrLine("1", 10, 50, 30, 20, 0.92),
            ],
        )

    def test_paddleocr_empty_result_does_not_fall_back_to_windows(self):
        inspector = ScreenInspector("screenshots", ocr_engine="paddleocr", ocr_fallback="windows")
        calls = []
        inspector._ocr_lines_via_paddleocr = lambda _image: []  # type: ignore[method-assign]
        inspector._ocr_lines_via_windows_ocr = lambda _image: calls.append("windows") or [OcrLine("fallback", 1, 2, 3, 4)]  # type: ignore[method-assign]

        lines = inspector.ocr_lines(image_path=Path("fake.png"))

        self.assertEqual(calls, [])
        self.assertEqual(lines, [])

    def test_paddleocr_failure_can_fall_back_to_windows(self):
        inspector = ScreenInspector("screenshots", ocr_engine="paddleocr", ocr_fallback="windows")
        calls = []

        def fail_paddle(_image):
            inspector._last_paddle_ocr_failed = True
            return []

        inspector._ocr_lines_via_paddleocr = fail_paddle  # type: ignore[method-assign]
        inspector._ocr_lines_via_windows_ocr = lambda _image: calls.append("windows") or [OcrLine("fallback", 1, 2, 3, 4)]  # type: ignore[method-assign]

        lines = inspector.ocr_lines(image_path=Path("fake.png"))

        self.assertEqual(calls, ["windows"])
        self.assertEqual(lines, [OcrLine("fallback", 1, 2, 3, 4)])

    def test_configured_paddle_model_root_requires_offline_files(self):
        with tempfile.TemporaryDirectory() as d:
            model_root = Path(d) / "models" / "paddleocr"
            model_root.mkdir(parents=True)
            inspector = ScreenInspector("screenshots", paddle_model_root=model_root)

            with self.assertRaisesRegex(FileNotFoundError, "离线模型不完整"):
                inspector.paddleocr_model_kwargs()


if __name__ == "__main__":
    unittest.main()
