from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class WindowConfig:
    title_keyword: str = "企业微信"
    use_relative_coordinates: bool = True
    anchors: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OcrConfig:
    engine: str = "paddleocr"
    lang: str = "ch"
    fallback: str = "windows"


@dataclass(frozen=True)
class VisionConfig:
    template_threshold: float = 0.86


@dataclass(frozen=True)
class SentinelConfig:
    enabled: bool = False
    # 3 个员工私聊作为结束边界。后续可扩展头像模板；当前先支持名称/识别结果匹配。
    names: list[str] = field(default_factory=list)
    # 识别/截断失败时停止，避免越过边界继续真实发送。
    stop_on_detection_failure: bool = True
    # 右侧已选列表每行删除按钮的窗口相对 x 坐标。
    remove_button_x_ratio: float = 0.940
    # 右侧已选列表的窗口相对区域，用于 OCR/截图解析。
    selected_list_region_ratio: list[float] = field(default_factory=lambda: [0.670, 0.220, 0.285, 0.760])
    # 右侧已选项每行高度约占窗口高度的比例，用于从 OCR 行坐标映射到删除按钮。
    selected_item_row_height_ratio: float = 0.075


@dataclass(frozen=True)
class RecipientSelectionConfig:
    # search_by_name：旧方案，逐个搜索群名。
    # bottom_of_picker：新方案，在“选择聊天/发送给”弹窗内滚到底部，连续勾选底部 N 个会话。
    mode: str = "bottom_of_picker"
    # 用户确认：底部出现员工、机器人也允许选择，不做群类型过滤。
    allow_staff_and_bots: bool = True
    # 每次打开选择聊天弹窗后，滚到底部的尝试次数。
    scroll_to_bottom_repeats: int = 5
    # 当前实机策略：从底部往上选择候选会话。
    selection_direction: str = "bottom_to_top"
    # 结束哨兵：遇到员工私聊边界时截断尾批。
    sentinel: SentinelConfig = field(default_factory=SentinelConfig)


@dataclass(frozen=True)
class SourceSelectionConfig:
    # 消息多选态下，源消息左侧复选框的窗口相对 x 坐标。
    checkbox_x_ratio: float = 0.322
    # 用户第一次选好消息后，后续批次按这些窗口相对 y 坐标重选源消息。
    checkbox_y_ratios: list[float] = field(default_factory=lambda: [0.547, 0.790])
    # 底部工具栏“逐条转发”按钮的窗口相对坐标。
    forward_button_ratio: list[float] = field(default_factory=lambda: [0.478, 0.901])


