from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .powershell import run_powershell, windows_path
from .screen import Region, ScreenInspector
from .wecom_window import WeComWindow, WindowRect

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SuggestedRegion:
    name: str
    left: int
    top: int
    width: int
    height: int
    note: str

    @property
    def region(self) -> Region:
        return Region(self.left, self.top, self.width, self.height)


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def suggest_regions(rect: WindowRect) -> list[SuggestedRegion]:
    """基于窗口矩形给出粗略校准区域。只截图/裁剪，不点击。"""
    def r(name: str, x: float, y: float, w: float, h: float, note: str) -> SuggestedRegion:
        return SuggestedRegion(
            name=name,
            left=rect.left + round(rect.width * x),
            top=rect.top + round(rect.height * y),
            width=max(1, round(rect.width * w)),
            height=max(1, round(rect.height * h)),
            note=note,
        )

    # Windows GetWindowRect 在部分 DPI/阴影环境下会包含不可见边框；下面的比例
    # 按“截图中的可见企业微信内容”保守取框，避免裁到桌面背景。
    return [
        r("window_full", 0.0, 0.0, 1.0, 1.0, "企业微信窗口整图，用于核对定位是否准确"),
        r("nav_bar", 0.16, 0.13, 0.08, 0.86, "最左侧功能导航栏"),
        r("search_box_area", 0.25, 0.17, 0.29, 0.07, "左上搜索框/加号区域"),
        r("conversation_list", 0.24, 0.25, 0.36, 0.74, "会话列表区域"),
        r("chat_header", 0.60, 0.13, 0.40, 0.12, "当前聊天标题栏"),
        r("chat_content", 0.60, 0.25, 0.40, 0.55, "聊天内容区域"),
        r("input_area", 0.60, 0.80, 0.40, 0.20, "底部输入框/工具栏区域，宽松裁剪以便人工核对"),
    ]


def crop_image(source: Path, target: Path, region: Region) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image  # type: ignore

        with Image.open(source) as img:
            box = (region.left, region.top, region.left + region.width, region.top + region.height)
            img.crop(box).save(target)
        return target
    except Exception as pil_exc:
        if _crop_via_powershell(source, target, region):
            return target
        raise RuntimeError(f"裁剪失败：Pillow={pil_exc}")


def _crop_via_powershell(source: Path, target: Path, region: Region) -> bool:
    script = """
param([string]$Source, [string]$Target, [int]$Left, [int]$Top, [int]$Width, [int]$Height)
Add-Type -AssemblyName System.Drawing
$img = [System.Drawing.Image]::FromFile($Source)
$bmp = New-Object System.Drawing.Bitmap $Width, $Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$srcRect = New-Object System.Drawing.Rectangle $Left, $Top, $Width, $Height
$dstRect = New-Object System.Drawing.Rectangle 0, 0, $Width, $Height
$g.DrawImage($img, $dstRect, $srcRect, [System.Drawing.GraphicsUnit]::Pixel)
$bmp.Save($Target, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose(); $img.Dispose()
""".strip()
    result = run_powershell(
        script,
        [
            "-Source",
            windows_path(source),
            "-Target",
            windows_path(target),
            "-Left",
            str(region.left),
            "-Top",
            str(region.top),
            "-Width",
            str(region.width),
            "-Height",
            str(region.height),
        ],
        timeout=20,
    )
    if result is None:
        return False
    if result.returncode != 0:
        log.debug("PowerShell 裁剪失败：%s %s", result.stdout, result.stderr)
        return False
    return target.exists()


def _rect_from_args(args: argparse.Namespace) -> Region:
    return Region(args.left, args.top, args.width, args.height)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="企业微信 RPA 实机校准工具（只截图/裁剪，不点击）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    probe = sub.add_parser("probe", help="定位企业微信窗口并保存校准截图")
    probe.add_argument("--title-keyword", default="企业微信")
    probe.add_argument("--screenshot-dir", default="screenshots")
    probe.add_argument("--json", action="store_true", help="输出 JSON，方便脚本读取")
    probe.add_argument("--crop-suggestions", action="store_true", help="同时裁剪建议区域到 screenshots/calibration")

    crop = sub.add_parser("crop", help="从截图中裁剪一个区域到模板/校准文件")
    crop.add_argument("--source", required=True, help="源截图路径")
    crop.add_argument("--out", required=True, help="输出 PNG 路径，例如 templates/send_button.png")
    crop.add_argument("--left", type=int, required=True)
    crop.add_argument("--top", type=int, required=True)
    crop.add_argument("--width", type=int, required=True)
    crop.add_argument("--height", type=int, required=True)

    suggest = sub.add_parser("suggest", help="根据窗口矩形打印建议裁剪区域")
    suggest.add_argument("--left", type=int, required=True)
    suggest.add_argument("--top", type=int, required=True)
    suggest.add_argument("--width", type=int, required=True)
    suggest.add_argument("--height", type=int, required=True)
    suggest.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "probe":
            window = WeComWindow(args.title_keyword)
            rect = window.locate()
            if rect is None:
                print("ERROR: 未找到企业微信窗口", file=sys.stderr)
                return 2
            inspector = ScreenInspector(args.screenshot_dir)
            shot = inspector.save_checkpoint("wecom_window_probe", region=Region(rect.left, rect.top, rect.width, rect.height))
            regions = suggest_regions(rect)
            crops: list[dict[str, object]] = []
            if args.crop_suggestions:
                full = Path(shot)
                # 截图已经是窗口局部图，因此裁剪时要转成截图内相对坐标。
                for item in regions:
                    rel = Region(item.left - rect.left, item.top - rect.top, item.width, item.height)
                    out = Path(args.screenshot_dir) / "calibration" / f"{item.name}.png"
                    crop_image(full, out, rel)
                    crops.append({"name": item.name, "path": str(out), "note": item.note})
            payload = {"rect": asdict(rect), "screenshot": str(shot), "suggested_regions": [asdict(x) for x in regions], "crops": crops}
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"窗口：{rect}")
                print(f"截图：{shot}")
                for item in regions:
                    print(f"- {item.name}: left={item.left} top={item.top} width={item.width} height={item.height} # {item.note}")
            return 0

        if args.cmd == "crop":
            out = crop_image(Path(args.source), Path(args.out), _rect_from_args(args))
            print(f"saved {out}")
            return 0

        if args.cmd == "suggest":
            rect = WindowRect(args.left, args.top, args.width, args.height)
            regions = suggest_regions(rect)
            if args.json:
                print(json.dumps([asdict(x) for x in regions], ensure_ascii=False, indent=2))
            else:
                for item in regions:
                    print(f"{item.name}: left={item.left} top={item.top} width={item.width} height={item.height} # {item.note}")
            return 0

        raise RuntimeError(f"未知命令：{args.cmd}")
    except Exception as exc:
        log.exception("校准工具失败：%s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
