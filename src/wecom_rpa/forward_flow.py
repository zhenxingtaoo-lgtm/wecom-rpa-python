from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from .config import AppConfig
from .groups import split_batches
from .models import TargetGroup, TargetStatus
from .safety import StopController, assert_batch_selection_count
from .screen import Region, ScreenInspector
from .storage import StateStore
from .wecom_window import WeComWindow, WindowRect

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FlowResult:
    run_id: int
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


class ForwardFlow:
    def __init__(
        self,
        config: AppConfig,
        store: StateStore,
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
        self.store = store
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
            time.sleep(min(0.1, deadline - time.monotonic()))

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

    def run(self, groups: list[TargetGroup]) -> FlowResult:
        self.store.upsert_targets(groups)
        run_id = self.store.start_run(self.config)
        if self.install_stop_hotkey:
            self.stop.install()
        status = "completed"
        try:
            self._emit_progress("run_started", total_targets=len(groups), dry_run=self.config.dry_run)
            groups = self._resume_targets(groups)
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
            else:
                log.info("收件人选择策略：search_by_name；按 CSV 群名逐个搜索选择")

            batches = split_batches(groups, self.config.batch_size) if groups else []
            for batch_index, batch in enumerate(batches):
                if self.stop.should_stop():
                    status = "stopped"
                    break
                if batch.batch_no == 1 and self.config.require_confirm_first_batch:
                    self._confirm("第一批开始前确认。dry-run 不会真实发送。")
                log.info("开始处理批次 %s，目标数=%s", batch.batch_no, len(batch.targets))
                self._run_batch(batch.batch_no, batch.targets, total_batches=len(batches))
                if self.boundary_reached:
                    for remaining_batch in batches[batch_index + 1 :]:
                        for target in remaining_batch.targets:
                            self.store.set_status(target.group_name, TargetStatus.SKIPPED, batch_no=batch.batch_no, error="哨兵边界已到达")
                    log.info("检测到哨兵边界，尾批处理完成后结束任务")
                    break
                if batch.batch_no < len(batches) and self.config.batch_interval_sec > 0:
                    self._sleep(self.config.batch_interval_sec)
        except Exception as exc:
            status = "failed"
            log.exception("流程失败：%s", exc)
            self.screen.save_error("flow_failed", str(exc))
            raise
        finally:
            if self.install_stop_hotkey:
                self.stop.uninstall()
            self.store.finish_run(run_id, status)
        result = FlowResult(run_id=run_id, status=status, summary=self.store.summary())
        self._emit_progress("run_finished", run_id=run_id, status=status, summary=result.summary)
        return result

    def _resume_targets(self, groups: list[TargetGroup]) -> list[TargetGroup]:
        statuses = self.store.get_statuses([group.group_name for group in groups])
        uncertain = [name for name, target_status in statuses.items() if target_status == TargetStatus.UNCERTAIN]
        if uncertain:
            raise RuntimeError(f"存在 uncertain 目标，需人工确认后再续跑：{uncertain}")
        completed = {TargetStatus.SENT, TargetStatus.SKIPPED}
        runnable = [group for group in groups if statuses.get(group.group_name) not in completed]
        skipped = len(groups) - len(runnable)
        if skipped:
            log.info("断点续跑：跳过已完成目标 %s 个，继续处理 %s 个", skipped, len(runnable))
        return runnable

    def _run_batch(self, batch_no: int, targets: list[TargetGroup], *, total_batches: int | None = None) -> None:
        self._emit_progress(
            "batch_started",
            batch_no=batch_no,
            total_batches=total_batches,
            target_count=len(targets),
            summary=self.store.summary(),
        )
        self.screen.save_checkpoint(f"batch_{batch_no}_before")
        if self.config.recipient_selection.mode == "bottom_of_picker":
            if self.config.dry_run:
                self._run_bottom_picker_batch(batch_no, targets)
            else:
                self._run_real_bottom_picker_batch(batch_no, targets)
        else:
            self._run_search_by_name_batch(batch_no, targets)
        self.screen.save_checkpoint(f"batch_{batch_no}_after")
        self._emit_progress(
            "batch_finished",
            batch_no=batch_no,
            total_batches=total_batches,
            summary=self.store.summary(),
        )

    def _run_bottom_picker_batch(self, batch_no: int, targets: list[TargetGroup]) -> None:
        """新方案 dry-run：不搜索群名，模拟在选择聊天弹窗底部连续勾选。

        真实实现时这里会：打开"选择聊天/发送给"弹窗 -> 滚到底部 -> 从底部往上勾选
        最多 batch_size 个会话 -> 发送。用户已确认员工/机器人也允许被选中，所以此策略
        不做群类型过滤，也不依赖群名 OCR。
        """
        selected_count = 0
        repeats = self.config.recipient_selection.scroll_to_bottom_repeats
        log.info("[dry-run] 批次 %s：假定已打开选择聊天弹窗，滚动到底部 repeats=%s", batch_no, repeats)
        log.info("[dry-run] 批次 %s：从弹窗底部连续选择 %s 个会话（不区分客户群/员工/机器人）", batch_no, len(targets))
        effective_targets = targets
        sentinel = self.config.recipient_selection.sentinel
        if sentinel.enabled:
            selected = [SelectedRecipient(target.group_name) for target in targets]
            trim = self._build_sentinel_trim_plan(selected)
            if trim.boundary_reached:
                self.boundary_reached = True
                effective_names = {item.name for item in trim.send}
                removed_names = {item.name for item in trim.remove}
                effective_targets = [target for target in targets if target.group_name in effective_names]
                for target in targets:
                    if target.group_name in removed_names:
                        self.store.set_status(target.group_name, TargetStatus.SKIPPED, batch_no=batch_no, error="dry-run：哨兵边界截断")
                log.info(
                    "[dry-run] 批次 %s 命中哨兵边界：保留=%s 移除=%s",
                    batch_no,
                    [item.name for item in trim.send],
                    [item.name for item in trim.remove],
                )

        for index, target in enumerate(effective_targets, start=1):
            if self.stop.should_stop():
                self.store.set_status(target.group_name, TargetStatus.SKIPPED, batch_no=batch_no, error="急停触发")
                continue
            current = self.store.get_status(target.group_name)
            if current in {TargetStatus.SENT, TargetStatus.SKIPPED}:
                log.info("跳过已完成目标：%s status=%s", target.group_name, current)
                continue
            log.info("[dry-run] 模拟勾选底部第 %s 个会话，占位记录=%s", index, target.group_name)
            self.store.set_status(target.group_name, TargetStatus.SELECTED, batch_no=batch_no)
            selected_count += 1

        assert_batch_selection_count(selected_count, self.config.batch_size)
        log.info("[dry-run] 批次 %s 已模拟从底部选择 %s 个会话；最终发送被跳过", batch_no, selected_count)
        for target in effective_targets:
            if self.store.get_status(target.group_name) == TargetStatus.SELECTED:
                self.store.set_status(target.group_name, TargetStatus.SKIPPED, batch_no=batch_no, error="dry-run：未点击最终发送")

    def _run_real_bottom_picker_batch(self, batch_no: int, targets: list[TargetGroup]) -> None:
        """实机方案：使用已校准的相对坐标完成一次底部会话批量发送。"""
        rect = self.window.locate()
        if rect is None:
            raise RuntimeError("真实发送需要先找到企业微信窗口")

        selected_count = len(targets)
        assert_batch_selection_count(selected_count, self.config.batch_size)
        log.warning("真实发送批次 %s：即将发送给 %s 个会话", batch_no, selected_count)

        if batch_no > 1:
            self._reselect_source_messages(rect)
        self._assert_exact_source_selection(rect, f"batch_{batch_no}_source_before_forward")

        self._open_recipient_picker_from_source(rect, batch_no)

        picker_open = True
        try:
            # 接收方选择弹窗左侧列表滚到底部。
            for _ in range(self.config.recipient_selection.scroll_to_bottom_repeats):
                self.window.mouse_wheel_relative(rect, 0.493, 0.646, -1200)
                self._sleep(0.15)

            target_checkbox_x = self._recipient_checkbox_x_ratio(rect)
            target_checkbox_y = self._recipient_checkbox_rows_bottom_to_top(selected_count, rect)
            for index, y_ratio in enumerate(target_checkbox_y):
                if self.stop.should_stop():
                    raise RuntimeError("急停触发")
                self._click_recipient_checkbox_until_selected(rect, target_checkbox_x, y_ratio, index + 1)

            self.screen.save_checkpoint(f"batch_{batch_no}_recipients_selected", region=Region(rect.left, rect.top, rect.width, rect.height))

            sentinel = self.config.recipient_selection.sentinel
            effective_targets = targets
            if sentinel.enabled:
                selected = self._read_ordered_selected_recipients(rect, target_checkbox_y, selected_count)
                if not selected:
                    if sentinel.stop_on_detection_failure:
                        raise RuntimeError("已启用哨兵边界，但左侧已勾选会话识别失败；为避免误发已停止")
                else:
                    if len(selected) != selected_count:
                        log.warning(
                            "左侧可见已勾选会话数量与预期不一致：expected=%s actual=%s",
                            selected_count,
                            len(selected),
                        )
                        if sentinel.stop_on_detection_failure:
                            self._mark_targets_uncertain(targets, batch_no, "左侧已勾选会话数量与预期不一致")
                            raise RuntimeError(
                                f"左侧已勾选会话数量与预期不一致：expected={selected_count} actual={len(selected)}；为避免误发已停止"
                            )
                    trim = self._build_sentinel_trim_plan(selected)
                    if trim.boundary_reached:
                        self.boundary_reached = True
                        if len(trim.send) == 0:
                            self._cancel_recipient_picker(rect)
                            picker_open = False
                            for target in targets:
                                self.store.set_status(target.group_name, TargetStatus.SKIPPED, batch_no=batch_no, error="哨兵边界截断：无可发送项")
                            return
                        self._uncheck_left_recipients(rect, trim.remove)
                        if sentinel.stop_on_detection_failure and not self._verify_left_selection_count(rect, trim.send, len(trim.send)):
                            self._mark_targets_uncertain(targets, batch_no, "哨兵截断后左侧勾选数量复查失败")
                            raise RuntimeError(
                                f"哨兵截断后左侧勾选数量复查失败：expected={len(trim.send)}；为避免误发已停止"
                            )
                        effective_targets = targets[: len(trim.send)]
                        for target in targets[len(trim.send) :]:
                            self.store.set_status(target.group_name, TargetStatus.SKIPPED, batch_no=batch_no, error="哨兵边界截断")
                        self.screen.save_checkpoint(f"batch_{batch_no}_recipients_trimmed", region=Region(rect.left, rect.top, rect.width, rect.height))
            for target in effective_targets:
                self.store.set_status(target.group_name, TargetStatus.SELECTED, batch_no=batch_no)

            # 弹窗右下角发送按钮。
            if not self._click_final_send_button(rect):
                raise RuntimeError("无法点击发送按钮")
            self._sleep(2.0)
            evidence = self.screen.save_checkpoint(f"batch_{batch_no}_post_send", region=Region(rect.left, rect.top, rect.width, rect.height))
            if evidence.suffix.lower() != ".png":
                self._mark_targets_uncertain(effective_targets, batch_no, "发送后截图证据不可用")
                raise RuntimeError("发送后截图证据不可用，已标记 uncertain，需人工确认后再续跑")
            if self._has_ocr_text(evidence, ("发送给", "分别发送给")):
                self._mark_targets_uncertain(effective_targets, batch_no, "发送后仍检测到收件人弹窗")
                raise RuntimeError("发送后仍检测到收件人弹窗，已标记 uncertain，需人工确认后再续跑")
            picker_open = False
            for target in effective_targets:
                self.store.set_status(target.group_name, TargetStatus.SENT, batch_no=batch_no)
        except Exception:
            if picker_open:
                self._cancel_recipient_picker(rect)
            raise

    def _mark_targets_uncertain(self, targets: list[TargetGroup], batch_no: int, error: str) -> None:
        for target in targets:
            self.store.set_status(target.group_name, TargetStatus.UNCERTAIN, batch_no=batch_no, error=error)

    def _open_recipient_picker_from_source(self, rect: WindowRect, batch_no: int) -> None:
        # 多选工具栏里的"转发"按钮点击后可能弹出子菜单（逐条转发/合并转发）。
        # 用键盘导航选中"逐条转发"比坐标点选更可靠。
        forward_x, forward_y = self.config.source_selection.forward_button_ratio
        if not self.window.click_relative(rect, forward_x, forward_y):
            raise RuntimeError("无法点击转发按钮")
        self._sleep(0.4)
        # 下拉菜单中"逐条转发"通常是第一项，按 Enter 即可选中。
        self.window.send_keys("{ENTER}")
        self._sleep(1.0)
        picker_shot = self.screen.save_checkpoint(f"batch_{batch_no}_recipient_picker_opened", region=Region(rect.left, rect.top, rect.width, rect.height))
        if not self._has_ocr_text(picker_shot, ("发送给", "分别发送给")):
            raise RuntimeError("点击逐条转发后未识别到发送给弹窗，已停止以避免误点会话列表")

    def _recipient_checkbox_x_ratio(self, rect: WindowRect) -> float:
        if rect.width >= 1600:
            return 0.400
        return 0.260

    def _recipient_send_button_ratios(self, rect: WindowRect) -> tuple[float, float]:
        if rect.width >= 1600:
            return (0.695, 0.959)
        return (0.574, 0.801)

    def _click_final_send_button(self, rect: WindowRect) -> bool:
        send_x, send_y = self._recipient_send_button_ratios(rect)
        click_x, click_y = rect.relative_point(send_x, send_y)
        click_screen = getattr(self.window, "click_screen", None)
        if callable(click_screen):
            return bool(click_screen(click_x, click_y))
        return bool(self.window.click_relative(rect, send_x, send_y))

    def _recipient_checkbox_rows_bottom_to_top(self, selected_count: int, rect: WindowRect | None = None) -> list[float]:
        if selected_count <= 0:
            return []
        if rect is not None and rect.width >= 1600:
            checkbox_y_top_to_bottom = [0.424, 0.493, 0.562, 0.633, 0.704, 0.774, 0.842, 0.911, 0.980]
        else:
            checkbox_y_top_to_bottom = [0.319, 0.382, 0.445, 0.507, 0.569, 0.632, 0.694, 0.757, 0.819]
        if selected_count < self.config.batch_size:
            rows = checkbox_y_top_to_bottom[-selected_count:]
        else:
            rows = checkbox_y_top_to_bottom[:selected_count]
        return list(reversed(rows))

    def _build_sentinel_trim_plan(self, selected_bottom_to_top: list[SelectedRecipient]) -> SentinelTrimPlan:
        sentinel_names = {self._recipient_match_key(name) for name in self.config.recipient_selection.sentinel.names}
        # 合并 OCR 重复检测（相邻行 Y 差 < 2% 且名称匹配键相同视为同一条）。
        deduped: list[SelectedRecipient] = []
        for item in selected_bottom_to_top:
            if deduped and item.y_ratio is not None and deduped[-1].y_ratio is not None:
                if abs(item.y_ratio - deduped[-1].y_ratio) < 0.020:
                    if self._recipient_match_key(item.name) == self._recipient_match_key(deduped[-1].name):
                        continue
            deduped.append(item)
        first_sentinel_index = next(
            (index for index, item in enumerate(deduped) if self._recipient_match_key(item.name) in sentinel_names),
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
        sentinel_names = {self._recipient_match_key(name) for name in self.config.recipient_selection.sentinel.names}
        return any(self._recipient_match_key(item.name) in sentinel_names for item in selected)

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
                left=rect.left + round(rect.width * 0.340),
                top=rect.top + round(rect.height * 0.245),
                width=round(rect.width * 0.290),
                height=round(rect.height * 0.755),
            )
        return Region(
            left=rect.left + round(rect.width * 0.220),
            top=rect.top + round(rect.height * 0.200),
            width=round(rect.width * 0.420),
            height=round(rect.height * 0.790),
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

        if len(recipients) != len(row_y_ratios):
            log.warning(
                "左侧已勾选会话识别数量与预期不一致：expected=%s actual=%s items=%s",
                len(row_y_ratios),
                len(recipients),
                recipients,
            )
        return recipients

    def _left_selected_checkbox_y_ratios(self, rect: WindowRect, checkpoint_name: str) -> list[float]:
        region = self._left_candidate_region(rect)
        image_path = self.screen.save_checkpoint(checkpoint_name, region=region)
        checkbox_points = self._left_checkbox_points(rect, region, image_path)
        return sorted(
            [
                (region.top + round(region.height * y_ratio) - rect.top) / rect.height
                for _x_ratio, y_ratio in checkbox_points
            ],
            reverse=True,
        )

    def _left_checkbox_points(self, rect: WindowRect, region: Region, image_path) -> list[tuple[float, float]]:
        checkbox_x = self._recipient_checkbox_x_ratio(rect)
        expected_local_x = ((rect.left + rect.width * checkbox_x) - region.left) / region.width
        return [
            point
            for point in self.screen.find_selected_checkbox_ratios(image_path)
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
            self._click_recipient_checkbox_until_unselected(rect, checkbox_x, recipient.y_ratio, recipient.name)

    def _cancel_recipient_picker(self, rect: WindowRect) -> None:
        image_path = self.screen.save_checkpoint("recipient_picker_cancel_before", region=Region(rect.left, rect.top, rect.width, rect.height))
        cancel_line = self._find_ocr_text_line(image_path, ("取消",))
        if cancel_line is not None:
            self.window.click_screen(rect.left + cancel_line.left + cancel_line.width // 2, rect.top + cancel_line.center_y)
        else:
            send_keys = getattr(self.window, "send_keys", None)
            if not callable(send_keys) or not send_keys("{ESC}"):
                self.window.click_screen(rect.left + int(rect.width * 0.863), rect.top + int(rect.height * 0.253))
        self._sleep(0.5)

    def _reselect_source_messages(self, rect: WindowRect) -> None:
        """后续批次只通过复选框列重选源消息，避免误触消息正文里的链接。"""
        already_selected_y = self._enter_multiselect_from_source(rect)
        for y_ratio in self.source_checkbox_y_ratios:
            if abs(y_ratio - already_selected_y) < 0.04:
                continue
            click_x, click_y = rect.relative_point(self.source_checkbox_x_ratio, y_ratio)
            if not self.window.click_screen(click_x, click_y):
                raise RuntimeError("无法重新勾选待转发消息复选框")
            self._sleep(0.2)
        image_path = self.screen.save_checkpoint("source_messages_reselected", region=None)
        if not self._verify_source_messages_selected(image_path, rect=rect):
            raise RuntimeError("第二轮源消息多选未成功打开或待转发消息未全部勾选")

    def _assert_exact_source_selection(self, rect: WindowRect, checkpoint_name: str) -> None:
        image_path = self.screen.save_checkpoint(checkpoint_name, region=None)
        if not self._verify_source_messages_selected(image_path, rect=rect):
            raise RuntimeError("源消息勾选数量或位置不符合记录，已停止以避免误发")

    def _record_exact_source_selection(self, image_path, rect: WindowRect | None = None) -> None:
        selected_points = self._source_selected_checkbox_ratios(image_path)
        expected_count = len(self.source_checkbox_y_ratios)
        if len(selected_points) != expected_count and rect is not None:
            fallback_points = self._source_selected_checkbox_ratios_from_fullscreen(rect, "source_fullscreen_probe")
            if fallback_points:
                log.info(
                    "窗口裁剪图源消息蓝勾数量不匹配，已使用全屏截图 fallback：crop=%s fullscreen=%s",
                    len(selected_points),
                    len(fallback_points),
                )
                selected_points = fallback_points
        if len(selected_points) != expected_count:
            log.warning(
                "源消息蓝色勾选框数量不等于配置数量：expected=%s actual=%s points=%s",
                expected_count,
                len(selected_points),
                [(round(x, 3), round(y, 3)) for x, y in selected_points],
            )
            if self.real_send_allowed:
                raise RuntimeError(f"真实发送前源消息蓝色勾选框数量不是 {expected_count} 个，已停止以避免误发")
            return
        source_points = sorted(selected_points, key=lambda item: item[1], reverse=True)
        self.source_checkbox_x_ratio = sum(x for x, _ in source_points) / len(source_points)
        self.source_checkbox_y_ratios = [y for _, y in source_points]
        log.info(
            "已记录源消息复选框：x_ratio=%.3f y_ratios=%s",
            self.source_checkbox_x_ratio,
            [round(y, 3) for y in self.source_checkbox_y_ratios],
        )

    def _verify_source_messages_selected(self, image_path, rect: WindowRect | None = None) -> bool:
        points = self._source_selected_checkbox_ratios(image_path)
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
        return [
            (x_ratio, y_ratio)
            for x_ratio, y_ratio in self.screen.find_selected_checkbox_ratios(image_path)
            if 0.25 <= x_ratio <= 0.50 and y_ratio >= 0.20
        ]

    def _source_selected_checkbox_ratios_from_fullscreen(self, rect: WindowRect, checkpoint_name: str) -> list[tuple[float, float]]:
        image_path = self.screen.save_checkpoint(checkpoint_name, region=None)
        size_getter = getattr(self.screen, "image_size", None)
        image_size = size_getter(image_path) if callable(size_getter) else None
        if not image_size:
            return []
        image_width, image_height = image_size
        if image_width <= 0 or image_height <= 0 or rect.width <= 0 or rect.height <= 0:
            return []

        converted: list[tuple[float, float]] = []
        for x_ratio, y_ratio in self.screen.find_selected_checkbox_ratios(image_path):
            abs_x = x_ratio * image_width
            abs_y = y_ratio * image_height
            if not (rect.left <= abs_x <= rect.right and rect.top <= abs_y <= rect.bottom):
                continue
            local_x = (abs_x - rect.left) / rect.width
            local_y = (abs_y - rect.top) / rect.height
            if 0.25 <= local_x <= 0.50 and local_y >= 0.20:
                converted.append((local_x, local_y))
        return converted

    def _enter_multiselect_from_source(self, rect: WindowRect) -> float:
        for checkbox_y, right_click_x, right_click_y in self._source_context_menu_candidates():
            current_rect = self.window.locate() or rect
            if not self.window.right_click_relative(current_rect, right_click_x, right_click_y):
                continue
            self._sleep(0.45)
            menu_shot = self.screen.save_checkpoint(
                f"source_context_menu_{right_click_x:.3f}_{right_click_y:.3f}",
                region=Region(current_rect.left, current_rect.top, current_rect.width, current_rect.height),
            )
            menu_line = self._find_context_menu_line(menu_shot, "多选")
            if menu_line is None:
                log.info("右键候选点未识别到多选菜单：x=%.3f y=%.3f", right_click_x, right_click_y)
                continue
            click_x = current_rect.left + menu_line.left + menu_line.width // 2
            click_y = current_rect.top + menu_line.center_y
            if not self.window.click_screen(click_x, click_y):
                raise RuntimeError("无法点击右键菜单中的多选")
            self._sleep(0.6)
            opened_shot = self.screen.save_checkpoint(
                "source_multiselect_opened",
                region=None,
            )
            opened_points = self._source_selected_checkbox_ratios(opened_shot)
            if not opened_points:
                log.info("已点击多选菜单但未检测到源消息蓝勾，继续尝试其他右键点：x=%.3f y=%.3f", right_click_x, right_click_y)
                continue
            return checkbox_y
        raise RuntimeError("无法通过右键菜单进入源消息多选：未识别到“多选”菜单项")

    def _source_context_menu_candidates(self) -> list[tuple[float, float, float]]:
        candidates: list[tuple[float, float, float]] = []
        # 普通聊天态下，右侧消息气泡比多选态蓝勾更靠上；优先点气泡左侧留白，
        # 避免点到链接正文导致打开网页。
        for checkbox_y in sorted(self.source_checkbox_y_ratios):
            for y_offset in (0.125, 0.10, 0.15, 0.18, 0.22):
                right_click_y = min(0.86, max(0.25, checkbox_y - y_offset))
                for right_click_x in (0.65, 0.68, 0.70, 0.75):
                    candidates.append((checkbox_y, right_click_x, right_click_y))
        return candidates

    def _find_context_menu_line(self, image_path, text: str):
        try:
            lines = self.screen.ocr_lines(image_path=image_path)
        except Exception as exc:
            log.warning("右键菜单 OCR 失败：%s", exc)
            return None
        image_width = None
        try:
            from PIL import Image

            with Image.open(image_path) as image:
                image_width = image.width
        except Exception:
            image_width = None
        target = text.replace(" ", "")
        for line in lines:
            line_text = line.text.replace(" ", "")
            if target not in line_text:
                continue
            if image_width is not None:
                # PaddleOCR may read the menu item as "三多选"; keep the match
                # constrained to the short right-side context menu to avoid
                # matching unrelated visible text in another foreground window.
                is_right_menu_text = line.left >= image_width * 0.70 and line.width <= image_width * 0.18
                if not is_right_menu_text:
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

    def _run_search_by_name_batch(self, batch_no: int, targets: list[TargetGroup]) -> None:
        """旧方案 dry-run：按 CSV 群名逐个搜索选择。"""
        selected_count = 0
        for target in targets:
            if self.stop.should_stop():
                self.store.set_status(target.group_name, TargetStatus.SKIPPED, batch_no=batch_no, error="急停触发")
                continue
            current = self.store.get_status(target.group_name)
            if current in {TargetStatus.SENT, TargetStatus.SKIPPED}:
                log.info("跳过已完成目标：%s status=%s", target.group_name, current)
                continue
            log.info("[dry-run] 模拟搜索并选择群聊：%s", target.group_name)
            self.store.set_status(target.group_name, TargetStatus.SELECTED, batch_no=batch_no)
            selected_count += 1

        assert_batch_selection_count(selected_count, self.config.batch_size)
        log.info("[dry-run] 批次 %s 已模拟选择 %s 个群；最终发送被跳过", batch_no, selected_count)
        for target in targets:
            if self.store.get_status(target.group_name) == TargetStatus.SELECTED:
                self.store.set_status(target.group_name, TargetStatus.SKIPPED, batch_no=batch_no, error="dry-run：未点击最终发送")