@dataclass(frozen=True)
class AppConfig:
    max_total_send: int
    batch_size: int = 9
    batch_interval_sec: float = 5
    max_retry_per_group: int = 2
    stop_hotkey: str = "ctrl+alt+q"
    dry_run: bool = True
    require_confirm_before_start: bool = True
    require_confirm_first_batch: bool = True
    window: WindowConfig = field(default_factory=WindowConfig)
    ocr: OcrConfig = field(default_factory=OcrConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    recipient_selection: RecipientSelectionConfig = field(default_factory=RecipientSelectionConfig)
    source_selection: SourceSelectionConfig = field(default_factory=SourceSelectionConfig)

    def validate(self, *, allow_real_send: bool = False) -> None:
        if self.max_total_send <= 0:
            raise ValueError("max_total_send 必须 > 0")
        if self.batch_size <= 0:
            raise ValueError("batch_size 必须 > 0")
        if self.batch_size > 9:
            raise ValueError("batch_size 不能超过 9（企业微信单次最多 9 个会话）")
        if self.batch_interval_sec < 0:
            raise ValueError("batch_interval_sec 不能为负数")
        if self.max_retry_per_group < 0:
            raise ValueError("max_retry_per_group 不能为负数")
        if self.recipient_selection.mode not in {"search_by_name", "bottom_of_picker"}:
            raise ValueError("recipient_selection.mode 必须是 search_by_name 或 bottom_of_picker")
        if self.ocr.engine not in {"paddleocr", "tesseract", "windows", "none"}:
            raise ValueError("ocr.engine 必须是 paddleocr、tesseract、windows 或 none")
        if self.ocr.fallback not in {"windows", "none"}:
            raise ValueError("ocr.fallback 必须是 windows 或 none")
        if self.recipient_selection.scroll_to_bottom_repeats <= 0:
            raise ValueError("recipient_selection.scroll_to_bottom_repeats 必须 > 0")
        if self.recipient_selection.selection_direction != "bottom_to_top":
            raise ValueError("recipient_selection.selection_direction 当前只支持 bottom_to_top")
        sentinel = self.recipient_selection.sentinel
        if sentinel.enabled and not sentinel.names:
            raise ValueError("recipient_selection.sentinel.names 启用哨兵时不能为空")
        if any(not name.strip() for name in sentinel.names):
            raise ValueError("recipient_selection.sentinel.names 不能包含空名称")
        if not 0 <= sentinel.remove_button_x_ratio <= 1:
            raise ValueError("recipient_selection.sentinel.remove_button_x_ratio 必须在 0..1 之间")
        if len(sentinel.selected_list_region_ratio) != 4:
            raise ValueError("recipient_selection.sentinel.selected_list_region_ratio 必须包含 4 个数")
        if any(v < 0 or v > 1 for v in sentinel.selected_list_region_ratio):
            raise ValueError("recipient_selection.sentinel.selected_list_region_ratio 必须都在 0..1 之间")
        if sentinel.selected_list_region_ratio[2] <= 0 or sentinel.selected_list_region_ratio[3] <= 0:
            raise ValueError("recipient_selection.sentinel.selected_list_region_ratio 宽高必须 > 0")
        if sentinel.selected_item_row_height_ratio <= 0:
            raise ValueError("recipient_selection.sentinel.selected_item_row_height_ratio 必须 > 0")
        if not 0 <= self.source_selection.checkbox_x_ratio <= 1:
            raise ValueError("source_selection.checkbox_x_ratio 必须在 0..1 之间")
        if not self.source_selection.checkbox_y_ratios:
            raise ValueError("source_selection.checkbox_y_ratios 不能为空")
        if any(y < 0 or y > 1 for y in self.source_selection.checkbox_y_ratios):
            raise ValueError("source_selection.checkbox_y_ratios 必须都在 0..1 之间")
        if len(self.source_selection.forward_button_ratio) != 2:
            raise ValueError("source_selection.forward_button_ratio 必须包含两个数")
        if any(v < 0 or v > 1 for v in self.source_selection.forward_button_ratio):
            raise ValueError("source_selection.forward_button_ratio 必须都在 0..1 之间")
        if not self.dry_run and not allow_real_send:
            raise ValueError("第一版禁止 dry_run=false：真实点击发送尚未实现")


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{key} 必须是对象")
    return value


def load_config(path: str | Path, *, force_dry_run: bool | None = None, allow_real_send: bool = False) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("配置文件顶层必须是对象")
    if "max_total_send" not in raw:
        raise ValueError("配置缺少 max_total_send")

    if force_dry_run is not None:
        raw["dry_run"] = force_dry_run

    cfg = AppConfig(
        max_total_send=int(raw["max_total_send"]),
        batch_size=int(raw.get("batch_size", 9)),
        batch_interval_sec=float(raw.get("batch_interval_sec", 5)),
        max_retry_per_group=int(raw.get("max_retry_per_group", 2)),
        stop_hotkey=str(raw.get("stop_hotkey", "ctrl+alt+q")),
        dry_run=bool(raw.get("dry_run", True)),
        require_confirm_before_start=bool(raw.get("require_confirm_before_start", True)),
        require_confirm_first_batch=bool(raw.get("require_confirm_first_batch", True)),
        window=WindowConfig(**_section(raw, "window")),
        ocr=OcrConfig(**_section(raw, "ocr")),
        vision=VisionConfig(**_section(raw, "vision")),
        recipient_selection=_load_recipient_selection(_section(raw, "recipient_selection")),
        source_selection=SourceSelectionConfig(**_section(raw, "source_selection")),
    )
    cfg.validate(allow_real_send=allow_real_send)
    return cfg


def _load_recipient_selection(data: dict[str, Any]) -> RecipientSelectionConfig:
    raw = dict(data)
    sentinel_raw = raw.pop("sentinel", {})
    if sentinel_raw is None:
        sentinel_raw = {}
    if not isinstance(sentinel_raw, dict):
        raise ValueError("recipient_selection.sentinel 必须是对象")
    return RecipientSelectionConfig(sentinel=SentinelConfig(**sentinel_raw), **raw)
