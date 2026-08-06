import types
import unittest
from unittest import mock

from wecom_rpa.alerts import show_error_dialog


class ErrorDialogAlertTest(unittest.TestCase):
    def test_windows_warning_sound_loops_until_dialog_is_closed(self):
        calls: list[tuple[object, int]] = []
        fake_winsound = types.SimpleNamespace(
            SND_ALIAS=1,
            SND_ASYNC=2,
            SND_LOOP=4,
            PlaySound=lambda sound, flags: calls.append((sound, flags)),
        )

        def close_dialog(*_args, **_kwargs):
            self.assertEqual(calls, [("SystemExclamation", 7)])
            return "ok"

        with (
            mock.patch("wecom_rpa.alerts.os.name", "nt"),
            mock.patch.dict("sys.modules", {"winsound": fake_winsound}),
            mock.patch("tkinter.messagebox.showerror", side_effect=close_dialog) as showerror,
        ):
            result = show_error_dialog("运行失败", "测试错误")

        self.assertEqual(result, "ok")
        showerror.assert_called_once_with("运行失败", "测试错误")
        self.assertEqual(calls, [("SystemExclamation", 7), (None, 0)])

    def test_warning_sound_is_stopped_if_dialog_raises(self):
        calls: list[tuple[object, int]] = []
        fake_winsound = types.SimpleNamespace(
            SND_ALIAS=1,
            SND_ASYNC=2,
            SND_LOOP=4,
            PlaySound=lambda sound, flags: calls.append((sound, flags)),
        )

        with (
            mock.patch("wecom_rpa.alerts.os.name", "nt"),
            mock.patch.dict("sys.modules", {"winsound": fake_winsound}),
            mock.patch("tkinter.messagebox.showerror", side_effect=RuntimeError("dialog failed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "dialog failed"):
                show_error_dialog("运行失败", "测试错误")

        self.assertEqual(calls[-1], (None, 0))

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
