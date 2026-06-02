from pathlib import Path
import tempfile
import unittest

from wecom_rpa.calibration import crop_image, suggest_regions
from wecom_rpa.wecom_window import WindowRect
from wecom_rpa.screen import Region


class CalibrationTest(unittest.TestCase):
    def test_suggest_regions_are_inside_window(self):
        rect = WindowRect(100, 200, 1000, 800)
        regions = suggest_regions(rect)
        self.assertGreaterEqual(len(regions), 5)
        names = {r.name for r in regions}
        self.assertIn("search_box_area", names)
        self.assertIn("input_area", names)
        for r in regions:
            self.assertGreaterEqual(r.left, rect.left)
            self.assertGreaterEqual(r.top, rect.top)
            self.assertLessEqual(r.left + r.width, rect.right)
            self.assertLessEqual(r.top + r.height, rect.bottom)

    def test_crop_image_with_powershell_or_pillow(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src = root / "src.png"
            dst = root / "dst.png"
            try:
                from PIL import Image  # type: ignore
                img = Image.new("RGB", (20, 20), color="white")
                img.save(src)
            except Exception:
                self.skipTest("Pillow 不可用，跳过纯单元裁剪测试")
            crop_image(src, dst, Region(5, 5, 10, 10))
            self.assertTrue(dst.exists())


if __name__ == "__main__":
    unittest.main()
