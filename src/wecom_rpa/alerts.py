from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

_WARNING_REPEAT_INTERVAL_SEC = 1.0


def _noop() -> None:
    pass


def _start_windows_warning_sound() -> Callable[[], None]:
    """Repeatedly play the Windows warning sound until the returned function is called."""
    if os.name != "nt":
        return _noop

    try:
        import winsound
    except Exception as exc:
        log.debug("加载 Windows 报错警告音失败：%s", exc)
        return _noop

    stopped = threading.Event()

    def play_once() -> None:
        try:
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception as exc:
            log.debug("播放 Windows 报错警告音失败：%s", exc)

    def repeat() -> None:
        while not stopped.wait(_WARNING_REPEAT_INTERVAL_SEC):
            play_once()

    # Play once before the dialog opens, then actively replay it. Some Windows
    # sound schemes do not reliably honor SND_LOOP for a system sound alias.
    play_once()
    warning_thread = threading.Thread(
        target=repeat,
        name="wecom-rpa-warning-sound",
        daemon=True,
    )
    warning_thread.start()

    def stop() -> None:
        stopped.set()
        warning_thread.join(timeout=0.5)
        try:
            # Stop a warning waveform that may still be playing when the dialog closes.
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
