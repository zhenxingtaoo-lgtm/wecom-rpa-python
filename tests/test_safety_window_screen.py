from pathlib import Path
import tempfile
import unittest

from wecom_rpa.safety import StopController, assert_batch_selection_count, assert_send_limit
from wecom_rpa.screen import OcrLine, ScreenInspector
from wecom_rpa.wecom_window import WeComWindow, WindowRect


class SafetyWindowScreenTest(unittest.TestCase):
    def test_stop_controller_manual_request(self):
        stop = StopController("ctrl+alt+q")
        self.assertFalse(stop.should_stop())
        stop.request_stop()
        self.assertTrue(stop.should_stop())
        stop.reset()
        self.assertFalse(stop.should_stop())

    def test_send_limit_and_batch_count_validation(self):
        self.assertEqual(assert_send_limit(5, 12), 5)
        self.assertEqual(assert_send_limit(20, 12), 12)
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

    def test_find_template_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            inspector = ScreenInspector(Path(d) / "screenshots", template_dir=Path(d) / "templates")
            self.assertIsNone(inspector.find_template("missing.png"))

    def test_windows_ocr_json_maps_to_lines(self):
        with tempfile.TemporaryDirectory() as d:
            inspector = ScreenInspector(Path(d) / "screenshots")
            fake = '[{"Text":"大 小 尘","Left":1,"Top":2,"Width":30,"Height":10}]'.encode("utf-8")

            class Result:
                returncode = 0
                stdout = fake
                stderr = b""

            import subprocess
            original_run = subprocess.run
            subprocess.run = lambda *_args, **_kwargs: Result()
            try:
                lines = inspector._ocr_lines_via_windows_ocr(Path("fake.png"))
            finally:
                subprocess.run = original_run

            self.assertEqual(lines, [OcrLine("大 小 尘", 1, 2, 30, 10)])

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

    def test_paddleocr_empty_result_falls_back_to_windows(self):
        inspector = ScreenInspector("screenshots", ocr_engine="paddleocr", ocr_fallback="windows")
        calls = []
        inspector._ocr_lines_via_paddleocr = lambda _image: []  # type: ignore[method-assign]
        inspector._ocr_lines_via_windows_ocr = lambda _image: calls.append("windows") or [OcrLine("fallback", 1, 2, 3, 4)]  # type: ignore[method-assign]

        lines = inspector.ocr_lines(image_path=Path("fake.png"))

        self.assertEqual(calls, ["windows"])
        self.assertEqual(lines, [OcrLine("fallback", 1, 2, 3, 4)])


if __name__ == "__main__":
    unittest.main()
