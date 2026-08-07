import unittest
from pathlib import Path

from wecom_rpa.gui import (
    convert_fullscreen_checkbox_ratios_to_window,
    detect_forward_button_ratio,
    select_source_checkbox_column,
)
from wecom_rpa.screen import OcrLine
from wecom_rpa.wecom_window import WindowRect


class GuiScalingTest(unittest.TestCase):
    def test_source_checkbox_column_accepts_left_edge_on_scaled_desktop(self):
        points = [
            (0.0112, 0.0242),
            (0.1787, 0.6170),
            (0.1788, 0.7559),
            (0.3619, 0.9740),
        ]

        selected = select_source_checkbox_column(points)

        self.assertEqual(selected, [(0.1787, 0.6170), (0.1788, 0.7559)])

    def test_fullscreen_checkbox_conversion_accepts_left_edge_on_scaled_desktop(self):
        class FakeInspector:
            def find_selected_checkbox_ratios(self, _image_path):
                return [(0.1787, 0.6170), (0.1788, 0.7559)]

            def filter_source_checkbox_marker_points(self, _image_path, points):
                return points

        rect = WindowRect(left=-2, top=-2, width=2564, height=1384)

        converted = convert_fullscreen_checkbox_ratios_to_window(
            FakeInspector(),
            Path("fake.png"),
            rect,
            (2560, 1440),
        )

        self.assertEqual(len(converted), 2)
        self.assertAlmostEqual(converted[0][0], ((0.1787 * 2560) + 2) / 2564, places=3)
        self.assertAlmostEqual(converted[0][1], ((0.6170 * 1440) + 2) / 1384, places=3)

    def test_forward_button_ocr_maps_physical_screenshot_to_logical_window_rect(self):
        class FakeInspector:
            def ocr_lines(self, *, image_path):
                return [
                    OcrLine(
                        "逐条转发",
                        left=round(0.47 * 2560 - 50),
                        top=round(0.90 * 1440 - 12),
                        width=100,
                        height=24,
                    )
                ]

            def image_size(self, image_path):
                return (2560, 1440)

        rect = WindowRect(left=0, top=0, width=2048, height=1152)

        ratio = detect_forward_button_ratio(FakeInspector(), Path("fake.png"), rect)

        self.assertIsNotNone(ratio)
        self.assertAlmostEqual(ratio[0], 0.47, places=3)
        self.assertAlmostEqual(ratio[1], 0.875, places=3)


if __name__ == "__main__":
    unittest.main()
