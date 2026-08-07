import threading
import time
import types
import unittest
from unittest import mock

from wecom_rpa.alerts import show_error_dialog


class ErrorDialogAlertTest(unittest.TestCase):
    def test_windows_warning_sound_loops_until_dialog_is_closed(self):
        beep_calls: list[int] = []
        stop_calls: list[tuple[object, int]] = []
        repeated = threading.Event()

        def message_beep(sound_type):
            beep_calls.append(sound_type)
            if len(beep_calls) >= 2:
                repeated.set()

        fake_winsound = types.SimpleNamespace(
            MB_ICONEXCLAMATION=48,
            MessageBeep=message_beep,
            PlaySound=lambda sound, flags: stop_calls.append((sound, flags)),
        )

        def close_dialog(*_args, **_kwargs):
            self.assertEqual(beep_calls, [48])
            self.assertTrue(repeated.wait(timeout=1.0))
            return "ok"

        with (
            mock.patch("wecom_rpa.alerts.os.name", "nt"),
            mock.patch("wecom_rpa.alerts._WARNING_REPEAT_INTERVAL_SEC", 0.01),
            mock.patch.dict("sys.modules", {"winsound": fake_winsound}),
            mock.patch("tkinter.messagebox.showerror", side_effect=close_dialog) as showerror,
        ):
            result = show_error_dialog("运行失败", "测试错误")

        self.assertEqual(result, "ok")
        showerror.assert_called_once_with("运行失败", "测试错误")
        self.assertGreaterEqual(len(beep_calls), 2)
        self.assertEqual(stop_calls, [(None, 0)])

        count_after_close = len(beep_calls)
        time.sleep(0.03)
        self.assertEqual(len(beep_calls), count_after_close)

    def test_warning_sound_is_stopped_if_dialog_raises(self):
        beep_calls: list[int] = []
        stop_calls: list[tuple[object, int]] = []
        fake_winsound = types.SimpleNamespace(
            MB_ICONEXCLAMATION=48,
            MessageBeep=lambda sound_type: beep_calls.append(sound_type),
            PlaySound=lambda sound, flags: stop_calls.append((sound, flags)),
        )

        with (
            mock.patch("wecom_rpa.alerts.os.name", "nt"),
            mock.patch.dict("sys.modules", {"winsound": fake_winsound}),
            mock.patch("tkinter.messagebox.showerror", side_effect=RuntimeError("dialog failed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "dialog failed"):
                show_error_dialog("运行失败", "测试错误")

        self.assertEqual(beep_calls, [48])
        self.assertEqual(stop_calls, [(None, 0)])

    def test_non_windows_error_dialog_does_not_use_winsound(self):
        with (
            mock.patch("wecom_rpa.alerts.os.name", "posix"),
            mock.patch("tkinter.messagebox.showerror", return_value="ok") as showerror,
        ):
            result = show_error_dialog("检查失败", "测试错误")

        self.assertEqual(result, "ok")
        showerror.assert_called_once_with("检查失败", "测试错误")


if __name__ == "__main__":
    unittest.main()
