from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)


@dataclass
class StopController:
    hotkey: str
    _stop_requested: bool = False
    _keyboard: object | None = field(default=None, init=False, repr=False)
    _registered: bool = field(default=False, init=False)
    _callbacks: list[Callable[[], None]] = field(default_factory=list, init=False, repr=False)

    def install(self) -> None:
        """注册全局急停热键；依赖不可用时安全降级为手动标记。"""
        try:
            import keyboard  # type: ignore
        except Exception as exc:
            log.info("急停热键未注册：keyboard 依赖不可用。hotkey=%s reason=%s", self.hotkey, exc)
            return

        try:
            keyboard.add_hotkey(self.hotkey, self.request_stop)
        except Exception as exc:
            log.warning("急停热键注册失败：%s reason=%s", self.hotkey, exc)
            return
        self._keyboard = keyboard
        self._registered = True
        log.info("急停热键已注册：%s", self.hotkey)

    def uninstall(self) -> None:
        if not self._registered or self._keyboard is None:
            return
        try:
            self._keyboard.remove_hotkey(self.hotkey)  # type: ignore[attr-defined]
        except Exception as exc:
            log.debug("移除急停热键失败：%s", exc)
        finally:
            self._registered = False
            self._keyboard = None

    def request_stop(self) -> None:
        self._stop_requested = True
        log.warning("收到急停请求")
        for callback in list(self._callbacks):
            try:
                callback()
            except Exception as exc:
                log.debug("急停回调执行失败：%s", exc)

    def should_stop(self) -> bool:
        return self._stop_requested

    def reset(self) -> None:
        self._stop_requested = False

    def add_callback(self, callback: Callable[[], None]) -> None:
        self._callbacks.append(callback)


class SendLimitError(ValueError):
    pass


def assert_batch_selection_count(selected_count: int, batch_size: int, *, hard_limit: int = 9) -> None:
    if selected_count < 0:
        raise SendLimitError("selected_count 不能为负数")
    if batch_size <= 0 or batch_size > hard_limit:
        raise SendLimitError(f"batch_size 必须在 1..{hard_limit} 之间")
    if selected_count > batch_size:
        raise SendLimitError(f"批次选择数量异常：{selected_count} > batch_size({batch_size})")
    if selected_count > hard_limit:
        raise SendLimitError(f"批次选择数量异常：{selected_count} > {hard_limit}")
