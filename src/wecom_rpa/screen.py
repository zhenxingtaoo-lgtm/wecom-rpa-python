from __future__ import annotations

import logging
import json
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .powershell import decode_process_output, powershell_exe, run_powershell, windows_path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Region:
    left: int
    top: int
    width: int
    height: int

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.width, self.height)


@dataclass(frozen=True)
class TemplateMatch:
    template_name: str
    confidence: float
    left: int
    top: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)


@dataclass(frozen=True)
class OcrLine:
    text: str
    left: int
    top: int
    width: int
    height: int
    confidence: float | None = None

    @property
    def center_y(self) -> int:
        return self.top + self.height // 2


class ScreenInspector:
    """截图 / 模板匹配 / OCR 封装。

    设计为“能实机就实机，缺依赖就安全降级”：在非 Windows/无 GUI/未安装
    mss、Pillow、opencv 时，不做点击，不抛出环境类异常，而是写占位文件，
    便于 CI 和 dry-run 测试继续运行。
    """

    def __init__(
        self,
        screenshot_dir: str | Path = "screenshots",
        *,
        template_dir: str | Path = "templates",
        template_threshold: float = 0.86,
        ocr_engine: str = "paddleocr",
        ocr_lang: str = "ch",
        ocr_fallback: str = "windows",
        paddle_model_root: str | Path | None = None,
    ):
        self.screenshot_dir = Path(screenshot_dir)
        self.template_dir = Path(template_dir)
        self.template_threshold = template_threshold
        self.ocr_engine = ocr_engine
        self.ocr_lang = ocr_lang
        self.ocr_fallback = ocr_fallback
        self.paddle_model_root = Path(paddle_model_root) if paddle_model_root else None
        (self.screenshot_dir / "checkpoints").mkdir(parents=True, exist_ok=True)
        (self.screenshot_dir / "errors").mkdir(parents=True, exist_ok=True)

    def save_checkpoint(self, name: str, region: Region | None = None) -> Path:
        return self._save_capture("checkpoints", name, region=region)

    def save_fullscreen_checkpoint(self, name: str) -> Path:
        safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name).strip("_") or "capture"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        target = self.screenshot_dir / "checkpoints" / f"{safe_name}_{stamp}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        if self._capture_png_via_powershell(target, None) and target.exists():
            log.info("保存 DPI-aware 全屏截图：%s", target)
            return target
        return self._save_capture("checkpoints", name, region=None)

    def save_error(self, name: str, reason: str, region: Region | None = None) -> Path:
        path = self._save_capture("errors", name, region=region, fallback_text=f"错误截图占位：{reason}\n")
        log.error("保存错误截图：%s reason=%s", path, reason)
        return path

    def capture(self, path: str | Path, region: Region | None = None) -> Path:
        """保存屏幕截图；失败时写 .placeholder.txt 并返回该路径。"""
        requested = Path(path)
        requested.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._capture_png(requested, region)
            log.info("保存截图：%s", requested)
            return requested
        except Exception as exc:  # 环境缺 GUI/依赖时安全降级
            fallback = requested.with_suffix(".placeholder.txt")
            fallback.write_text(f"截图占位：当前环境无法保存 PNG。原因：{exc}\n", encoding="utf-8")
            log.warning("截图不可用，已保存占位文件：%s (%s)", fallback, exc)
            return fallback

    def image_size(self, image_path: str | Path) -> tuple[int, int] | None:
        try:
            from PIL import Image  # type: ignore

            with Image.open(image_path) as image:
                return image.size
        except Exception as exc:
            log.debug("读取图片尺寸失败：%s", exc)
            return None

    def paddleocr_model_kwargs(self) -> dict[str, str]:
        return self._paddleocr_kwargs()

    def is_capture_evidence(self, path: str | Path) -> bool:
        return Path(path).suffix.lower() == ".png"

    def find_template(
        self,
        template_name: str,
        *,
        image_path: str | Path | None = None,
        region: Region | None = None,
        threshold: float | None = None,
    ) -> TemplateMatch | None:
        """在截图中查找模板，返回最佳匹配；缺依赖/模板不存在时返回 None。"""
        threshold = self.template_threshold if threshold is None else threshold
        template_path = self.template_dir / template_name
        if not template_path.exists():
            log.debug("模板不存在：%s", template_path)
            return None

        captured_tmp: Path | None = None
        if image_path is None:
            captured_tmp = self.capture(self.screenshot_dir / "checkpoints" / "template_scan.png", region=region)
            image_path = captured_tmp
        image_path = Path(image_path)
        if image_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".bmp"}:
            return None

        try:
            import cv2  # type: ignore
        except Exception as exc:
            log.debug("OpenCV 不可用，跳过模板识别：%s", exc)
            return None

        haystack = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        needle = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
        if haystack is None or needle is None:
            return None
        if haystack.shape[0] < needle.shape[0] or haystack.shape[1] < needle.shape[1]:
            return None

        result = cv2.matchTemplate(haystack, needle, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if float(max_val) < threshold:
            log.debug("模板匹配未达阈值：%s confidence=%.3f threshold=%.3f", template_name, max_val, threshold)
            return None
        left, top = int(max_loc[0]), int(max_loc[1])
        match = TemplateMatch(template_name, float(max_val), left, top, int(needle.shape[1]), int(needle.shape[0]))
        log.info("模板匹配成功：%s confidence=%.3f center=%s", template_name, match.confidence, match.center)
        return match

    def ocr_text(self, region: Region | None = None, image_path: str | Path | None = None) -> str:
        """OCR 占位/轻量封装。当前不强制引入 OCR 依赖，实机阶段再接入。"""
        log.debug("OCR 尚未接入，region=%s image_path=%s", region, image_path)
        return ""

    def ocr_lines(self, region: Region | None = None, image_path: str | Path | None = None) -> list[OcrLine]:
        """返回 OCR 文本行。

        运行时按配置选择 OCR 后端；PaddleOCR/Tesseract 不可用时可回退 Windows OCR。
        """
        captured_tmp: Path | None = None
        if image_path is None:
            captured_tmp = self.capture(self.screenshot_dir / "checkpoints" / "ocr_scan.png", region=region)
            image_path = captured_tmp
        image = Path(image_path)
        if image.suffix.lower() not in {".png", ".jpg", ".jpeg", ".bmp"}:
            return []

        if self.ocr_engine == "none":
            return []
        if self.ocr_engine == "windows":
            return self._ocr_lines_via_windows_ocr(image)
        if self.ocr_engine == "paddleocr":
            lines = self._ocr_lines_via_paddleocr(image)
            if lines or self.ocr_fallback == "none":
                return lines
            log.info("PaddleOCR 不可用或未识别到文本，尝试 Windows OCR")
            return self._ocr_lines_via_windows_ocr(image)

        try:
            from PIL import Image  # type: ignore
            import pytesseract  # type: ignore
        except Exception as exc:
            log.info("Python OCR 依赖不可用，尝试 Windows OCR：%s", exc)
            return self._ocr_lines_via_windows_ocr(image) if self.ocr_fallback == "windows" else []

        try:
            data = pytesseract.image_to_data(Image.open(image), lang="chi_sim+eng", output_type=pytesseract.Output.DICT)
        except Exception as exc:
            log.warning("Python OCR 识别失败，尝试 Windows OCR：%s", exc)
            return self._ocr_lines_via_windows_ocr(image) if self.ocr_fallback == "windows" else []

        grouped: dict[tuple[int, int, int], list[int]] = {}
        for index, text in enumerate(data.get("text", [])):
            text = str(text).strip()
            if not text:
                continue
            key = (
                int(data.get("block_num", [0])[index]),
                int(data.get("par_num", [0])[index]),
                int(data.get("line_num", [0])[index]),
            )
            grouped.setdefault(key, []).append(index)

        lines: list[OcrLine] = []
        for indexes in grouped.values():
            texts = [str(data["text"][i]).strip() for i in indexes if str(data["text"][i]).strip()]
            if not texts:
                continue
            left = min(int(data["left"][i]) for i in indexes)
            top = min(int(data["top"][i]) for i in indexes)
            right = max(int(data["left"][i]) + int(data["width"][i]) for i in indexes)
            bottom = max(int(data["top"][i]) + int(data["height"][i]) for i in indexes)
            confidences = []
            for i in indexes:
                try:
                    conf = float(data["conf"][i])
                except (TypeError, ValueError):
                    continue
                if conf >= 0:
                    confidences.append(conf)
            confidence = sum(confidences) / len(confidences) if confidences else None
            lines.append(OcrLine(" ".join(texts), left, top, right - left, bottom - top, confidence))
        return sorted(lines, key=lambda line: line.top)

    def _ocr_lines_via_paddleocr(self, image_path: Path) -> list[OcrLine]:
        try:
            from paddleocr import PaddleOCR  # type: ignore
        except Exception as exc:
            log.info("PaddleOCR 依赖不可用：%s", exc)
            return []
        try:
            kwargs = self._paddleocr_kwargs()
            ocr = PaddleOCR(lang=self.ocr_lang, use_textline_orientation=True, **kwargs)
        except TypeError:
            ocr = PaddleOCR(lang=self.ocr_lang, **kwargs)
        except Exception as exc:
            log.warning("PaddleOCR 初始化失败：%s", exc)
            return []
        try:
            if hasattr(ocr, "predict"):
                raw = ocr.predict(str(image_path))
            else:
                raw = ocr.ocr(str(image_path), cls=True)
        except Exception as exc:
            log.warning("PaddleOCR 识别失败：%s", exc)
            return []
        return self._parse_paddleocr_result(raw)

    def _paddleocr_kwargs(self) -> dict[str, str]:
        root = self._resolve_paddle_model_root()
        if root is None:
            return {}
        det_dir = root / "det" / "ch" / "ch_PP-OCRv4_det_infer"
        rec_dir = root / "rec" / "ch" / "ch_PP-OCRv4_rec_infer"
        cls_dir = root / "cls" / "ch_ppocr_mobile_v2.0_cls_infer"
        required = [
            det_dir / "inference.pdmodel",
            det_dir / "inference.pdiparams",
            rec_dir / "inference.pdmodel",
            rec_dir / "inference.pdiparams",
            cls_dir / "inference.pdmodel",
            cls_dir / "inference.pdiparams",
        ]
        missing = [path for path in required if not path.exists()]
        if missing:
            if self.paddle_model_root is not None:
                raise FileNotFoundError(f"PaddleOCR 离线模型不完整：{missing}")
            log.warning("PaddleOCR 离线模型不完整，跳过指定模型目录：%s", missing)
            return {}
        return {
            "det_model_dir": str(det_dir),
            "rec_model_dir": str(rec_dir),
            "cls_model_dir": str(cls_dir),
        }

    def _resolve_paddle_model_root(self) -> Path | None:
        candidates: list[Path] = []
        if self.paddle_model_root is not None:
            configured = self.paddle_model_root
            configured_candidates = [configured]
            if not configured.is_absolute():
                configured_candidates.append(Path.cwd() / configured)
            for candidate in configured_candidates:
                if candidate.exists():
                    return candidate.resolve()
            log.warning("PaddleOCR 配置模型目录不存在，将尝试默认缓存目录：%s", configured)
        candidates.append(Path.cwd() / "models" / "paddleocr")
        candidates.append(Path.home() / ".paddleocr" / "whl")
        if getattr(sys, "frozen", False):
            executable_dir = Path(sys.executable).resolve().parent
            candidates.append(executable_dir / "models" / "paddleocr")
            candidates.append(executable_dir.parent / "models" / "paddleocr")
            candidates.append(Path(getattr(sys, "_MEIPASS", executable_dir)) / "models" / "paddleocr")
        for candidate in candidates:
            if candidate.exists():
                return candidate.resolve()
        return None

    def _parse_paddleocr_result(self, raw: Any) -> list[OcrLine]:
        items: list[Any] = []
        if not raw:
            return []
        if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], list):
            items = raw[0]
        elif isinstance(raw, list):
            items = raw
        lines: list[OcrLine] = []
        for item in items:
            try:
                box = item[0]
                payload = item[1]
                text = str(payload[0]).strip()
                confidence = float(payload[1]) if len(payload) > 1 else None
            except (IndexError, TypeError, ValueError):
                continue
            if not text or not box:
                continue
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            left = int(min(xs))
            top = int(min(ys))
            right = int(max(xs))
            bottom = int(max(ys))
            lines.append(OcrLine(text, left, top, right - left, bottom - top, confidence))
        return sorted(lines, key=lambda line: line.top)

    def _ocr_lines_via_windows_ocr(self, image_path: Path) -> list[OcrLine]:
        powershell = powershell_exe()
        if powershell is None:
            return []
        script = r"""
param([string]$ImagePath)
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]
function Await($WinRtTask, $ResultType) {
  $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.IsGenericMethod })[0]
  $netTask = $asTask.MakeGenericMethod($ResultType).Invoke($null, @($WinRtTask))
  $netTask.Wait(-1) | Out-Null
  $netTask.Result
}
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($ImagePath)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$lang = New-Object Windows.Globalization.Language 'zh-Hans-CN'
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
if (-not $engine) { exit 3 }
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$items = New-Object System.Collections.Generic.List[object]
foreach ($line in $result.Lines) {
  $left = 1000000; $top = 1000000; $right = 0; $bottom = 0
  foreach ($word in $line.Words) {
    $r = $word.BoundingRect
    if ($r.X -lt $left) { $left = $r.X }
    if ($r.Y -lt $top) { $top = $r.Y }
    if (($r.X + $r.Width) -gt $right) { $right = $r.X + $r.Width }
    if (($r.Y + $r.Height) -gt $bottom) { $bottom = $r.Y + $r.Height }
  }
  if ($left -eq 1000000) { $left = 0; $top = 0 }
  $items.Add([PSCustomObject]@{
    Text=$line.Text
    Left=[int]$left
    Top=[int]$top
    Width=[int]($right-$left)
    Height=[int]($bottom-$top)
  }) | Out-Null
}
$items | ConvertTo-Json -Compress
""".strip()
        try:
            result = run_powershell(script, ["-ImagePath", windows_path(image_path)], timeout=30)
            if result is None:
                return []
            if result.returncode != 0 or not result.stdout.strip():
                log.warning("Windows OCR 失败：%s", decode_process_output(result.stderr).strip())
                return []
            raw = json.loads(decode_process_output(result.stdout))
            if isinstance(raw, dict):
                raw = [raw]
            return [
                OcrLine(
                    text=str(item.get("Text", "")),
                    left=int(item.get("Left", 0)),
                    top=int(item.get("Top", 0)),
                    width=int(item.get("Width", 0)),
                    height=int(item.get("Height", 0)),
                )
                for item in raw
                if str(item.get("Text", "")).strip()
            ]
        except Exception as exc:
            log.warning("Windows OCR 异常：%s", exc)
            return []

    def find_selected_checkbox_ratios(self, image_path: str | Path) -> list[tuple[float, float]]:
        """识别当前窗口截图里已选源消息的蓝色复选框中心点比例。

        优先用 Pillow 在当前进程内扫描，避免高 DPI 截图下反复启动 PowerShell。
        Pillow 不可用时再通过 Windows System.Drawing fallback。
        """
        image = Path(image_path)
        if not image.exists() or image.suffix.lower() != ".png":
            return []
        points = self._find_selected_checkbox_ratios_via_pillow(image)
        if points:
            return points
        return self._find_selected_checkbox_ratios_via_powershell(image)

    def find_checkbox_outline_ratios(self, image_path: str | Path) -> list[tuple[float, float]]:
        """识别未勾选复选框的灰色方框轮廓中心点比例。"""
        image = Path(image_path)
        if not image.exists() or image.suffix.lower() != ".png":
            return []
        try:
            from PIL import Image  # type: ignore
        except Exception as exc:
            log.debug("Pillow 不可用，跳过灰色复选框轮廓扫描：%s", exc)
            return []
        try:
            with Image.open(image) as raw:
                bitmap = raw.convert("RGB")
                width, height = bitmap.size
                pixels = bitmap.load()
                visited: set[tuple[int, int]] = set()
                scale = max(width / 1440.0, height / 900.0, 1.0)
                min_size = max(10, int(10 * scale))
                max_size = max(42, int(36 * scale))
                max_delta = max(10, int(8 * scale))
                points: list[tuple[float, float]] = []
                for y in range(0, height):
                    for x in range(0, width):
                        if (x, y) in visited:
                            continue
                        r, g, b = pixels[x, y]
                        if not self._is_checkbox_outline_gray(r, g, b):
                            visited.add((x, y))
                            continue
                        queue: deque[tuple[int, int]] = deque([(x, y)])
                        visited.add((x, y))
                        min_x = max_x = x
                        min_y = max_y = y
                        count = 0
                        while queue:
                            px, py = queue.popleft()
                            count += 1
                            min_x = min(min_x, px)
                            max_x = max(max_x, px)
                            min_y = min(min_y, py)
                            max_y = max(max_y, py)
                            for nx, ny in ((px + 1, py), (px - 1, py), (px, py + 1), (px, py - 1)):
                                if nx < 0 or ny < 0 or nx >= width or ny >= height or (nx, ny) in visited:
                                    continue
                                nr, ng, nb = pixels[nx, ny]
                                if self._is_checkbox_outline_gray(nr, ng, nb):
                                    visited.add((nx, ny))
                                    queue.append((nx, ny))
                        box_width = max_x - min_x + 1
                        box_height = max_y - min_y + 1
                        if (
                            count >= 20
                            and min_size <= box_width <= max_size
                            and min_size <= box_height <= max_size
                            and abs(box_width - box_height) <= max_delta
                        ):
                            points.append((((min_x + max_x) / 2.0) / width, ((min_y + max_y) / 2.0) / height))
                points = self._dedupe_ratio_points(points)
                log.info(
                    "灰色复选框轮廓扫描：backend=pillow image=%sx%s points=%s",
                    width,
                    height,
                    [(round(x, 3), round(y, 3)) for x, y in points],
                )
                return sorted(points, key=lambda point: point[1])
        except Exception as exc:
            log.debug("Pillow 灰色复选框轮廓扫描失败：%s", exc)
            return []

    def _checkbox_size_limits(self, width: int, height: int) -> tuple[int, int, int]:
        scale = max(width / 1440.0, height / 900.0, 1.0)
        min_size = max(8, int(7 * scale))
        max_size = max(42, int(32 * scale))
        max_delta = max(8, int(6 * scale))
        return min_size, max_size, max_delta

    def _is_selected_checkbox_blue(self, r: int, g: int, b: int) -> bool:
        return b > 170 and 90 < g < 190 and r < 100

    def _is_checkbox_outline_gray(self, r: int, g: int, b: int) -> bool:
        return 90 <= r <= 205 and 90 <= g <= 205 and 90 <= b <= 205 and max(r, g, b) - min(r, g, b) <= 32

    def _dedupe_ratio_points(self, points: list[tuple[float, float]]) -> list[tuple[float, float]]:
        deduped: list[tuple[float, float]] = []
        for point in sorted(points, key=lambda item: (item[1], item[0])):
            if any(abs(point[0] - existing[0]) <= 0.006 and abs(point[1] - existing[1]) <= 0.006 for existing in deduped):
                continue
            deduped.append(point)
        return deduped

    def _find_selected_checkbox_ratios_via_pillow(self, image_path: Path) -> list[tuple[float, float]]:
        try:
            from PIL import Image  # type: ignore
        except Exception as exc:
            log.debug("Pillow 不可用，跳过 Python 蓝勾扫描：%s", exc)
            return []
        try:
            with Image.open(image_path) as raw:
                image = raw.convert("RGB")
                width, height = image.size
                pixels = image.load()
                visited: set[tuple[int, int]] = set()
                min_size, max_size, max_delta = self._checkbox_size_limits(width, height)
                points: list[tuple[float, float]] = []
                start_y = int(height * 0.05)
                start_x = int(width * 0.02)
                end_x = int(width * 0.95)
                for y in range(start_y, height):
                    for x in range(start_x, end_x):
                        if (x, y) in visited:
                            continue
                        r, g, b = pixels[x, y]
                        if not self._is_selected_checkbox_blue(r, g, b):
                            visited.add((x, y))
                            continue
                        queue: deque[tuple[int, int]] = deque([(x, y)])
                        visited.add((x, y))
                        min_x = max_x = x
                        min_y = max_y = y
                        count = 0
                        while queue:
                            px, py = queue.popleft()
                            count += 1
                            min_x = min(min_x, px)
                            max_x = max(max_x, px)
                            min_y = min(min_y, py)
                            max_y = max(max_y, py)
                            for nx, ny in ((px + 1, py), (px - 1, py), (px, py + 1), (px, py - 1)):
                                if nx < 0 or ny < 0 or nx >= width or ny >= height or (nx, ny) in visited:
                                    continue
                                nr, ng, nb = pixels[nx, ny]
                                visited.add((nx, ny))
                                if self._is_selected_checkbox_blue(nr, ng, nb):
                                    queue.append((nx, ny))
                        box_width = max_x - min_x + 1
                        box_height = max_y - min_y + 1
                        if (
                            count >= 40
                            and min_size <= box_width <= max_size
                            and min_size <= box_height <= max_size
                            and abs(box_width - box_height) <= max_delta
                        ):
                            points.append((((min_x + max_x) / 2.0) / width, ((min_y + max_y) / 2.0) / height))
                log.info(
                    "蓝色复选框扫描：backend=pillow image=%sx%s points=%s",
                    width,
                    height,
                    [(round(x, 3), round(y, 3)) for x, y in points],
                )
                return sorted(points, key=lambda point: point[1])
        except Exception as exc:
            log.debug("Pillow 蓝勾扫描失败：%s", exc)
            return []

    def _find_selected_checkbox_ratios_via_powershell(self, image: Path) -> list[tuple[float, float]]:
        powershell = powershell_exe()
        if powershell is None:
            return []
        script = """
param([string]$ImagePath)
Add-Type -AssemblyName System.Drawing
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
$img = [System.Drawing.Bitmap]::FromFile($ImagePath)
$w = $img.Width
$h = $img.Height
$scale = [Math]::Max([Math]::Max($w / 1440.0, $h / 900.0), 1.0)
$minSize = [Math]::Max(8, [int](7 * $scale))
$maxSize = [Math]::Max(28, [int](24 * $scale))
$maxDelta = [Math]::Max(8, [int](6 * $scale))
$visited = New-Object 'bool[,]' $w,$h
$boxes = New-Object System.Collections.Generic.List[object]
for ($y = [int]($h * 0.05); $y -lt $h; $y++) {
  for ($x = [int]($w * 0.12); $x -lt [int]($w * 0.95); $x++) {
    if ($visited[$x,$y]) { continue }
    $c = $img.GetPixel($x, $y)
    $isBlue = ($c.B -gt 170 -and $c.G -gt 90 -and $c.G -lt 180 -and $c.R -lt 90)
    if (-not $isBlue) { $visited[$x,$y] = $true; continue }
    $queue = New-Object System.Collections.Queue
    $queue.Enqueue(@($x,$y))
    $visited[$x,$y] = $true
    $minX=$x; $maxX=$x; $minY=$y; $maxY=$y; $count=0
    while ($queue.Count -gt 0) {
      $p = $queue.Dequeue()
      $px = [int]$p[0]; $py = [int]$p[1]
      $count++
      if ($px -lt $minX) { $minX = $px }; if ($px -gt $maxX) { $maxX = $px }
      if ($py -lt $minY) { $minY = $py }; if ($py -gt $maxY) { $maxY = $py }
      foreach ($d in @(@(1,0),@(-1,0),@(0,1),@(0,-1))) {
        $nx = $px + [int]$d[0]; $ny = $py + [int]$d[1]
        if ($nx -lt 0 -or $ny -lt 0 -or $nx -ge $w -or $ny -ge $h -or $visited[$nx,$ny]) { continue }
        $nc = $img.GetPixel($nx, $ny)
        $nb = ($nc.B -gt 170 -and $nc.G -gt 90 -and $nc.G -lt 180 -and $nc.R -lt 90)
        $visited[$nx,$ny] = $true
        if ($nb) { $queue.Enqueue(@($nx,$ny)) }
      }
    }
    $bw = $maxX - $minX + 1
    $bh = $maxY - $minY + 1
    if ($count -ge 40 -and $bw -ge $minSize -and $bw -le $maxSize -and $bh -ge $minSize -and $bh -le $maxSize -and [Math]::Abs($bw - $bh) -le $maxDelta) {
      $boxes.Add([PSCustomObject]@{ XR=(($minX+$maxX)/2.0)/$w; YR=(($minY+$maxY)/2.0)/$h; W=$bw; H=$bh; Count=$count }) | Out-Null
    }
  }
}
$img.Dispose()
if ($boxes.Count -eq 0) { "[]" } else { $boxes | Sort-Object YR | ConvertTo-Json -Compress }
""".strip()
        try:
            result = run_powershell(script, ["-ImagePath", windows_path(image)], timeout=30)
            if result is None:
                return []
            if result.returncode != 0 or not result.stdout.strip():
                log.warning("源消息复选框识别失败：%s", decode_process_output(result.stderr).strip())
                return []
            raw = json.loads(decode_process_output(result.stdout))
            if isinstance(raw, dict):
                raw = [raw]
            points = [(float(item["XR"]), float(item["YR"])) for item in raw]
            log.info(
                "蓝色复选框扫描：backend=powershell points=%s",
                [(round(x, 3), round(y, 3)) for x, y in points],
            )
            return points
        except Exception as exc:
            log.warning("源消息复选框识别异常：%s", exc)
            return []

    def _save_capture(self, subdir: str, name: str, *, region: Region | None = None, fallback_text: str | None = None) -> Path:
        safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name).strip("_") or "capture"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        target = self.screenshot_dir / subdir / f"{safe_name}_{stamp}.png"
        path = self.capture(target, region=region)
        if fallback_text and path.suffix == ".txt":
            path.write_text(fallback_text, encoding="utf-8")
        return path

    def _capture_png(self, path: Path, region: Region | None) -> None:
        try:
            import mss  # type: ignore
            from PIL import Image  # type: ignore
        except Exception as mss_exc:
            try:
                if self._capture_png_via_pyautogui(path, region):
                    log.info("截图后端：pyautogui")
                    return
            except Exception as pyautogui_exc:
                if self._capture_png_via_powershell(path, region):
                    log.info("截图后端：powershell")
                    return
                raise RuntimeError(f"截图依赖不可用：mss={mss_exc}; pyautogui={pyautogui_exc}")
            if self._capture_png_via_powershell(path, region):
                log.info("截图后端：powershell")
                return
            raise RuntimeError(f"截图依赖不可用：mss={mss_exc}; pyautogui=unknown")

        with mss.mss() as sct:
            monitor: dict[str, int]
            if region is None:
                monitor = dict(sct.monitors[1])
            else:
                monitor = {"left": region.left, "top": region.top, "width": region.width, "height": region.height}
            raw = sct.grab(monitor)
            image = Image.frombytes("RGB", raw.size, raw.rgb)
            self._save_checked_capture(image, path, region, backend="mss")

    def _save_checked_capture(self, image: Any, path: Path, region: Region | None, *, backend: str) -> None:
        image.save(path)
        if not self._is_nearly_black(path):
            log.info("截图后端：%s", backend)
            return
        log.warning("截图后端 %s 生成近似全黑图片，尝试备用截图后端：%s", backend, path)
        if backend != "pyautogui" and self._capture_png_via_pyautogui(path, region) and not self._is_nearly_black(path):
            log.info("截图后端：pyautogui")
            return
        if self._capture_png_via_powershell(path, region) and not self._is_nearly_black(path):
            log.info("截图后端：powershell")
            return
        log.warning("备用截图后端仍生成近似全黑图片：%s", path)

    def _capture_png_via_pyautogui(self, path: Path, region: Region | None) -> bool:
        try:
            import pyautogui  # type: ignore

            if region is None:
                image = pyautogui.screenshot()
            else:
                image = pyautogui.screenshot(region=region.as_tuple())
            image.save(path)
            return path.exists()
        except Exception as exc:
            log.debug("pyautogui 截图失败：%s", exc)
            return False

    def _is_nearly_black(self, path: Path) -> bool:
        try:
            from PIL import Image, ImageStat  # type: ignore

            with Image.open(path) as image:
                gray = image.convert("L")
                stat = ImageStat.Stat(gray)
                mean = float(stat.mean[0])
                extrema = gray.getextrema()
                max_value = int(extrema[1]) if extrema else 0
                return mean <= 3.0 and max_value <= 12
        except Exception as exc:
            log.debug("黑屏检测失败：%s", exc)
            return False

    def _capture_png_via_powershell(self, path: Path, region: Region | None) -> bool:
        powershell = powershell_exe()
        if powershell is None:
            return False
        out_path = windows_path(path)
        left = 0 if region is None else region.left
        top = 0 if region is None else region.top
        width = 0 if region is None else region.width
        height = 0 if region is None else region.height
        script = """
param([string]$OutPath, [int]$Left, [int]$Top, [int]$Width, [int]$Height)
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class DpiAwareCapture {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("shcore.dll")] public static extern int SetProcessDpiAwareness(int value);
}
"@
try { [void][DpiAwareCapture]::SetProcessDpiAwareness(2) } catch { try { [void][DpiAwareCapture]::SetProcessDPIAware() } catch {} }
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
if ($Width -le 0 -or $Height -le 0) {
  $bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
  $Left = $bounds.Left; $Top = $bounds.Top; $Width = $bounds.Width; $Height = $bounds.Height
}
$bmp = New-Object System.Drawing.Bitmap $Width, $Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($Left, $Top, 0, 0, (New-Object System.Drawing.Size $Width, $Height))
$bmp.Save($OutPath, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
""".strip()
        try:
            args = [
                "-OutPath",
                out_path,
                "-Left",
                str(left),
                "-Top",
                str(top),
                "-Width",
                str(width),
                "-Height",
                str(height),
            ]
            result = run_powershell(script, args, timeout=20)
            if result is None:
                return False
            if result.returncode != 0:
                log.debug("PowerShell 截图失败：%s %s", decode_process_output(result.stdout), decode_process_output(result.stderr))
                return False
            return path.exists()
        except Exception as exc:
            log.debug("PowerShell 截图异常：%s", exc)
            return False
