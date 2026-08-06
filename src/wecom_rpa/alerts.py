from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


def _noop() -> None:
    pass


def _start_windows_warning_sound() -> Callable[[], None]:
    """Start a looping Windows warning sound and return a function that stops it."""
    if os.name != "nt":
        return _noop

    try:
        import winsound

        flags = winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_LOOP
        winsound.PlaySound("SystemExclamation", flags)
    except Exception as exc:
        log.debug("启动 Windows 报错警告音失败：%s", exc)
        return _noop

    def stop() -> None:
        try:
            winsound.PlaySound(None, 0)
        except Exception as exc:
            log.debug("停止 Windows 报错警告音失败：%s", exc)

    return stop


def show_error_dialog(title: str, message: str, **options: Any) -> str:
    """Show an error dialog, sounding an alarm until it is closed on Windows."""
    from tkinter import messagebox

    stop_warning_sound = _start_windows_warning_sound()
    try:
        return messagebox.showerror(title, message, **options)
    finally:
        stop_warning_sound()
