from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable

from .config import AppConfig
from .safety import StopController, assert_batch_selection_count
from .screen import (
    SOURCE_CHECKBOX_COLUMN_MAX_X,
    SOURCE_CHECKBOX_COLUMN_MAX_Y,
    SOURCE_CHECKBOX_COLUMN_MIN_X,
    SOURCE_CHECKBOX_COLUMN_MIN_Y,
    Region,
    ScreenInspector,
    fullscreen_point_to_window_ratio,
    select_aligned_checkbox_column,
)
from .wecom_window import WeComWindow, WindowRect

log = logging.getLogger(__name__)


SOURCE_CONTEXT_RIGHT_CLICK_X_RATIOS = (0.90, 0.86, 0.94, 0.75)


@dataclass(frozen=True)
class FlowResult:
    status: str
    summary: dict[str, int]


@dataclass(frozen=True)
class SelectedRecipient:
    name: str
    y_ratio: float | None = None


@dataclass(frozen=True)
class SentinelTrimPlan:
    send: list[SelectedRecipient]
    remove: list[SelectedRecipient]
    boundary_reached: bool


@dataclass
class FastPathState:
    window_rect: WindowRect | None = None
    recipient_checkbox_points_bottom_to_top: list[tuple[float, float]] | None = None
    recipient_scroll_drag: tuple[tuple[int, int], tuple[int, int]] | None = None
    recipient_scroll_track: tuple[int, int] | None = None
    final_send_button_ratio: tuple[float, float] | None = None
    ready: bool = False
    available_from_batch: int = 2


class ForwardFlow:
    def __init__(
        self,
        config: AppConfig,
        *,
        screenshot_dir: str = "screenshots",
        yes: bool = False,
        real_send_allowed: bool = False,
        stop_controller: StopController | None = None,
        install_stop_hotkey: bool = True,
        confirm_callback: Callable[[str], bool] | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.config = config
        self.yes = yes
        self.real_send_allowed = real_send_allowed
        self.stop = stop_controller or StopController(config.stop_hotkey)
        self.install_stop_hotkey = install_stop_hotkey
        self.confirm_callback = confirm_callback
        self.progress_callback = progress_callback
        self.screen = ScreenInspector(
            screenshot_dir,
            template_threshold=config.vision.template_threshold,
            ocr_engine=config.ocr.engine,
            ocr_lang=config.ocr.lang,
            ocr_fallback=config.ocr.fallback,
            paddle_model_root=config.ocr.model_root,
        )
        self.window = WeComWindow(config.window.title_keyword, anchors=config.window.anchors)
        self.source_checkbox_x_ratio = config.source_selection.checkbox_x_ratio
        self.source_checkbox_y_ratios = list(config.source_selection.checkbox_y_ratios)
        self.boundary_reached = False
        self._progress_total_batches: int | None = None
        self.sent_count = 0
        self._source_context_right_click: tuple[float, float, float] | None = None
        self._source_multiselect_menu_offset: tuple[int, int] | None = None
        self._last_recipient_checkbox_x_ratio: float | None = None
        self._fast_path = FastPathState()

    def _emit_progress(self, event: str, **payload: Any) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback({"event": event, **payload})
        except Exception as exc:
            log.debug("进度回调失败：event=%s reason=%s", event, exc)

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            if self.stop.should_stop():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.1, remaining))

    @contextmanager
    def _step(self, name: str, **details: Any):
        detail_text = " ".join(f"{key}={value}" for key, value in details.items())
        started = time.monotonic()
        log.info("开始%s%s", name, f"：{detail_text}" if detail_text else "")
        try:
            yield
        except Exception:
            elapsed = time.monotonic() - started
            log.exception("%s失败：elapsed=%.2fs%s", name, elapsed, f" {detail_text}" if detail_text else "")
            raise
        else:
            elapsed = time.monotonic() - started
            log.info("%s完成：elapsed=%.2fs%s", name, elapsed, f" {detail_text}" if detail_text else "")

    def _confirm(self, prompt: str) -> None:
        if self.yes:
            log.info("跳过人工确认：%s", prompt)
            return
        if self.confirm_callback is not None:
            if not self.confirm_callback(prompt):
                raise RuntimeError("用户未确认，流程停止")
            return
        answer = input(f"{prompt} 输入 YES 继续：").strip()
        if answer != "YES":
            raise RuntimeError("用户未确认，流程停止")

    def run(self, send_count: int) -> FlowResult:
        if send_count <= 0:
            raise ValueError("发送数量必须 > 0")
        if self.install_stop_hotkey:
            self.stop.install()
        status = "completed"
        try:
            self._emit_progress("run_started", total_targets=send_count, dry_run=self.config.dry_run)
            rect = self.window.locate()
            selection_shot = self.screen.save_checkpoint("message_selection_start", region=None)
            if not self.config.dry_run and rect:
                selected_points = self._source_selected_checkbox_ratios(selection_shot)
                if len(selected_points) != len(self.source_checkbox_y_ratios):
                    selected_points = self._source_selected_checkbox_ratios_from_fullscreen(rect, "source_startup_fullscreen_probe")
                if selected_points:
                    self._record_exact_source_selection(selection_shot, rect=rect)
                elif self.real_send_allowed:
                    log.info("启动时未看到源消息蓝勾，尝试按已记录位置重新进入多选并勾选源消息")
                    self._reselect_source_messages(rect)
                else:
                    self._record_exact_source_selection(selection_shot, rect=rect)
            if self.config.require_confirm_before_start:
                self._confirm("请确认企业微信已滚动到底部，并且待转发的最后 2-5 条消息可见/已正确选中。")

            if self.config.dry_run:
                log.warning("当前为 dry-run：不会点击最终发送按钮，也不会真实发送。")
            elif not self.real_send_allowed:
                raise RuntimeError("真实发送缺少运行期授权")

            if self.config.recipient_selection.mode == "bottom_of_picker":
                log.info(
                    "收件人选择策略：bottom_of_picker；每批打开选择聊天弹窗后滚到底部，连续选择底部最多 %s 个会话；员工/机器人不过滤=%s",
                    self.config.batch_size,
                    self.config.recipient_selection.allow_staff_and_bots,
                )
            batch_counts = self._split_send_count(send_count)
            self._progress_total_batches = len(batch_counts)
            for batch_index, batch_count in enumerate(batch_counts, start=1):
                if self.stop.should_stop():
                    status = "stopped"
                    break
                if batch_index == 1 and self.config.require_confirm_first_batch:
                    self._confirm("第一批开始前确认。dry-run 不会真实发送。")
                log.info("开始处理批次 %s，计划会话数=%s", batch_index, batch_count)
                sent = self._run_batch(batch_index, batch_count)
                self.sent_count += sent
                if self.boundary_reached:
                    log.info("检测到哨兵边界，尾批处理完成后结束任务")
                    break
                if batch_index < len(batch_counts) and self.config.batch_interval_sec > 0:
                    self._sleep(self.config.batch_interval_sec)
        except Exception as exc:
            if self.stop.should_stop():
                status = "stopped"
                log.warning("运行已按停止请求结束：%s", exc)
            else:
                status = "failed"
                log.exception("流程失败：%s", exc)
                self.screen.save_error("flow_failed", str(exc))
                raise
        finally:
            if self.install_stop_hotkey:
                self.stop.uninstall()
        result = FlowResult(status=status, summary={"planned": send_count, "sent": self.sent_count})
        self._emit_progress("run_finished", status=status, summary=result.summary)
        return result

    def _split_send_count(self, send_count: int) -> list[int]:
        return [
            min(self.config.batch_size, send_count - offset)
            for offset in range(0, send_count, self.config.batch_size)
        ]

    def preflight_cache_from_values(
        self,
        *,
        window_rect: WindowRect,
        recipient_checkbox_points_bottom_to_top: list[tuple[float, float]],
        recipient_scroll_drag: tuple[tuple[int, int], tuple[int, int]] | None,
        recipient_scroll_track: tuple[int, int] | None,
        final_send_button_ratio: tuple[float, float],
    ) -> FastPathState:
        return FastPathState(
            window_rect=window_rect,
            recipient_checkbox_points_bottom_to_top=list(recipient_checkbox_points_bottom_to_top),
            recipient_scroll_drag=recipient_scroll_drag,
            recipient_scroll_track=recipient_scroll_track,
            final_send_button_ratio=final_send_button_ratio,
            ready=True,
            available_from_batch=1,
        )

    def apply_preflight_cache(self, cache: FastPathState | None) -> None:
        if cache is None or not cache.ready:
            return
        self._fast_path = FastPathState(
            window_rect=cache.window_rect,
            recipient_checkbox_points_bottom_to_top=list(cache.recipient_checkbox_points_bottom_to_top or []),
            recipient_scroll_drag=cache.recipient_scroll_drag,
            recipient_scroll_track=cache.recipient_scroll_track,
            final_send_button_ratio=cache.final_send_button_ratio,
            ready=True,
            available_from_batch=cache.available_from_batch,
        )
        if self._fast_path.recipient_checkbox_points_bottom_to_top:
            self._last_recipient_checkbox_x_ratio = (
                sum(x for x, _y in self._fast_path.recipient_checkbox_points_bottom_to_top)
                / len(self._fast_path.recipient_checkbox_points_bottom_to_top)
            )

    def export_preflight_cache(self) -> FastPathState:
        return FastPathState(
            window_rect=self._fast_path.window_rect,
            recipient_checkbox_points_bottom_to_top=list(self._fast_path.recipient_checkbox_points_bottom_to_top or []),
            recipient_scroll_drag=self._fast_path.recipient_scroll_drag,
            recipient_scroll_track=self._fast_path.recipient_scroll_track,
            final_send_button_ratio=self._fast_path.final_send_button_ratio,
            ready=self._fast_path.ready,
            available_from_batch=self._fast_path.available_from_batch,
        )

    def preflight_first_batch_until_final_send_button(self, selected_count: int) -> FastPathState:
        """Run the real first-batch path until the final Send button is recognized, then cancel."""
        batch_no = 1
        with self._step("检查预演定位企业微信窗口", batch=batch_no):
            rect = self._locate_or_reuse_window(batch_no)
        if rect is None:
            raise RuntimeError("检查预演需要先找到企业微信窗口")
        assert_batch_selection_count(selected_count, self.config.batch_size)

        with self._step("检查预演复查源消息勾选", batch=batch_no):
            self._assert_exact_source_selection(rect, "preflight_source_before_forward")

        with self._step("检查预演打开收件人选择弹窗", batch=batch_no):
            self._open_recipient_picker_from_source(rect, batch_no)

        picker_open = True
        try:
            with self._step("检查预演收件人列表滚动到底部", batch=batch_no):
                self._scroll_recipient_picker_to_bottom(rect, batch_no, require_scrollbar_thumb=True)
                self._assert_recipient_picker_still_open(rect, batch_no, "preflight_after_scroll")

            target_checkbox_points = self._recipient_checkbox_points_bottom_to_top(selected_count, rect, batch_no)
            with self._step("检查预演勾选收件人", batch=batch_no, count=selected_count):
                for index, (x_ratio, y_ratio) in enumerate(target_checkbox_points, start=1):
                    if not self._click_recipient_checkbox(rect, x_ratio, y_ratio):
                        raise RuntimeError(
                            f"检查预演无法点击第 {index} 个接收会话：ratio=({x_ratio:.3f}, {y_ratio:.3f})"
                        )
                    self._sleep(0.12)

                selected_points = self._left_selected_checkbox_y_ratios(
                    rect,
                    "preflight_recipients_selected_verify",
                )
                if len(selected_points) != selected_count:
                    raise RuntimeError(
                        f"检查预演收件人数量复核失败：expected={selected_count} actual={len(selected_points)}"
                    )

            with self._step("检查预演识别最终发送按钮", batch=batch_no, count=selected_count):
                final_send_button = self._detect_final_send_button_ratio(rect)
                if final_send_button is None:
                    raise RuntimeError("检查预演未识别到会话选择框内的发送按钮")
                self._fast_path.final_send_button_ratio = final_send_button

            self._fast_path.window_rect = rect
            self._fast_path.recipient_checkbox_points_bottom_to_top = list(target_checkbox_points)
            self._fast_path.available_from_batch = 1
            self._mark_fast_path_ready(batch_no, rect)
            self._fast_path.ready = True
            self._fast_path.available_from_batch = 1
            return self.export_preflight_cache()
        finally:
            if picker_open:
                self._cancel_recipient_picker(rect)

    def _run_batch(self, batch_no: int, batch_count: int, *, total_batches: int | None = None) -> int:
        total_batches = self._progress_total_batches if total_batches is None else total_batches
        self._emit_progress(
            "batch_started",
            batch_no=batch_no,
            total_batches=total_batches,
            target_count=batch_count,
            summary={"planned": batch_count, "sent": self.sent_count},
        )
        self.screen.save_checkpoint(f"batch_{batch_no}_before")
        if self.config.dry_run:
            sent = self._run_bottom_picker_batch(batch_no, batch_count)
        else:
            sent = self._run_real_bottom_picker_batch(batch_no, batch_count)
        self.screen.save_checkpoint(f"batch_{batch_no}_after")
        self._emit_progress(
            "batch_finished",
            batch_no=batch_no,
            total_batches=total_batches,
            summary={"planned": batch_count, "sent": self.sent_count + sent},
        )
        return sent

    def _run_bottom_picker_batch(self, batch_no: int, batch_count: int) -> int:
        """新方案 dry-run：不搜索群名，模拟在选择聊天弹窗底部连续勾选。

        真实实现时这里会：打开"选择聊天/发送给"弹窗 -> 滚到底部 -> 从底部往上勾选
        最多 batch_size 个会话 -> 发送。用户已确认员工/机器人也允许被选中，所以此策略
        不做群类型过滤，也不依赖群名 OCR。
        """
        repeats = self.config.recipient_selection.scroll_to_bottom_repeats
        log.info("[dry-run] 批次 %s：假定已打开选择聊天弹窗，滚动到底部 repeats=%s", batch_no, repeats)
        log.info("[dry-run] 批次 %s：从弹窗底部连续选择 %s 个会话", batch_no, batch_count)
        selected_count = 0
        for index in range(1, batch_count + 1):
            if self.stop.should_stop():
                break
            log.info("[dry-run] 模拟勾选底部第 %s 个会话", index)
            selected_count += 1

        assert_batch_selection_count(selected_count, self.config.batch_size)
        log.info("[dry-run] 批次 %s 已模拟从底部选择 %s 个会话；最终发送被跳过", batch_no, selected_count)
        return 0

    def _run_real_bottom_picker_batch(self, batch_no: int, selected_count: int) -> int:
        """实机方案：使用已校准的相对坐标完成一次底部会话批量发送。"""
        with self._step("定位企业微信窗口", batch=batch_no):
            rect = self._locate_or_reuse_window(batch_no)
        if rect is None:
            raise RuntimeError("真实发送需要先找到企业微信窗口")

        assert_batch_selection_count(selected_count, self.config.batch_size)
        log.warning("真实发送批次 %s：即将发送给 %s 个会话", batch_no, selected_count)

        source_verified_by_reselect = False
        if batch_no > 1:
            with self._step("重新勾选源消息", batch=batch_no):
                self._reselect_source_messages(rect)
            source_verified_by_reselect = self._can_use_fast_path(batch_no)
        if source_verified_by_reselect:
            log.info("快速模式：重新勾选源消息已完成内部复核，跳过额外源消息复查截图：batch=%s", batch_no)
        else:
            with self._step("复查源消息勾选", batch=batch_no):
                self._assert_exact_source_selection(rect, f"batch_{batch_no}_source_before_forward")

        with self._step("打开收件人选择弹窗", batch=batch_no):
            self._open_recipient_picker_from_source(rect, batch_no)

        picker_open = True
        try:
            # 接收方选择弹窗左侧列表滚到底部。
            with self._step("收件人列表滚动到底部", batch=batch_no, repeats=self.config.recipient_selection.scroll_to_bottom_repeats):
                self._scroll_recipient_picker_to_bottom(rect, batch_no)
                self._assert_recipient_picker_still_open(rect, batch_no, "after_scroll")

            target_checkbox_points = self._recipient_checkbox_points_bottom_to_top(selected_count, rect, batch_no)
            target_checkbox_y = [y_ratio for _x_ratio, y_ratio in target_checkbox_points]
            with self._step("勾选收件人", batch=batch_no, count=selected_count):
                for index, (x_ratio, y_ratio) in enumerate(target_checkbox_points):
                    if self.stop.should_stop():
                        raise RuntimeError("急停触发")
                    click_started = time.monotonic()
                    if not self._click_recipient_checkbox(rect, x_ratio, y_ratio):
                        raise RuntimeError(
                            f"无法点击第 {index + 1} 个接收会话："
                            f"ratio=({x_ratio:.3f}, {y_ratio:.3f})"
                        )
                    self._sleep(0.12)
                    log.info(
                        "勾选收件人点击完成：batch=%s index=%s/%s "
                        "ratio=(%.3f, %.3f) elapsed=%.3fs",
                        batch_no,
                        index + 1,
                        selected_count,
                        x_ratio,
                        y_ratio,
                        time.monotonic() - click_started,
                    )

                verify_started = time.monotonic()
                selected_points = self._left_selected_checkbox_y_ratios(
                    rect,
                    f"batch_{batch_no}_recipients_selected_verify",
                )
                log.info(
                    "勾选收件人整体验证完成：batch=%s expected=%s actual=%s "
                    "points=%s elapsed=%.2fs",
                    batch_no,
                    selected_count,
                    len(selected_points),
                    [round(point, 3) for point in selected_points],
                    time.monotonic() - verify_started,
                )
                if len(selected_points) != selected_count:
                    raise RuntimeError(
                        f"勾选收件人数量复核失败：expected={selected_count} "
                        f"actual={len(selected_points)}"
                    )

            self.screen.save_checkpoint(f"batch_{batch_no}_recipients_selected", region=Region(rect.left, rect.top, rect.width, rect.height))

            sentinel = self.config.recipient_selection.sentinel
            effective_count = selected_count
            if sentinel.enabled:
                with self._step("识别并检查哨兵会话", batch=batch_no, expected=selected_count):
                    selected = self._read_ordered_selected_recipients(rect, target_checkbox_y, selected_count)
                if not selected:
                    if sentinel.stop_on_detection_failure:
                        raise RuntimeError("已启用哨兵边界，但左侧已勾选会话识别失败；为避免误发已停止")
                else:
                    log.info(
                        "哨兵检查名单：batch=%s bottom_to_top=%s sentinel_names=%s",
                        batch_no,
                        [item.name for item in selected],
                        list(sentinel.names),
                    )
                    if len(selected) != selected_count:
                        log.warning(
                            "左侧可见已勾选会话数量与预期不一致：expected=%s actual=%s",
                            selected_count,
                            len(selected),
                        )
                        if sentinel.stop_on_detection_failure:
                            raise RuntimeError(
                                f"左侧已勾选会话数量与预期不一致：expected={selected_count} actual={len(selected)}；为避免误发已停止"
                            )
                    if sentinel.stop_on_detection_failure and any(
                        self._recipient_match_key(item.name).startswith("unknown-")
                        for item in selected
                    ):
                        raise RuntimeError(
                            "哨兵检查存在未识别的会话名称，无法证明哨兵不在其中；为避免误发已停止"
                        )
                    trim = self._build_sentinel_trim_plan(selected)
                    log.info(
                        "哨兵截断计划：batch=%s boundary_reached=%s send=%s remove=%s",
                        batch_no,
                        trim.boundary_reached,
                        [item.name for item in trim.send],
                        [item.name for item in trim.remove],
                    )
                    if trim.boundary_reached:
                        self.boundary_reached = True
                        if len(trim.send) == 0:
                            self._cancel_recipient_picker(rect)
                            picker_open = False
                            return 0
                        self._uncheck_left_recipients(rect, trim.remove)
                        remaining_y = [
                            item.y_ratio
                            for item in trim.send
                            if item.y_ratio is not None
                        ]
                        with self._step("复核哨兵截断结果", batch=batch_no, expected=len(trim.send)):
                            remaining = self._read_ordered_selected_recipients(
                                rect,
                                remaining_y,
                                len(trim.send),
                            )
                        if (
                            len(remaining) != len(trim.send)
                            or self._contains_sentinel(remaining)
                            or any(
                                self._recipient_match_key(item.name).startswith("unknown-")
                                for item in remaining
                            )
                        ):
                            raise RuntimeError(
                                "哨兵截断后复核失败："
                                f"expected={len(trim.send)} actual={len(remaining)} "
                                f"remaining={[item.name for item in remaining]}；为避免误发已停止"
                            )
                        effective_count = len(trim.send)
                        self.screen.save_checkpoint(f"batch_{batch_no}_recipients_trimmed", region=Region(rect.left, rect.top, rect.width, rect.height))

            if self.stop.should_stop():
                raise RuntimeError("急停触发，最终发送按钮未点击")

            # 弹窗右下角发送按钮。
            with self._step("点击最终发送按钮", batch=batch_no, count=effective_count):
                if not self._click_final_send_button(rect):
                    raise RuntimeError("无法点击发送按钮")
            self._sleep(2.0)
            with self._step("发送后截图复查", batch=batch_no):
                self._verify_post_send_result(rect, batch_no)
            picker_open = False
            self._mark_fast_path_ready(batch_no, rect)
            return effective_count
        except Exception:
            if picker_open:
                if self.stop.should_stop():
                    send_keys = getattr(self.window, "send_keys", None)
                    if callable(send_keys):
                        send_keys("{ESC}")
                else:
                    self._cancel_recipient_picker(rect)
            raise

    def _can_use_fast_path(self, batch_no: int) -> bool:
        return bool(
            batch_no >= self._fast_path.available_from_batch
            and self._fast_path.ready
            and self._fast_path.window_rect is not None
        )

    def _locate_or_reuse_window(self, batch_no: int) -> WindowRect | None:
        if batch_no > 1 and self._can_use_fast_path(batch_no):
            rect = self._fast_path.window_rect
            log.info("快速模式：复用第一批企业微信窗口位置：batch=%s rect=%s", batch_no, rect)
            self._activate_window_for_capture()
            return rect
        rect = self.window.locate()
        if rect is not None:
            self._fast_path.window_rect = rect
            log.info("记录企业微信窗口位置供后续快速模式复用：batch=%s rect=%s", batch_no, rect)
        return rect

    def _mark_fast_path_ready(self, batch_no: int, rect: WindowRect) -> None:
        if batch_no != 1 or self.boundary_reached:
            return
        if self._fast_path.window_rect is None:
            self._fast_path.window_rect = rect
        missing = []
        if not self._fast_path.recipient_checkbox_points_bottom_to_top:
            missing.append("recipient_checkbox_points")
        if self._fast_path.recipient_scroll_drag is None or self._fast_path.recipient_scroll_track is None:
            missing.append("recipient_scroll")
        if self._fast_path.final_send_button_ratio is None:
            missing.append("final_send_button")
        if missing:
            log.warning("快速模式未启用：第一批缺少缓存项 batch=%s missing=%s", batch_no, missing)
            return
        self._fast_path.ready = True
        log.info(
            "快速模式已启用：后续批次将复用第一批坐标 rect=%s recipient_points=%s scroll_drag=%s scroll_track=%s final_send_button=%s",
            self._fast_path.window_rect,
            [(round(x, 3), round(y, 3)) for x, y in self._fast_path.recipient_checkbox_points_bottom_to_top],
            self._fast_path.recipient_scroll_drag,
            self._fast_path.recipient_scroll_track,
            tuple(round(value, 3) for value in self._fast_path.final_send_button_ratio),
        )

    def _verify_post_send_result(self, rect: WindowRect, batch_no: int) -> None:
        if batch_no > 1 and self._can_use_fast_path(batch_no):
            self._activate_window_for_capture()
            evidence = self.screen.save_checkpoint(
                f"batch_{batch_no}_post_send_picker_title_fast",
                region=self._recipient_picker_title_region(rect),
            )
            is_capture_evidence = getattr(self.screen, "is_capture_evidence", None)
            evidence_ok = bool(is_capture_evidence(evidence)) if callable(is_capture_evidence) else evidence.suffix.lower() == ".png"
            if not evidence_ok:
                raise RuntimeError("发送后局部截图证据不可用，无法确认本批发送结果")
            log.info("快速模式：发送后仅保存弹窗标题区域作为关闭证据，跳过 OCR 复查：batch=%s screenshot=%s", batch_no, evidence)
            return

        evidence = self.screen.save_checkpoint(f"batch_{batch_no}_post_send", region=Region(rect.left, rect.top, rect.width, rect.height))
        is_capture_evidence = getattr(self.screen, "is_capture_evidence", None)
        evidence_ok = bool(is_capture_evidence(evidence)) if callable(is_capture_evidence) else evidence.suffix.lower() == ".png"
        if not evidence_ok:
            raise RuntimeError("发送后截图证据不可用，无法确认本批发送结果")
        title_shot = self.screen.save_checkpoint(
            f"batch_{batch_no}_post_send_picker_title",
            region=self._recipient_picker_title_region(rect),
        )
        if self._has_ocr_text(title_shot, ("发送给", "分别发送给")):
            raise RuntimeError("发送后仍检测到收件人弹窗，无法确认本批发送结果")
        wide_title_shot = self.screen.save_checkpoint(
            f"batch_{batch_no}_post_send_picker_title_wide",
            region=self._recipient_picker_wide_region(rect),
        )
        if self._has_ocr_text(wide_title_shot, ("发送给", "分别发送给")):
            raise RuntimeError("发送后宽区域仍检测到收件人弹窗，无法确认本批发送结果")

    def _open_recipient_picker_from_source(self, rect: WindowRect, batch_no: int) -> None:
        started = time.monotonic()
        configured_x, configured_y = self.config.source_selection.forward_button_ratio
        forward_x = min(0.98, max(0.02, float(configured_x)))
        forward_y = min(0.98, max(0.02, float(configured_y)))
        click_x, click_y = rect.relative_point(forward_x, forward_y)
        log.info(
            "开始点击检查阶段识别到的逐条转发按钮：batch=%s ratio=(%.3f, %.3f) abs=(%s, %s) rect=%s",
            batch_no,
            forward_x,
            forward_y,
            click_x,
            click_y,
            rect,
        )
        if not self.window.click_relative(rect, forward_x, forward_y):
            elapsed = time.monotonic() - started
            log.error("点击逐条转发按钮失败：batch=%s ratio=(%.3f, %.3f) elapsed=%.2fs", batch_no, forward_x, forward_y, elapsed)
            raise RuntimeError("无法点击检查阶段识别到的逐条转发按钮")
        elapsed = time.monotonic() - started
        log.info("点击逐条转发按钮完成：batch=%s elapsed=%.2fs", batch_no, elapsed)

        started = time.monotonic()
        log.info("开始等待发送给弹窗出现：batch=%s", batch_no)
        if batch_no > 1 and self._can_use_fast_path(batch_no):
            self._sleep(0.35)
            picker_shot = self.screen.save_checkpoint(
                f"batch_{batch_no}_recipient_picker_opened_fast",
                region=self._recipient_picker_title_region(rect),
            )
            log.info(
                "快速模式：复用第一批弹窗位置，跳过发送给标题 OCR：batch=%s screenshot=%s elapsed=%.2fs",
                batch_no,
                picker_shot,
                time.monotonic() - started,
            )
            return
        picker_shot = self._wait_for_recipient_picker_title(
            rect,
            batch_no,
            prefix="attempt",
            attempts=3,
            started=started,
        )
        if picker_shot is not None:
            return
        picker_shot = getattr(self, "_last_recipient_picker_probe", picker_shot)
        elapsed = time.monotonic() - started
        log.error("发送给弹窗识别失败：batch=%s screenshot=%s elapsed=%.2fs", batch_no, picker_shot, elapsed)
        raise RuntimeError(f"点击逐条转发后未识别到发送给弹窗，已停止以避免误点会话列表。screenshot={picker_shot}")

    def _wait_for_recipient_picker_title(
        self,
        rect: WindowRect,
        batch_no: int,
        *,
        prefix: str,
        attempts: int,
        started: float,
    ):
        picker_shot = None
        for attempt in range(1, attempts + 1):
            self._sleep(0.20 if attempt == 1 else 0.15)
            suffix = f"attempt_{attempt}" if prefix == "attempt" else f"{prefix}_attempt_{attempt}"
            picker_shot = self.screen.save_checkpoint(
                f"batch_{batch_no}_recipient_picker_opened_{suffix}",
                region=self._recipient_picker_title_region(rect),
            )
            self._last_recipient_picker_probe = picker_shot
            if self._has_ocr_text(picker_shot, ("发送给", "分别发送给")):
                elapsed = time.monotonic() - started
                log.info(
                    "发送给弹窗识别完成：batch=%s attempt=%s screenshot=%s elapsed=%.2fs",
                    batch_no,
                    suffix,
                    picker_shot,
                    elapsed,
                )
                return picker_shot
            log.info(
                "发送给弹窗尚未识别到：batch=%s attempt=%s screenshot=%s elapsed=%.2fs",
                batch_no,
                suffix,
                picker_shot,
                time.monotonic() - started,
            )
            wide_shot = self.screen.save_checkpoint(
                f"batch_{batch_no}_recipient_picker_opened_{suffix}_wide",
                region=self._recipient_picker_wide_region(rect),
            )
            self._last_recipient_picker_probe = wide_shot
            if self._has_ocr_text(wide_shot, ("发送给", "分别发送给")):
                elapsed = time.monotonic() - started
                log.info(
                    "发送给弹窗宽区域识别完成：batch=%s attempt=%s screenshot=%s elapsed=%.2fs",
                    batch_no,
                    suffix,
                    wide_shot,
                    elapsed,
                )
                return wide_shot
            log.info(
                "发送给弹窗宽区域仍未识别到：batch=%s attempt=%s screenshot=%s elapsed=%.2fs",
                batch_no,
                suffix,
                wide_shot,
                time.monotonic() - started,
            )
        return None

    def _recipient_checkbox_x_ratio(self, rect: WindowRect) -> float:
        if self._last_recipient_checkbox_x_ratio is not None:
            return self._last_recipient_checkbox_x_ratio
        if rect.width >= 1600:
            return 0.302
        return 0.260

    def _recipient_picker_title_region(self, rect: WindowRect) -> Region:
        return Region(
            left=rect.left + round(rect.width * 0.465),
            top=rect.top + round(rect.height * 0.155),
            width=round(rect.width * 0.190),
            height=round(rect.height * 0.145),
        )

    def _recipient_picker_wide_region(self, rect: WindowRect) -> Region:
        return Region(
            left=rect.left + round(rect.width * 0.260),
            top=rect.top + round(rect.height * 0.090),
            width=round(rect.width * 0.520),
            height=round(rect.height * 0.720),
        )

    def _click_final_send_button(self, rect: WindowRect) -> bool:
        detected = self._fast_path.final_send_button_ratio
        if detected is not None and self._fast_path.ready:
            log.info("快速模式：复用第一批最终发送按钮坐标：ratio=(%.3f, %.3f)", detected[0], detected[1])
        else:
            detected = self._detect_final_send_button_ratio(rect)
        if detected is None:
            log.error("未能识别最终发送按钮，已停止避免误点：rect=%s", rect)
            return False
        self._fast_path.final_send_button_ratio = detected
        send_x, send_y = detected
        click_x, click_y = rect.relative_point(send_x, send_y)
        log.info(
            "点击 OCR/图像识别到的最终发送按钮：ratio=(%.3f, %.3f) abs=(%s, %s)",
            send_x,
            send_y,
            click_x,
            click_y,
        )
        click_screen = getattr(self.window, "click_screen", None)
        if callable(click_screen):
            return bool(click_screen(click_x, click_y))
        return bool(self.window.click_relative(rect, send_x, send_y))

    def _detect_final_send_button_ratio(self, rect: WindowRect) -> tuple[float, float] | None:
        region = self._final_send_button_region(rect)
        self._activate_window_for_capture()
        region_path = self.screen.save_checkpoint("final_send_button_probe_region", region=region)
        size_getter = getattr(self.screen, "image_size", None)
        region_size = size_getter(region_path) if callable(size_getter) else None
        if not region_size or region_size[0] <= 0 or region_size[1] <= 0:
            region_size = (region.width, region.height)
        region_ratio = (
            (region.left - rect.left) / rect.width,
            (region.top - rect.top) / rect.height,
            region.width / rect.width,
            region.height / rect.height,
        )
        detected = self._detect_final_send_button_ratio_in_image(
            region_path,
            region_size,
            window_region_ratio=region_ratio,
            blue_search_region_ratio=(0.0, 0.0, 1.0, 1.0),
        )
        if detected is not None:
            log.info(
                "通过发送按钮局部区域识别到最终发送按钮：region=%s ratio=(%.3f, %.3f)",
                region,
                detected[0],
                detected[1],
            )
            return detected
        log.warning("局部区域未识别到最终发送按钮，回退整窗识别：region=%s screenshot=%s", region, region_path)

        image_path = self._save_window_checkpoint("final_send_button_probe", rect)
        image_size = size_getter(image_path) if callable(size_getter) else None
        if not image_size or image_size[0] <= 0 or image_size[1] <= 0:
            image_size = (rect.width, rect.height)
        return self._detect_final_send_button_ratio_in_image(
            image_path,
            image_size,
            window_region_ratio=(0.0, 0.0, 1.0, 1.0),
            blue_search_region_ratio=(0.35, 0.55, 0.40, 0.35),
        )

    def _final_send_button_region(self, rect: WindowRect) -> Region:
        return Region(
            left=rect.left + round(rect.width * 0.420),
            top=rect.top + round(rect.height * 0.660),
            width=round(rect.width * 0.300),
            height=round(rect.height * 0.220),
        )

    def _detect_final_send_button_ratio_in_image(
        self,
        image_path,
        image_size: tuple[int, int],
        *,
        window_region_ratio: tuple[float, float, float, float],
        blue_search_region_ratio: tuple[float, float, float, float],
    ) -> tuple[float, float] | None:
        size_getter = getattr(self.screen, "image_size", None)
        image_width, image_height = image_size
        if image_width <= 0 or image_height <= 0:
            return None
        offset_x, offset_y, width_ratio, height_ratio = window_region_ratio

        try:
            lines = self.screen.ocr_lines(image_path=image_path)
        except Exception as exc:
            log.warning("最终发送按钮 OCR 识别失败：%s", exc)
            lines = []

        candidates: list[tuple[int, float, float, str]] = []
        for line in lines:
            text = line.text.replace(" ", "")
            if "分别发送给" in text or "发送给" == text:
                continue
            if "分别发送" in text:
                priority = 2
            elif text == "发送":
                priority = 1
            else:
                continue
            local_x = (line.left + line.width / 2.0) / image_width
            local_y = (line.top + line.height / 2.0) / image_height
            center_x = offset_x + local_x * width_ratio
            center_y = offset_y + local_y * height_ratio
            if 0.35 <= center_x <= 0.75 and 0.55 <= center_y <= 0.90:
                candidates.append((priority, center_x, center_y, line.text))
        if candidates:
            _priority, x_ratio, y_ratio, text = sorted(
                candidates,
                key=lambda item: (item[0], -abs(item[1] - 0.55), item[2]),
                reverse=True,
            )[0]
            log.info("识别到最终发送按钮：text=%s ratio=(%.3f, %.3f)", text, x_ratio, y_ratio)
            return (x_ratio, y_ratio)

        button = self._detect_blue_final_send_button_ratio(
            image_path,
            image_size,
            search_region_ratio=blue_search_region_ratio,
        )
        if button is not None:
            x_ratio = offset_x + button[0] * width_ratio
            y_ratio = offset_y + button[1] * height_ratio
            log.info("通过蓝色按钮区域识别到最终发送按钮：ratio=(%.3f, %.3f)", x_ratio, y_ratio)
            return (x_ratio, y_ratio)
        return None

    def _detect_blue_final_send_button_ratio(
        self,
        image_path,
        image_size: tuple[int, int],
        *,
        search_region_ratio: tuple[float, float, float, float],
    ) -> tuple[float, float] | None:
        try:
            import cv2  # type: ignore
            import numpy as np  # type: ignore
        except Exception as exc:
            log.debug("OpenCV/NumPy 不可用，跳过最终发送按钮蓝色区域识别：%s", exc)
            return None

        bitmap = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if bitmap is None:
            return None
        image_width, image_height = image_size
        if image_width <= 0 or image_height <= 0:
            return None
        search_x, search_y, search_width, search_height = search_region_ratio
        roi_left = round(image_width * search_x)
        roi_top = round(image_height * search_y)
        roi_right = round(image_width * (search_x + search_width))
        roi_bottom = round(image_height * (search_y + search_height))
        roi = bitmap[roi_top:roi_bottom, roi_left:roi_right]
        if roi.size == 0:
            return None
        mask = cv2.inRange(
            roi,
            np.array((150, 80, 0), dtype=np.uint8),
            np.array((255, 190, 110), dtype=np.uint8),
        )
        component_count, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=4)
        candidates: list[tuple[int, float, float]] = []
        for index in range(1, component_count):
            _left, _top, width, height, area = (int(value) for value in stats[index])
            if area < 600 or width < 60 or height < 20:
                continue
            center_x, center_y = centroids[index]
            candidates.append((area, (roi_left + float(center_x)) / image_width, (roi_top + float(center_y)) / image_height))
        if not candidates:
            return None
        _area, x_ratio, y_ratio = sorted(candidates, key=lambda item: item[0], reverse=True)[0]
        return (x_ratio, y_ratio)

    def _recipient_scrollbar_track_point(self, rect: WindowRect) -> tuple[float, float]:
        if rect.width >= 1600:
            return (0.494, self._recipient_scrollbar_track_bottom_ratio(rect))
        return (0.492, self._recipient_scrollbar_track_bottom_ratio(rect))

    def _recipient_scrollbar_track_bottom_ratio(self, rect: WindowRect) -> float:
        if rect.width >= 1600:
            return 0.855
        return 0.850

    def _recipient_scrollbar_thumb_at_bottom(
        self,
        thumb: tuple[float, float, float] | None,
        rect: WindowRect,
    ) -> bool:
        if thumb is None:
            return False
        _x_ratio, _top_ratio, bottom_ratio = thumb
        return bottom_ratio >= self._recipient_scrollbar_track_bottom_ratio(rect) - 0.010

    def _detect_recipient_scrollbar_thumb(
        self,
        image_path,
        rect: WindowRect,
    ) -> tuple[float, float, float] | None:
        try:
            import cv2  # type: ignore
            import numpy as np  # type: ignore
        except Exception as exc:
            log.debug("OpenCV/NumPy 不可用，跳过收件人滚动条滑块识别：%s", exc)
            return None

        bitmap = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if bitmap is None:
            return None
        image_height, image_width = bitmap.shape[:2]
        if image_width <= 0 or image_height <= 0:
            return None

        track_x_ratio, _track_bottom = self._recipient_scrollbar_track_point(rect)
        roi_left = max(0, round(image_width * (track_x_ratio - 0.035)))
        roi_right = min(image_width, round(image_width * (track_x_ratio + 0.035)))
        roi_top = max(0, round(image_height * 0.200))
        roi_bottom = min(image_height, round(image_height * 0.900))
        roi = bitmap[roi_top:roi_bottom, roi_left:roi_right]
        if roi.size == 0:
            return None

        blue, green, red = cv2.split(roi)
        maximum = np.maximum(np.maximum(red, green), blue)
        minimum = np.minimum(np.minimum(red, green), blue)
        mask = (
            (maximum - minimum <= 14)
            & (red >= 105)
            & (red <= 205)
            & (green >= 105)
            & (green <= 205)
            & (blue >= 105)
            & (blue <= 205)
        ).astype("uint8") * 255

        component_count, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
        candidates: list[tuple[int, float, float, float]] = []
        for index in range(1, component_count):
            left, top, width, height, area = (int(value) for value in stats[index])
            if area < 40:
                continue
            if height < max(24, image_height * 0.018):
                continue
            if width < 3 or width > max(36, image_width * 0.025):
                continue
            if height < width * 4:
                continue
            center_x, _center_y = centroids[index]
            x_ratio = (roi_left + float(center_x)) / image_width
            if abs(x_ratio - track_x_ratio) > 0.035:
                continue
            top_ratio = (roi_top + top) / image_height
            bottom_ratio = (roi_top + top + height) / image_height
            candidates.append((area, x_ratio, top_ratio, bottom_ratio))

        if not candidates:
            return None
        _area, x_ratio, top_ratio, bottom_ratio = sorted(candidates, key=lambda item: item[0], reverse=True)[0]
        return (x_ratio, top_ratio, bottom_ratio)

    def _recipient_scrollbar_drag_candidates(
        self,
        rect: WindowRect,
        thumb: tuple[float, float, float] | None = None,
    ) -> list[tuple[tuple[int, int], tuple[int, int]]]:
        if thumb is not None:
            x_ratio, top_ratio, bottom_ratio = thumb
            height_ratio = max(0.0, bottom_ratio - top_ratio)
            center_y_ratio = top_ratio + height_ratio / 2.0
            target_y_ratio = self._recipient_scrollbar_track_bottom_ratio(rect) - height_ratio / 2.0
            target_y_ratio = min(self._recipient_scrollbar_track_bottom_ratio(rect), max(center_y_ratio, target_y_ratio))
            return [(rect.relative_point(x_ratio, center_y_ratio), rect.relative_point(x_ratio, target_y_ratio))]
        if rect.width >= 1600:
            x_ratio, start_y_ratios, end_y_ratio = (
                0.494,
                (0.285, 0.330, 0.375, 0.420),
                self._recipient_scrollbar_track_bottom_ratio(rect),
            )
        else:
            x_ratio, start_y_ratios, end_y_ratio = (
                0.492,
                (0.315, 0.360, 0.405),
                self._recipient_scrollbar_track_bottom_ratio(rect),
            )
        end = rect.relative_point(x_ratio, end_y_ratio)
        return [(rect.relative_point(x_ratio, start_y), end) for start_y in start_y_ratios]

    def _recipient_list_compare_region(self, rect: WindowRect) -> Region:
        return Region(
            left=rect.left + round(rect.width * 0.285),
            top=rect.top + round(rect.height * 0.325),
            width=round(rect.width * 0.200),
            height=round(rect.height * 0.510),
        )

    def _save_recipient_list_checkpoint(self, name: str, rect: WindowRect) -> Path:
        self._activate_window_for_capture()
        return self.screen.save_checkpoint(name, region=self._recipient_list_compare_region(rect))

    def _recipient_list_hover_point(self, rect: WindowRect) -> tuple[int, int]:
        x_ratio = 0.365 if rect.width >= 1600 else 0.330
        return rect.relative_point(x_ratio, 0.560)

    def _reveal_recipient_scrollbar(self, rect: WindowRect, batch_no: int) -> tuple[int, int] | None:
        hover_x, hover_y = self._recipient_list_hover_point(rect)
        move_screen = getattr(self.window, "move_screen", None)
        if not callable(move_screen):
            log.warning(
                "Cannot reveal recipient scrollbar because window.move_screen is unavailable: batch=%s hover=(%s, %s)",
                batch_no,
                hover_x,
                hover_y,
            )
            return None
        log.info("Reveal recipient scrollbar by hovering list: batch=%s hover=(%s, %s)", batch_no, hover_x, hover_y)
        if not move_screen(hover_x, hover_y):
            log.warning(
                "Failed to hover recipient list before scrollbar detection: batch=%s hover=(%s, %s)",
                batch_no,
                hover_x,
                hover_y,
            )
            return None
        self._sleep(0.20)
        return (hover_x, hover_y)

    def _scroll_recipient_picker_to_bottom(
        self,
        rect: WindowRect,
        batch_no: int,
        *,
        require_scrollbar_thumb: bool = False,
    ) -> None:
        """拖动会话列表滚动条到底部，并通过稳定画面确认位置。"""
        click_x_ratio, click_y_ratio = self._recipient_scrollbar_track_point(rect)
        click_x, click_y = rect.relative_point(click_x_ratio, click_y_ratio)
        configured_repeats = self.config.recipient_selection.scroll_to_bottom_repeats
        max_fallback_rounds = min(3, max(1, configured_repeats))
        drag_screen = getattr(self.window, "drag_screen", None)
        if self._can_use_fast_path(batch_no) and self._fast_path.recipient_scroll_drag is not None:
            if not callable(drag_screen):
                raise RuntimeError("当前环境不支持拖动收件人列表滚动条，已停止避免未到底就选择会话")
            drag_start, drag_end = self._fast_path.recipient_scroll_drag
            track = self._fast_path.recipient_scroll_track or (click_x, click_y)
            log.info(
                "Fast path: reuse cached recipient scrollbar coordinates: batch=%s drag_start=%s drag_end=%s track=%s",
                batch_no,
                drag_start,
                drag_end,
                track,
            )
            if not drag_screen(*drag_start, *drag_end, duration=0.25):
                raise RuntimeError(f"快速模式拖动收件人滚动条失败：batch={batch_no} start={drag_start} end={drag_end}")
            self._sleep(0.10)
            if not self.window.click_screen(*track):
                raise RuntimeError(f"快速模式点击收件人滚动条底部轨道失败：batch={batch_no} abs={track}")
            self._sleep(0.08)
            screenshot = self._save_recipient_list_checkpoint(f"batch_{batch_no}_recipient_scroll_fast", rect)
            log.info("Fast path: recipient list scrolled to bottom with cached coordinates: batch=%s screenshot=%s", batch_no, screenshot)
            return
        previous_path = self._save_recipient_list_checkpoint(
            f"batch_{batch_no}_recipient_scroll_before",
            rect,
        )
        self._reveal_recipient_scrollbar(rect, batch_no)
        scrollbar_probe_path = self._save_window_checkpoint(f"batch_{batch_no}_recipient_scrollbar_before", rect)
        scrollbar_thumb = self._detect_recipient_scrollbar_thumb(scrollbar_probe_path, rect)
        if scrollbar_thumb is not None:
            log.info(
                "识别到收件人滚动条滑块：batch=%s thumb=%s at_bottom=%s screenshot=%s",
                batch_no,
                tuple(round(value, 3) for value in scrollbar_thumb),
                self._recipient_scrollbar_thumb_at_bottom(scrollbar_thumb, rect),
                scrollbar_probe_path,
            )
        else:
            if require_scrollbar_thumb:
                raise RuntimeError(
                    "检查环境未识别到会话选择框左侧列表滚动条。请确认发送给弹窗已经打开、鼠标悬停后滚动条可见，"
                    f"已停止以避免误选。batch={batch_no} screenshot={scrollbar_probe_path}"
                )
            log.warning("未识别到收件人滚动条滑块，将使用固定候选拖拽点：batch=%s screenshot=%s", batch_no, scrollbar_probe_path)
        if self._recipient_scrollbar_thumb_at_bottom(scrollbar_thumb, rect):
            drag_candidates = self._recipient_scrollbar_drag_candidates(rect, thumb=scrollbar_thumb)
            self._fast_path.recipient_scroll_track = (click_x, click_y)
            if drag_candidates:
                self._fast_path.recipient_scroll_drag = drag_candidates[0]
            log.info("收件人滚动条滑块已在底部：batch=%s screenshot=%s", batch_no, scrollbar_probe_path)
            return
        drag_candidates = self._recipient_scrollbar_drag_candidates(rect, thumb=scrollbar_thumb)
        stable_rounds = 0
        moved = False

        log.info(
            "开始滚动收件人列表：batch=%s method=scrollbar_drag "
            "drag_candidates=%s verify_track=(%s, %s) max_verify_rounds=%s",
            batch_no,
            drag_candidates,
            click_x,
            click_y,
            max_fallback_rounds,
        )
        drag_screen = getattr(self.window, "drag_screen", None)
        if not callable(drag_screen):
            raise RuntimeError("当前环境不支持拖动收件人列表滚动条，已停止避免未到底就选择会话")

        if self._can_use_fast_path(batch_no) and self._fast_path.recipient_scroll_drag is not None:
            drag_start, drag_end = self._fast_path.recipient_scroll_drag
            track = self._fast_path.recipient_scroll_track or (click_x, click_y)
            log.info(
                "快速模式：复用第一批收件人滚动条坐标：batch=%s drag_start=%s drag_end=%s track=%s",
                batch_no,
                drag_start,
                drag_end,
                track,
            )
            if not drag_screen(*drag_start, *drag_end, duration=0.25):
                raise RuntimeError(f"快速模式拖动收件人滚动条失败：batch={batch_no} start={drag_start} end={drag_end}")
            self._sleep(0.10)
            if not self.window.click_screen(*track):
                raise RuntimeError(f"快速模式点击收件人滚动条底部轨道失败：batch={batch_no} abs={track}")
            self._sleep(0.08)
            screenshot = self._save_recipient_list_checkpoint(f"batch_{batch_no}_recipient_scroll_fast", rect)
            log.info("快速模式：收件人列表滚动到底部完成：batch=%s screenshot=%s", batch_no, screenshot)
            return

        for attempt, (drag_start, drag_end) in enumerate(drag_candidates, start=1):
            log.info(
                "尝试拖动收件人滚动条：batch=%s attempt=%s start=%s end=%s",
                batch_no,
                attempt,
                drag_start,
                drag_end,
            )
            if not drag_screen(*drag_start, *drag_end, duration=0.40):
                log.warning("拖动收件人滚动条操作失败：batch=%s attempt=%s", batch_no, attempt)
                continue
            self._sleep(0.20)
            dragged_path = self._save_recipient_list_checkpoint(
                f"batch_{batch_no}_recipient_scroll_after_drag_{attempt}",
                rect,
            )
            dragged_scrollbar_path = self._save_window_checkpoint(
                f"batch_{batch_no}_recipient_scrollbar_after_drag_{attempt}",
                rect,
            )
            dragged_thumb = self._detect_recipient_scrollbar_thumb(dragged_scrollbar_path, rect)
            dragged_thumb_at_bottom = self._recipient_scrollbar_thumb_at_bottom(dragged_thumb, rect)
            difference = self._recipient_list_image_difference(previous_path, dragged_path)
            if difference is None:
                raise RuntimeError(
                    "无法比较拖动滚动条前后的截图，不能确认滚动操作是否生效。"
                    f"before={previous_path} after={dragged_path}"
                )
            moved = difference >= 1.5
            log.info(
                "收件人滚动条拖动结果：batch=%s attempt=%s difference=%.3f moved=%s screenshot=%s",
                batch_no,
                attempt,
                difference,
                moved,
                dragged_path,
            )
            log.info(
                "收件人滚动条滑块复核：batch=%s attempt=%s thumb=%s thumb_at_bottom=%s screenshot=%s",
                batch_no,
                attempt,
                tuple(round(value, 3) for value in dragged_thumb) if dragged_thumb is not None else None,
                dragged_thumb_at_bottom,
                dragged_scrollbar_path,
            )
            previous_path = dragged_path
            if moved and (dragged_thumb is None or dragged_thumb_at_bottom):
                self._fast_path.recipient_scroll_drag = (drag_start, drag_end)
                break

        if not moved:
            raise RuntimeError(
                "未能拖动收件人列表滚动条，列表没有发生移动，已停止避免从非底部选择会话。"
                f"batch={batch_no} candidates={drag_candidates} screenshot={previous_path}"
            )

        verification_rounds = max_fallback_rounds + 2
        for round_no in range(1, verification_rounds + 1):
            log.info(
                "点击收件人列表滚动条底部轨道进行到底复核：batch=%s round=%s "
                "ratio=(%.3f, %.3f) abs=(%s, %s)",
                batch_no,
                round_no,
                click_x_ratio,
                click_y_ratio,
                click_x,
                click_y,
            )
            if not self.window.click_screen(click_x, click_y):
                raise RuntimeError(
                    f"无法点击收件人列表滚动条轨道：batch={batch_no} "
                    f"abs=({click_x}, {click_y})"
                )
            self._sleep(0.12)
            current_path = self._save_recipient_list_checkpoint(
                f"batch_{batch_no}_recipient_scroll_round_{round_no}",
                rect,
            )
            current_scrollbar_path = self._save_window_checkpoint(
                f"batch_{batch_no}_recipient_scrollbar_round_{round_no}",
                rect,
            )
            current_thumb = self._detect_recipient_scrollbar_thumb(current_scrollbar_path, rect)
            current_thumb_at_bottom = self._recipient_scrollbar_thumb_at_bottom(current_thumb, rect)
            difference = self._recipient_list_image_difference(previous_path, current_path)
            if difference is None:
                raise RuntimeError(
                    "无法比较收件人列表滚动前后的截图，不能确认是否已经滚动到底部。"
                    f"before={previous_path} after={current_path}"
                )
            if difference >= 1.5:
                moved = True
                stable_rounds = 0
            else:
                stable_rounds += 1
            log.info(
                "收件人列表滚动结果：batch=%s round=%s difference=%.3f "
                "moved=%s stable_rounds=%s screenshot=%s",
                batch_no,
                round_no,
                difference,
                moved,
                stable_rounds,
                current_path,
            )
            log.info(
                "收件人滚动条滑块复核：batch=%s round=%s thumb=%s thumb_at_bottom=%s screenshot=%s",
                batch_no,
                round_no,
                tuple(round(value, 3) for value in current_thumb) if current_thumb is not None else None,
                current_thumb_at_bottom,
                current_scrollbar_path,
            )
            previous_path = current_path
            bottom_confirmed = current_thumb is None or current_thumb_at_bottom or scrollbar_thumb is not None
            if moved and stable_rounds >= 2 and bottom_confirmed:
                self._fast_path.recipient_scroll_track = (click_x, click_y)
                if self._fast_path.recipient_scroll_drag is None and drag_candidates:
                    self._fast_path.recipient_scroll_drag = drag_candidates[0]
                log.info(
                    "收件人列表已确认滚动到底部：batch=%s method=drag_then_verify moved=%s rounds=%s screenshot=%s",
                    batch_no,
                    moved,
                    round_no,
                    current_path,
                )
                return

        if not moved:
            raise RuntimeError(
                "点击滚动条轨道后未检测到收件人列表移动，滚动操作未生效，已停止避免误选。"
                f"batch={batch_no} abs=({click_x}, {click_y}) last_screenshot={previous_path}"
            )
        raise RuntimeError(
            "收件人列表在限定次数内未出现连续稳定画面，无法确认已经滚动到底部，已停止避免误选。"
            f"batch={batch_no} rounds={verification_rounds} last_screenshot={previous_path}"
        )

    def _recipient_list_image_difference(self, before_path: Path, after_path: Path) -> float | None:
        try:
            from PIL import Image, ImageChops, ImageStat

            with Image.open(before_path) as before_image, Image.open(after_path) as after_image:
                width = min(before_image.width, after_image.width)
                height = min(before_image.height, after_image.height)
                box = (0, 0, width, height)
                before_crop = before_image.convert("L").crop(box).resize((96, 160))
                after_crop = after_image.convert("L").crop(box).resize((96, 160))
                return float(ImageStat.Stat(ImageChops.difference(before_crop, after_crop)).mean[0])
        except Exception as exc:
            log.error(
                "比较收件人列表截图失败：before=%s after=%s error=%s",
                before_path,
                after_path,
                exc,
            )
            return None

    def _activate_window_for_capture(self) -> None:
        activate = getattr(self.window, "activate", None)
        if callable(activate):
            activate()
            self._sleep(0.10)

    def _save_window_checkpoint(self, name: str, rect: WindowRect):
        self._activate_window_for_capture()
        return self.screen.save_checkpoint(name, region=Region(rect.left, rect.top, rect.width, rect.height))

    def _assert_recipient_picker_still_open(self, rect: WindowRect, batch_no: int, stage: str) -> None:
        started = time.monotonic()
        self._activate_window_for_capture()
        image_path = self.screen.save_checkpoint(
            f"batch_{batch_no}_recipient_picker_{stage}",
            region=self._recipient_picker_title_region(rect),
        )
        if batch_no > 1 and self._can_use_fast_path(batch_no):
            log.info(
                "快速模式：复用第一批弹窗位置，跳过收件人弹窗 OCR 复核：batch=%s stage=%s screenshot=%s elapsed=%.2fs",
                batch_no,
                stage,
                image_path,
                time.monotonic() - started,
            )
            return
        if self._has_ocr_text(image_path, ("发送给", "分别发送给")):
            log.info(
                "收件人弹窗复核通过：batch=%s stage=%s screenshot=%s elapsed=%.2fs",
                batch_no,
                stage,
                image_path,
                time.monotonic() - started,
            )
            return
        wide_image_path = self.screen.save_checkpoint(
            f"batch_{batch_no}_recipient_picker_{stage}_wide",
            region=self._recipient_picker_wide_region(rect),
        )
        if self._has_ocr_text(wide_image_path, ("发送给", "分别发送给")):
            log.info(
                "收件人弹窗宽区域复核通过：batch=%s stage=%s screenshot=%s elapsed=%.2fs",
                batch_no,
                stage,
                wide_image_path,
                time.monotonic() - started,
            )
            return
        log.error(
            "收件人弹窗复核失败：batch=%s stage=%s screenshot=%s elapsed=%.2fs",
            batch_no,
            stage,
            wide_image_path,
            time.monotonic() - started,
        )
        raise RuntimeError(f"收件人弹窗在 {stage} 阶段未保持在前台或未识别到，已停止以避免误点。screenshot={wide_image_path}")

    def _recipient_checkbox_rows_bottom_to_top(self, selected_count: int, rect: WindowRect | None = None) -> list[float]:
        if selected_count <= 0:
            return []
        if rect is not None and rect.width >= 1600:
            checkbox_y_top_to_bottom = [0.285, 0.358, 0.432, 0.506, 0.580, 0.654, 0.728, 0.802, 0.876]
        else:
            checkbox_y_top_to_bottom = [0.319, 0.382, 0.445, 0.507, 0.569, 0.632, 0.694, 0.757, 0.819]
        if selected_count < self.config.batch_size:
            rows = checkbox_y_top_to_bottom[-selected_count:]
        else:
            rows = checkbox_y_top_to_bottom[:selected_count]
        return list(reversed(rows))

    def _recipient_checkbox_points_bottom_to_top(
        self, selected_count: int, rect: WindowRect, batch_no: int
    ) -> list[tuple[float, float]]:
        if selected_count <= 0:
            return []
        cached = self._fast_path.recipient_checkbox_points_bottom_to_top
        if self._can_use_fast_path(batch_no) and cached and len(cached) >= selected_count:
            selected = cached[:selected_count]
            log.info(
                "快速模式：复用第一批收件人复选框坐标：batch=%s count=%s points=%s",
                batch_no,
                selected_count,
                [(round(x, 3), round(y, 3)) for x, y in selected],
            )
            return selected
        image_path = self._save_window_checkpoint(f"batch_{batch_no}_recipient_checkbox_candidates", rect)
        find_outlines = getattr(self.screen, "find_checkbox_outline_ratios", None)
        min_x, max_x = self._recipient_checkbox_x_bounds(rect)
        min_y, max_y = self._recipient_checkbox_y_bounds(rect)
        scan_region = self._expanded_checkbox_scan_region(min_x, min_y, max_x, max_y)
        raw_points = list(
            find_outlines(
                image_path,
                scan_region_ratio=scan_region,
            )
        ) if callable(find_outlines) else []
        candidates = select_aligned_checkbox_column(
            raw_points,
            min_x=min_x,
            max_x=max_x,
            min_y=min_y,
            max_y=max_y,
        )
        candidates = self._dedupe_recipient_checkbox_points(candidates)
        log.info(
            "收件人复选框候选识别：batch=%s expected=%s full_window=%s raw=%s filtered=%s",
            batch_no,
            selected_count,
            image_path,
            [(round(x, 3), round(y, 3)) for x, y in raw_points],
            [(round(x, 3), round(y, 3)) for x, y in candidates],
        )
        if len(candidates) < selected_count:
            fallback_x = self._recipient_checkbox_x_ratio(rect)
            fallback_y = self._recipient_checkbox_rows_bottom_to_top(selected_count, rect)
            log.error(
                "收件人复选框候选不足，已停止避免误点：expected=%s actual=%s diagnostic_fallback=%s screenshot=%s",
                selected_count,
                len(candidates),
                [(round(fallback_x, 3), round(y, 3)) for y in fallback_y],
                image_path,
            )
            raise RuntimeError(f"未识别到足够的会话复选框：expected={selected_count} actual={len(candidates)}，已停止以避免误点。screenshot={image_path}")

        selected = sorted(candidates, key=lambda point: point[1], reverse=True)[:selected_count]
        self._last_recipient_checkbox_x_ratio = sum(x for x, _y in selected) / len(selected)
        if selected_count == self.config.batch_size or not self._fast_path.recipient_checkbox_points_bottom_to_top:
            self._fast_path.recipient_checkbox_points_bottom_to_top = list(selected)
        log.info("将从底部往上勾选收件人坐标：%s", [(round(x, 3), round(y, 3)) for x, y in selected])
        return selected

    def _expanded_checkbox_scan_region(
        self,
        min_x: float,
        min_y: float,
        max_x: float,
        max_y: float,
    ) -> tuple[float, float, float, float]:
        # 连通域必须看到完整方框。扫描区域向外留出边距，最终候选仍按
        # min/max 严格过滤，避免把相邻列的图标带入结果。
        scan_left = max(0.0, min_x - 0.020)
        scan_top = max(0.0, min_y - 0.035)
        scan_right = min(1.0, max_x + 0.020)
        scan_bottom = min(1.0, max_y + 0.035)
        return (scan_left, scan_top, scan_right - scan_left, scan_bottom - scan_top)

    def _recipient_checkbox_x_bounds(self, rect: WindowRect) -> tuple[float, float]:
        if rect.width >= 1600:
            return (0.235, 0.430)
        return (0.200, 0.380)

    def _recipient_checkbox_y_bounds(self, rect: WindowRect) -> tuple[float, float]:
        if rect.width >= 1600:
            return (0.265, 0.900)
        return (0.290, 0.870)

    def _dedupe_recipient_checkbox_points(self, points: list[tuple[float, float]]) -> list[tuple[float, float]]:
        deduped: list[tuple[float, float]] = []
        for point in sorted(points, key=lambda item: item[1]):
            if any(abs(point[1] - existing[1]) <= 0.012 for existing in deduped):
                continue
            deduped.append(point)
        return deduped

    def _build_sentinel_trim_plan(self, selected_bottom_to_top: list[SelectedRecipient]) -> SentinelTrimPlan:
        # 合并 OCR 重复检测（相邻行 Y 差 < 2% 且名称匹配键相同视为同一条）。
        deduped: list[SelectedRecipient] = []
        for item in selected_bottom_to_top:
            if deduped and item.y_ratio is not None and deduped[-1].y_ratio is not None:
                if abs(item.y_ratio - deduped[-1].y_ratio) < 0.020:
                    if self._recipient_match_key(item.name) == self._recipient_match_key(deduped[-1].name):
                        continue
            deduped.append(item)
        first_sentinel_index = next(
            (index for index, item in enumerate(deduped) if self._recipient_is_sentinel(item.name)),
            None,
        )
        if first_sentinel_index is None:
            return SentinelTrimPlan(send=deduped, remove=[], boundary_reached=False)
        return SentinelTrimPlan(
            send=deduped[:first_sentinel_index],
            remove=deduped[first_sentinel_index:],
            boundary_reached=True,
        )

    def _read_ordered_selected_recipients(
        self,
        rect: WindowRect,
        selected_y_ratios: list[float],
        expected_count: int,
    ) -> list[SelectedRecipient]:
        selected = self._read_left_selected_recipients(rect, selected_y_ratios)
        if selected and len(selected) == expected_count:
            return selected

        if selected:
            log.warning(
                "左侧已勾选列表不完整：left=%s expected=%s，按左侧结果返回并由上层决定是否停止",
                len(selected),
                expected_count,
            )
        return selected

    def _contains_sentinel(self, selected: list[SelectedRecipient]) -> bool:
        return any(self._recipient_is_sentinel(item.name) for item in selected)

    def _recipient_is_sentinel(self, name: str) -> bool:
        item_key = self._recipient_match_key(name)
        return any(
            sentinel_key and sentinel_key in item_key
            for sentinel_key in (
                self._recipient_match_key(value)
                for value in self.config.recipient_selection.sentinel.names
            )
        )

    def _selected_list_region(self, rect: WindowRect) -> Region:
        x_ratio, y_ratio, width_ratio, height_ratio = self.config.recipient_selection.sentinel.selected_list_region_ratio
        return Region(
            left=rect.left + round(rect.width * x_ratio),
            top=rect.top + round(rect.height * y_ratio),
            width=round(rect.width * width_ratio),
            height=round(rect.height * height_ratio),
        )

    def _left_candidate_region(self, rect: WindowRect) -> Region:
        if rect.width >= 1600:
            return Region(
                left=rect.left + round(rect.width * 0.255),
                top=rect.top + round(rect.height * 0.260),
                width=round(rect.width * 0.245),
                height=round(rect.height * 0.650),
            )
        return Region(
            left=rect.left + round(rect.width * 0.200),
            top=rect.top + round(rect.height * 0.260),
            width=round(rect.width * 0.300),
            height=round(rect.height * 0.610),
        )

    def _read_left_selected_recipients(self, rect: WindowRect, selected_y_ratios: list[float]) -> list[SelectedRecipient]:
        region = self._left_candidate_region(rect)
        image_path = self.screen.save_checkpoint("left_selected_recipients_ocr", region=region)
        checkbox_points = self._left_checkbox_points(rect, region, image_path)
        detected_y_ratios = [
            (region.top + round(region.height * y_ratio) - rect.top) / rect.height
            for _x_ratio, y_ratio in checkbox_points
        ]
        if len(detected_y_ratios) != len(selected_y_ratios):
            log.warning(
                "左侧蓝色勾选框检测数量与预期不一致：expected=%s actual=%s",
                len(selected_y_ratios),
                len(detected_y_ratios),
            )
            if not detected_y_ratios or len(detected_y_ratios) > len(selected_y_ratios):
                return []
        row_y_ratios = sorted(detected_y_ratios, reverse=True)

        lines = self.screen.ocr_lines(image_path=image_path)
        if not lines:
            log.warning("左侧已勾选会话 OCR 未读到文本")
            return []

        recipients: list[SelectedRecipient] = []
        row_tolerance = 0.030
        ignored_keys = ("搜索", "创建聊天", "转到微信")
        for index, y_ratio in enumerate(row_y_ratios, start=1):
            row_texts = []
            for line in lines:
                line_y_ratio = (region.top + line.center_y - rect.top) / rect.height
                if abs(line_y_ratio - y_ratio) > row_tolerance:
                    continue
                text = self._normalize_recipient_text(line.text)
                text_key = self._recipient_match_key(text)
                if not text_key or any(key in text_key for key in ignored_keys):
                    continue
                row_texts.append(text)
            name = " ".join(row_texts) if row_texts else f"unknown-{index}"
            recipients.append(SelectedRecipient(name, y_ratio))

        log.info(
            "左侧已勾选会话 OCR 结果：bottom_to_top=%s",
            [item.name for item in recipients],
        )
        if len(recipients) != len(row_y_ratios):
            log.warning(
                "左侧已勾选会话识别数量与预期不一致：expected=%s actual=%s items=%s",
                len(row_y_ratios),
                len(recipients),
                recipients,
            )
        return recipients

    def _left_selected_checkbox_y_ratios(self, rect: WindowRect, checkpoint_name: str) -> list[float]:
        image_path = self._save_window_checkpoint(checkpoint_name, rect)
        min_x, max_x = self._recipient_checkbox_x_bounds(rect)
        min_y, max_y = self._recipient_checkbox_y_bounds(rect)
        scan_region = self._expanded_checkbox_scan_region(min_x, min_y, max_x, max_y)
        raw_points = list(
            self.screen.find_selected_checkbox_ratios(
                image_path,
                scan_region_ratio=scan_region,
            )
        )
        checkbox_points = select_aligned_checkbox_column(
            raw_points,
            min_x=min_x,
            max_x=max_x,
            min_y=min_y,
            max_y=max_y,
        )
        log.info(
            "左侧已选会话复选框扫描：checkpoint=%s x_bounds=(%.3f, %.3f) y_bounds=(%.3f, %.3f) points=%s",
            checkpoint_name,
            min_x,
            max_x,
            min_y,
            max_y,
            [(round(x, 3), round(y, 3)) for x, y in checkbox_points],
        )
        return sorted(
            [
                y_ratio
                for _x_ratio, y_ratio in checkbox_points
            ],
            reverse=True,
        )

    def _left_checkbox_points(self, rect: WindowRect, region: Region, image_path) -> list[tuple[float, float]]:
        checkbox_x = self._recipient_checkbox_x_ratio(rect)
        expected_local_x = ((rect.left + rect.width * checkbox_x) - region.left) / region.width
        scan_left = max(0.0, expected_local_x - 0.070)
        scan_right = min(1.0, expected_local_x + 0.070)
        return [
            point
            for point in self.screen.find_selected_checkbox_ratios(
                image_path,
                scan_region_ratio=(scan_left, 0.0, scan_right - scan_left, 1.0),
            )
            if abs(point[0] - expected_local_x) <= 0.070
        ]

    def _is_left_row_selected(self, rect: WindowRect, y_ratio: float, *, checkpoint_name: str) -> bool:
        selected_y = self._left_selected_checkbox_y_ratios(rect, checkpoint_name)
        return any(abs(existing_y - y_ratio) <= 0.025 for existing_y in selected_y)

    def _click_recipient_checkbox_until_selected(self, rect: WindowRect, x_ratio: float, y_ratio: float, index: int) -> None:
        if self._is_left_row_selected(rect, y_ratio, checkpoint_name=f"recipient_row_{index}_before"):
            return
        attempts = [
            (x_ratio, y_ratio),
            (x_ratio, y_ratio + 0.010),
            (x_ratio, y_ratio - 0.010),
            (x_ratio, y_ratio + 0.020),
            (x_ratio, y_ratio - 0.020),
            (x_ratio + 0.012, y_ratio),
            (x_ratio - 0.012, y_ratio),
        ]
        for attempt_x, attempt_y in attempts:
            if not self._click_recipient_checkbox(rect, attempt_x, attempt_y):
                continue
            self._sleep(0.25)
            if self._is_left_row_selected(rect, y_ratio, checkpoint_name=f"recipient_row_{index}_after"):
                return
        raise RuntimeError(f"无法确认第 {index} 个接收会话已勾选：y_ratio={y_ratio:.3f}")

    def _click_recipient_checkbox_until_unselected(self, rect: WindowRect, x_ratio: float, y_ratio: float, name: str) -> None:
        if not self._is_left_row_selected(rect, y_ratio, checkpoint_name=f"recipient_uncheck_{name}_before"):
            return
        attempts = [
            (x_ratio, y_ratio),
            (x_ratio, y_ratio + 0.010),
            (x_ratio, y_ratio - 0.010),
            (x_ratio, y_ratio + 0.020),
            (x_ratio, y_ratio - 0.020),
            (x_ratio + 0.012, y_ratio),
            (x_ratio - 0.012, y_ratio),
        ]
        for attempt_x, attempt_y in attempts:
            if not self._click_recipient_checkbox(rect, attempt_x, attempt_y):
                continue
            self._sleep(0.25)
            if not self._is_left_row_selected(rect, y_ratio, checkpoint_name=f"recipient_uncheck_{name}_after"):
                return
        raise RuntimeError(f"无法确认左侧已勾选项已取消：{name}")

    def _click_recipient_checkbox(self, rect: WindowRect, x_ratio: float, y_ratio: float) -> bool:
        click_screen = getattr(self.window, "click_screen", None)
        if callable(click_screen):
            click_x, click_y = rect.relative_point(x_ratio, y_ratio)
            return bool(click_screen(click_x, click_y))
        return bool(self.window.click_relative(rect, x_ratio, y_ratio))

    def _verify_left_selection_count(self, rect: WindowRect, selected: list[SelectedRecipient], expected_count: int) -> bool:
        points = self._left_selected_checkbox_y_ratios(rect, "left_selected_recipients_verify")
        if len(points) != expected_count:
            log.warning("左侧勾选数量复查失败：expected=%s actual=%s selected=%s", expected_count, len(points), selected)
            return False
        for recipient in selected:
            if recipient.y_ratio is None:
                log.warning("左侧勾选复查失败：缺少 y 坐标 selected=%s", selected)
                return False
            if not any(abs(point_y - recipient.y_ratio) <= 0.025 for point_y in points):
                log.warning(
                    "左侧勾选复查失败：目标行未保持勾选 name=%s y=%.3f actual=%s",
                    recipient.name,
                    recipient.y_ratio,
                    [round(point_y, 3) for point_y in points],
                )
                return False
        return True

    def _read_selected_recipients(self, rect: WindowRect, expected_count: int) -> list[SelectedRecipient]:
        region = self._selected_list_region(rect)
        image_path = self.screen.save_checkpoint("selected_recipients_ocr", region=region)
        lines = self.screen.ocr_lines(image_path=image_path)
        if not lines:
            log.warning("右侧已选列表 OCR 未读到文本")
            return []

        row_height = self.config.recipient_selection.sentinel.selected_item_row_height_ratio * rect.height
        recipients: list[SelectedRecipient] = []
        seen_rows: set[int] = set()
        sentinel_names = {self._recipient_match_key(name) for name in self.config.recipient_selection.sentinel.names}
        for line in lines:
            text = self._normalize_recipient_text(line.text)
            text_key = self._recipient_match_key(text)
            if not text_key:
                continue
            ignored_keys = ("分别发送给", "已选择", "已选", "聊天记录", "逐条转发", "合并转发", "转发", "留言")
            if any(key in text_key for key in ignored_keys):
                continue
            row = int(line.center_y / row_height) if row_height > 0 else len(recipients)
            if row in seen_rows and text_key not in sentinel_names:
                continue
            seen_rows.add(row)
            y_ratio = (region.top + line.center_y - rect.top) / rect.height
            recipients.append(SelectedRecipient(text, y_ratio))

        # 右侧"已选"列表显示顺序就是实际从底部往上选中的顺序。
        recipients = sorted(recipients, key=lambda item: item.y_ratio or 0)
        if expected_count and len(recipients) != expected_count:
            log.warning("右侧已选列表识别数量与预期不一致：expected=%s actual=%s items=%s", expected_count, len(recipients), recipients)
        return recipients

    def _normalize_recipient_text(self, text: str) -> str:
        cleaned = " ".join(text.replace("\u3000", " ").split())
        for marker in ("外部", "全员"):
            cleaned = cleaned.replace(marker, "")
        if "(" in cleaned:
            cleaned = cleaned.split("(", 1)[0]
        if "（" in cleaned:
            cleaned = cleaned.split("（", 1)[0]
        return cleaned.strip()

    def _recipient_match_key(self, text: str) -> str:
        return self._normalize_recipient_text(text).replace(" ", "")

    def _remove_selected_recipients(self, rect: WindowRect, recipients: list[SelectedRecipient]) -> None:
        remove_x = self.config.recipient_selection.sentinel.remove_button_x_ratio
        for recipient in sorted(recipients, key=lambda item: item.y_ratio or 0, reverse=True):
            if recipient.y_ratio is None:
                raise RuntimeError(f"无法删除已选项：缺少 y 坐标 name={recipient.name}")
            if not self.window.click_relative(rect, remove_x, recipient.y_ratio):
                raise RuntimeError(f"无法删除已选项：{recipient.name}")
            self._sleep(0.15)

    def _uncheck_left_recipients(self, rect: WindowRect, recipients: list[SelectedRecipient]) -> None:
        checkbox_x = self._recipient_checkbox_x_ratio(rect)
        for recipient in sorted(recipients, key=lambda item: item.y_ratio or 0, reverse=True):
            if recipient.y_ratio is None:
                raise RuntimeError(f"无法取消左侧已勾选项：缺少 y 坐标 name={recipient.name}")
            started = time.monotonic()
            if not self._click_recipient_checkbox(rect, checkbox_x, recipient.y_ratio):
                raise RuntimeError(f"无法取消左侧已勾选项：{recipient.name}")
            self._sleep(0.15)
            log.info(
                "取消哨兵边界会话完成：name=%s ratio=(%.3f, %.3f) elapsed=%.3fs",
                recipient.name,
                checkbox_x,
                recipient.y_ratio,
                time.monotonic() - started,
            )

    def _cancel_recipient_picker(self, rect: WindowRect) -> None:
        started = time.monotonic()
        image_path = self.screen.save_checkpoint(
            "recipient_picker_cancel_before",
            region=Region(rect.left, rect.top, rect.width, rect.height),
        )
        send_keys = getattr(self.window, "send_keys", None)
        closed_by_escape = bool(callable(send_keys) and send_keys("{ESC}"))
        if not closed_by_escape:
            self.window.click_screen(
                rect.left + int(rect.width * 0.863),
                rect.top + int(rect.height * 0.253),
            )
        self._sleep(0.35)
        log.info(
            "关闭收件人弹窗完成：method=%s screenshot=%s elapsed=%.2fs",
            "escape" if closed_by_escape else "fixed_close",
            image_path,
            time.monotonic() - started,
        )

    def _reselect_source_messages(self, rect: WindowRect) -> None:
        """后续批次按第一轮记录的复选框坐标直接点击，不逐条校验避免鼠标遮挡蓝勾检测。"""
        already_selected_y = self._enter_multiselect_from_source(rect)
        for y_ratio in self.source_checkbox_y_ratios:
            if abs(y_ratio - already_selected_y) < 0.04:
                continue
            click_x, click_y = rect.relative_point(self.source_checkbox_x_ratio, y_ratio)
            if not self.window.click_screen(click_x, click_y):
                raise RuntimeError("无法重新勾选待转发消息复选框")
            self._sleep(0.2)
        self._sleep(0.3)
        image_path = self.screen.save_checkpoint("source_messages_reselected", region=None)
        if not self._verify_source_messages_selected(image_path, rect=rect):
            raise RuntimeError("第二轮源消息多选未成功打开或待转发消息未全部勾选")

    def _assert_exact_source_selection(self, rect: WindowRect, checkpoint_name: str) -> None:
        image_path = self.screen.save_checkpoint(checkpoint_name, region=None)
        if not self._verify_source_messages_selected(image_path, rect=rect):
            raise RuntimeError("源消息勾选数量或位置不符合记录，已停止以避免误发")

    def _record_exact_source_selection(self, image_path, rect: WindowRect | None = None) -> None:
        selected_points = self._source_selected_checkbox_ratios_in_window(image_path, rect) if rect is not None else self._source_selected_checkbox_ratios(image_path)
        expected_count = len(self.source_checkbox_y_ratios)
        if len(selected_points) != expected_count and rect is not None:
            fallback_points = self._source_selected_checkbox_ratios_from_fullscreen(rect, "source_fullscreen_probe")
            if fallback_points:
                log.info(
                    "窗口截图源消息数量与检查记录不一致，已使用 DPI-aware 全屏截图复核："
                    "expected=%s crop=%s fullscreen=%s",
                    expected_count,
                    len(selected_points),
                    len(fallback_points),
                )
                selected_points = fallback_points
        if len(selected_points) != expected_count:
            log.warning(
                "源消息数量与检查阶段记录不一致：expected=%s actual=%s points=%s",
                expected_count,
                len(selected_points),
                [(round(x, 3), round(y, 3)) for x, y in selected_points],
            )
            if self.real_send_allowed:
                raise RuntimeError(
                    f"真实发送前源消息蓝色勾选框数量不是检查阶段记录的 {expected_count} 个，已停止以避免误发"
                )
            return
        source_points = sorted(selected_points, key=lambda item: item[1], reverse=True)
        self.source_checkbox_x_ratio = sum(x for x, _ in source_points) / len(source_points)
        self.source_checkbox_y_ratios = [y for _, y in source_points]
        log.info(
            "已记录源消息复选框：count=%s x_ratio=%.3f y_ratios=%s",
            len(source_points),
            self.source_checkbox_x_ratio,
            [round(y, 3) for y in self.source_checkbox_y_ratios],
        )

    def _verify_source_messages_selected(self, image_path, rect: WindowRect | None = None) -> bool:
        points = self._source_selected_checkbox_ratios_in_window(image_path, rect) if rect is not None else self._source_selected_checkbox_ratios(image_path)
        expected_count = len(self.source_checkbox_y_ratios)
        if len(points) != expected_count and rect is not None:
            fallback_points = self._source_selected_checkbox_ratios_from_fullscreen(rect, "source_verify_fullscreen_probe")
            if fallback_points:
                log.info(
                    "窗口裁剪图源消息复查数量不匹配，已使用全屏截图 fallback：crop=%s fullscreen=%s",
                    len(points),
                    len(fallback_points),
                )
                points = fallback_points
        if len(points) != expected_count:
            log.warning(
                "源消息复查失败：蓝色勾选框数量不匹配 expected=%s actual=%s points=%s",
                expected_count,
                len(points),
                [(round(x, 3), round(y, 3)) for x, y in points],
            )
            return False
        x_tolerance = 0.06
        y_tolerance = 0.04
        matched = 0
        for expected_y in self.source_checkbox_y_ratios:
            if any(
                abs(x_ratio - self.source_checkbox_x_ratio) <= x_tolerance and abs(y_ratio - expected_y) <= y_tolerance
                for x_ratio, y_ratio in points
            ):
                matched += 1
        if matched != len(self.source_checkbox_y_ratios):
            log.warning(
                "源消息重选复查失败：expected=%s matched=%s points=%s source_x=%.3f source_y=%s",
                len(self.source_checkbox_y_ratios),
                matched,
                [(round(x, 3), round(y, 3)) for x, y in points],
                self.source_checkbox_x_ratio,
                [round(y, 3) for y in self.source_checkbox_y_ratios],
            )
            return False
        source_points = sorted(points, key=lambda item: item[1], reverse=True)
        self.source_checkbox_x_ratio = sum(x for x, _ in source_points) / len(source_points)
        self.source_checkbox_y_ratios = [y for _, y in source_points]
        log.info(
            "源消息复查通过并更新坐标：x_ratio=%.3f y_ratios=%s",
            self.source_checkbox_x_ratio,
            [round(y, 3) for y in self.source_checkbox_y_ratios],
        )
        return True

    def _source_selected_checkbox_ratios(self, image_path) -> list[tuple[float, float]]:
        raw_points = self.screen.filter_source_checkbox_marker_points(
            image_path,
            list(
                self.screen.find_selected_checkbox_ratios(
                    image_path,
                    scan_region_ratio=(
                        SOURCE_CHECKBOX_COLUMN_MIN_X,
                        0.07,
                        SOURCE_CHECKBOX_COLUMN_MAX_X - SOURCE_CHECKBOX_COLUMN_MIN_X,
                        0.76,
                    ),
                )
            ),
        )
        return self._select_source_checkbox_column(raw_points)

    def _select_source_checkbox_column(
        self,
        points: list[tuple[float, float]],
    ) -> list[tuple[float, float]]:
        return select_aligned_checkbox_column(
            points,
            min_x=SOURCE_CHECKBOX_COLUMN_MIN_X,
            max_x=SOURCE_CHECKBOX_COLUMN_MAX_X,
            min_y=SOURCE_CHECKBOX_COLUMN_MIN_Y,
            max_y=SOURCE_CHECKBOX_COLUMN_MAX_Y,
        )

    def _source_selected_checkbox_ratios_in_window(self, image_path, rect: WindowRect | None) -> list[tuple[float, float]]:
        if rect is None:
            return self._source_selected_checkbox_ratios(image_path)
        size_getter = getattr(self.screen, "image_size", None)
        image_size = size_getter(image_path) if callable(size_getter) else None
        if not image_size:
            return self._source_selected_checkbox_ratios(image_path)
        return self._convert_fullscreen_checkbox_ratios_to_window(image_path, rect, image_size)

    def _source_selected_checkbox_ratios_from_fullscreen(self, rect: WindowRect, checkpoint_name: str) -> list[tuple[float, float]]:
        save_fullscreen = getattr(self.screen, "save_fullscreen_checkpoint", None)
        save_checkpoint = getattr(self.screen, "save_checkpoint", None)
        if callable(save_fullscreen):
            image_path = save_fullscreen(checkpoint_name)
        elif callable(save_checkpoint):
            image_path = save_checkpoint(checkpoint_name, region=None)
        else:
            return []
        size_getter = getattr(self.screen, "image_size", None)
        image_size = size_getter(image_path) if callable(size_getter) else None
        if not image_size:
            return []
        return self._convert_fullscreen_checkbox_ratios_to_window(image_path, rect, image_size)

    def _convert_fullscreen_checkbox_ratios_to_window(
        self,
        image_path,
        rect: WindowRect,
        image_size: tuple[int, int],
    ) -> list[tuple[float, float]]:
        image_width, image_height = image_size
        if image_width <= 0 or image_height <= 0 or rect.width <= 0 or rect.height <= 0:
            return []

        raw_points = self.screen.filter_source_checkbox_marker_points(
            image_path,
            list(self.screen.find_selected_checkbox_ratios(image_path)),
        )

        converted: list[tuple[float, float]] = []
        for x_ratio, y_ratio in raw_points:
            mapped = fullscreen_point_to_window_ratio(
                x_ratio * image_width,
                y_ratio * image_height,
                rect,
                image_size,
                x_range=(SOURCE_CHECKBOX_COLUMN_MIN_X, SOURCE_CHECKBOX_COLUMN_MAX_X),
                y_range=(SOURCE_CHECKBOX_COLUMN_MIN_Y, SOURCE_CHECKBOX_COLUMN_MAX_Y),
            )
            if mapped is not None:
                converted.append(mapped)
        return self._select_source_checkbox_column(converted)

    def _enter_multiselect_from_source(self, rect: WindowRect) -> float:
        if self._source_context_right_click is not None and self._source_multiselect_menu_offset is not None:
            checkbox_y, right_click_x, right_click_y = self._source_context_right_click
            click_x, click_y = rect.relative_point(right_click_x, right_click_y)
            offset_x, offset_y = self._source_multiselect_menu_offset
            log.info(
                "复用源消息多选坐标：right_click_ratio=(%.3f, %.3f) "
                "right_click_abs=(%s, %s) menu_offset=(%s, %s) menu_abs=(%s, %s)",
                right_click_x,
                right_click_y,
                click_x,
                click_y,
                offset_x,
                offset_y,
                click_x + offset_x,
                click_y + offset_y,
            )
            if not self.window.right_click_relative(rect, right_click_x, right_click_y):
                raise RuntimeError("无法在已记录的源消息位置打开右键菜单")
            self._sleep(0.30)
            if not self.window.click_screen(click_x + offset_x, click_y + offset_y):
                raise RuntimeError("无法点击已记录的多选菜单坐标")
            self._sleep(0.50)
            if not self._source_multiselect_opened(rect, "source_multiselect_reused"):
                raise RuntimeError("复用已记录坐标后未检测到源消息多选状态，已停止避免继续误点")
            return checkbox_y

        for checkbox_y, right_click_x, right_click_y in self._source_context_menu_candidates():
            if self.stop.should_stop():
                raise RuntimeError("急停触发，已停止重新选择源消息")
            click_x, click_y = rect.relative_point(right_click_x, right_click_y)
            if not self.window.right_click_relative(rect, right_click_x, right_click_y):
                continue
            self._sleep(0.30)
            menu_region = self._source_context_menu_region(rect, right_click_x, right_click_y)
            menu_shot = self.screen.save_checkpoint(
                f"source_context_menu_{right_click_x:.3f}_{right_click_y:.3f}",
                region=menu_region,
            )
            menu_line = self._find_context_menu_line(menu_shot, "多选")
            if self.stop.should_stop():
                raise RuntimeError("急停触发，已停止重新选择源消息")
            if menu_line is None:
                log.info(
                    "右键候选点局部区域未识别到多选菜单：x=%.3f y=%.3f region=%s",
                    right_click_x,
                    right_click_y,
                    menu_region,
                )
                self.window.send_keys("{ESC}")
                continue
            menu_click_x = menu_region.left + menu_line.left + menu_line.width // 2
            menu_click_y = menu_region.top + menu_line.center_y
            if not self.window.click_screen(menu_click_x, menu_click_y):
                raise RuntimeError("无法点击右键菜单中的多选")
            self._sleep(0.50)
            if not self._source_multiselect_opened(rect, "source_multiselect_opened"):
                raise RuntimeError("已识别并点击多选菜单，但未检测到源消息蓝勾，已停止避免继续误点")
            self._source_context_right_click = (checkbox_y, right_click_x, right_click_y)
            self._source_multiselect_menu_offset = (menu_click_x - click_x, menu_click_y - click_y)
            log.info(
                "已记录源消息多选坐标：right_click_ratio=(%.3f, %.3f) "
                "right_click_abs=(%s, %s) menu_abs=(%s, %s) menu_offset=(%s, %s)",
                right_click_x,
                right_click_y,
                click_x,
                click_y,
                menu_click_x,
                menu_click_y,
                menu_click_x - click_x,
                menu_click_y - click_y,
            )
            return checkbox_y
        raise RuntimeError("无法通过右键菜单进入源消息多选：未识别到“多选”菜单项")

    def _source_context_menu_candidates(self) -> list[tuple[float, float, float]]:
        # 源消息复选框只提供行坐标；发出的消息气泡贴近窗口右侧，保留 0.75 兼容旧布局。
        return [
            (checkbox_y, right_click_x, min(0.90, max(0.06, checkbox_y)))
            for checkbox_y in sorted(self.source_checkbox_y_ratios)
            for right_click_x in SOURCE_CONTEXT_RIGHT_CLICK_X_RATIOS
        ]

    def _source_context_menu_region(
        self,
        rect: WindowRect,
        right_click_x: float,
        right_click_y: float,
    ) -> Region:
        click_x, click_y = rect.relative_point(right_click_x, right_click_y)
        left = max(rect.left, click_x - round(rect.width * 0.24))
        top = max(rect.top, click_y - round(rect.height * 0.25))
        right = min(rect.right, click_x + round(rect.width * 0.12))
        bottom = min(rect.bottom, click_y + round(rect.height * 0.30))
        return Region(left=left, top=top, width=max(1, right - left), height=max(1, bottom - top))

    def _source_multiselect_opened(self, rect: WindowRect, checkpoint_name: str) -> bool:
        opened_shot = self.screen.save_checkpoint(
            checkpoint_name,
            region=Region(rect.left, rect.top, rect.width, rect.height),
        )
        opened_points = self._source_selected_checkbox_ratios(opened_shot)
        log.info(
            "源消息多选状态复核：checkpoint=%s points=%s",
            checkpoint_name,
            [(round(x, 3), round(y, 3)) for x, y in opened_points],
        )
        return bool(opened_points)

    def _find_context_menu_line(self, image_path, text: str):
        try:
            lines = self.screen.ocr_lines(image_path=image_path)
        except Exception as exc:
            log.warning("右键菜单 OCR 失败：%s", exc)
            return None
        target = text.replace(" ", "")
        for line in lines:
            line_text = line.text.replace(" ", "")
            if target not in line_text:
                continue
            if len(line_text) <= len(target) + 2:
                return line
        return None

    def _has_ocr_text(self, image_path, texts: tuple[str, ...]) -> bool:
        return self._find_ocr_text_line(image_path, texts) is not None

    def _find_ocr_text_line(self, image_path, texts: tuple[str, ...]):
        try:
            lines = self.screen.ocr_lines(image_path=image_path)
        except Exception as exc:
            log.warning("OCR 文本查找失败：%s", exc)
            return None
        targets = tuple(text.replace(" ", "") for text in texts)
        for line in lines:
            line_text = line.text.replace(" ", "")
            if any(target in line_text for target in targets):
                return line
        return None
