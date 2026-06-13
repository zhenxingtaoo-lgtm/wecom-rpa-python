from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from .powershell import decode_process_output, powershell_exe, run_powershell

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WindowRect:
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def relative_point(self, x_ratio: float, y_ratio: float) -> tuple[int, int]:
        if not 0 <= x_ratio <= 1 or not 0 <= y_ratio <= 1:
            raise ValueError("相对坐标比例必须在 0..1 之间")
        return (self.left + round(self.width * x_ratio), self.top + round(self.height * y_ratio))


class WeComWindow:
    """企业微信窗口定位。

    优先使用 pygetwindow 枚举桌面窗口；如果依赖缺失或当前环境没有 GUI，
    返回 None，由上层 dry-run/校准流程继续安全运行。
    """

    def __init__(self, title_keyword: str, *, anchors: dict[str, Any] | None = None):
        self.title_keyword = title_keyword
        self.anchors = anchors or {}
        self._last_hwnd: int | None = None

    def locate(self) -> WindowRect | None:
        rect = self._locate_via_powershell()
        if rect is not None:
            return rect

        try:
            import pygetwindow as gw  # type: ignore
        except Exception as exc:
            log.info("窗口定位依赖 pygetwindow 不可用：%s", exc)
            return None

        try:
            windows = gw.getAllWindows()
        except Exception as exc:
            log.warning("枚举窗口失败：%s", exc)
            return None

        candidates = []
        for win in windows:
            title = getattr(win, "title", "") or ""
            if self.title_keyword not in title:
                continue
            width = int(getattr(win, "width", 0) or 0)
            height = int(getattr(win, "height", 0) or 0)
            if width <= 0 or height <= 0:
                continue
            candidates.append(win)

        if not candidates:
            log.info("未找到企业微信窗口：title_keyword=%s", self.title_keyword)
            return None

        win = candidates[0]
        hwnd = getattr(win, "_hWnd", None)
        if hwnd:
            self._last_hwnd = int(hwnd)
        self._prepare_window(win)
        rect = WindowRect(
            left=int(getattr(win, "left", 0) or 0),
            top=int(getattr(win, "top", 0) or 0),
            width=int(getattr(win, "width", 0) or 0),
            height=int(getattr(win, "height", 0) or 0),
        )
        log.info("找到企业微信窗口：title=%s rect=%s", getattr(win, "title", ""), rect)
        return rect

    def _prepare_window(self, win: Any) -> None:
        maximized = False
        try:
            if hasattr(win, "maximize"):
                win.maximize()
                time.sleep(0.3)
                maximized = True
        except Exception as exc:
            log.debug("最大化企业微信窗口失败：%s", exc)
        if not maximized:
            try:
                win.resizeTo(1600, 900)
                win.moveTo(0, 0)
                time.sleep(0.2)
            except Exception as exc:
                log.debug("调整企业微信窗口尺寸失败：%s", exc)
        try:
            win.activate()
        except Exception as exc:
            log.debug("激活企业微信窗口失败：%s", exc)

    def _locate_via_powershell(self) -> WindowRect | None:
        try:
            result = run_powershell(self._powershell_locator_script(), timeout=20)
            if result is None:
                return None
            stdout = decode_process_output(result.stdout)
            stderr = decode_process_output(result.stderr)
            if result.returncode != 0 or not stdout.strip():
                log.info("PowerShell 未找到企业微信窗口：%s", stderr.strip())
                return None
            data = json.loads(stdout.strip().splitlines()[-1])
            self._last_hwnd = int(data["Hwnd"]) if data.get("Hwnd") is not None else None
            rect = WindowRect(left=int(data["Left"]), top=int(data["Top"]), width=int(data["Width"]), height=int(data["Height"]))
            log.info(
                "PowerShell 找到企业微信窗口：title=%s main_handle=%s rect=%s work_area=%sx%s",
                data.get("Title"),
                data.get("IsMainHandle"),
                rect,
                data.get("WorkAreaWidth"),
                data.get("WorkAreaHeight"),
            )
            return rect
        except Exception as exc:
            log.warning("PowerShell 窗口定位失败：%s", exc)
            return None

    def _powershell_locator_script(self) -> str:
        return """
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32Rect {
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("shcore.dll")] public static extern int SetProcessDpiAwareness(int value);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, System.Text.StringBuilder text, int count);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
  public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
"@
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
try { [void][Win32Rect]::SetProcessDpiAwareness(2) } catch { try { [void][Win32Rect]::SetProcessDPIAware() } catch {} }
Add-Type -AssemblyName System.Windows.Forms
$wxProcesses = @(Get-Process WXWork -ErrorAction SilentlyContinue)
if (-not $wxProcesses) { exit 2 }
$wxPids = @($wxProcesses | Select-Object -ExpandProperty Id)
$mainHandles = @($wxProcesses | Where-Object { $_.MainWindowHandle -ne 0 } | ForEach-Object { [long]$_.MainWindowHandle })
if (-not $wxPids) { exit 2 }
$candidates = New-Object System.Collections.Generic.List[object]
$callback = [Win32Rect+EnumWindowsProc]{
  param($hWnd, $lParam)
  [uint32]$windowPid = 0
  [void][Win32Rect]::GetWindowThreadProcessId($hWnd, [ref]$windowPid)
  if ($wxPids -contains [int]$windowPid) {
    $titleBuilder = New-Object System.Text.StringBuilder 512
    [void][Win32Rect]::GetWindowText($hWnd, $titleBuilder, $titleBuilder.Capacity)
    $rect = New-Object Win32Rect+RECT
    [void][Win32Rect]::GetWindowRect($hWnd, [ref]$rect)
    $candidates.Add([PSCustomObject]@{
      Hwnd=$hWnd.ToInt64()
      Title=$titleBuilder.ToString()
      Visible=[Win32Rect]::IsWindowVisible($hWnd)
      Left=$rect.Left
      Top=$rect.Top
      Width=$rect.Right-$rect.Left
      Height=$rect.Bottom-$rect.Top
      IsMainHandle=($mainHandles -contains $hWnd.ToInt64())
    })
  }
  return $true
}
[void][Win32Rect]::EnumWindows($callback, [IntPtr]::Zero)
$p = $candidates |
  Where-Object { $_.IsMainHandle -and $_.Width -ge 900 -and $_.Height -ge 600 -and $_.Visible -and $_.Title -like '*企业微信*' -and $_.Title -notlike 'open.work.weixin.qq.com*' } |
  Sort-Object Width -Descending |
  Select-Object -First 1
if (-not $p) {
  $p = $candidates |
  Where-Object { $_.Width -ge 900 -and $_.Height -ge 600 -and $_.Title -like '*企业微信*' -and $_.Title -notlike 'open.work.weixin.qq.com*' } |
  Sort-Object Width -Descending |
  Select-Object -First 1
}
if (-not $p) {
  $p = $candidates |
    Where-Object { $_.Width -ge 900 -and $_.Height -ge 600 -and $_.Visible } |
    Sort-Object Width -Descending |
    Select-Object -First 1
}
if (-not $p) { exit 2 }
$h = [IntPtr]$p.Hwnd
$workArea = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
[void][Win32Rect]::ShowWindow($h, 3)
[void][Win32Rect]::BringWindowToTop($h)
[void][Win32Rect]::SetForegroundWindow($h)
Start-Sleep -Milliseconds 500
$r = New-Object Win32Rect+RECT
[void][Win32Rect]::GetWindowRect($h, [ref]$r)
[PSCustomObject]@{ Hwnd=$h.ToInt64(); Left=$r.Left; Top=$r.Top; Width=$r.Right-$r.Left; Height=$r.Bottom-$r.Top; Title=$p.Title; IsMainHandle=$p.IsMainHandle; WorkAreaWidth=$workArea.Width; WorkAreaHeight=$workArea.Height } | ConvertTo-Json -Compress
""".strip()

    def anchor_point(self, name: str, rect: WindowRect) -> tuple[int, int] | None:
        """把配置中的相对锚点转换为屏幕坐标。

        支持：
        anchors:
          search_box: {x_ratio: 0.5, y_ratio: 0.2}
          send_button: [0.9, 0.92]
        """
        raw = self.anchors.get(name)
        if raw is None:
            return None
        if isinstance(raw, dict):
            return rect.relative_point(float(raw["x_ratio"]), float(raw["y_ratio"]))
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            return rect.relative_point(float(raw[0]), float(raw[1]))
        raise ValueError(f"窗口锚点格式错误：{name}")

    def scroll_chat_to_bottom(self, rect: WindowRect | None = None, *, repeats: int = 3) -> bool:
        """把当前会话尽量滚动到最新消息底部。

        这是选择“最后 N 条消息”的安全前置步骤。实现上只发送 End 键，
        不点击任何发送/确认按钮；在无 GUI/非 Windows 环境会安全返回 False。
        repeats > 1 用来处理企业微信偶发未聚焦或消息区未吃到首个按键的情况。
        """
        if repeats <= 0:
            raise ValueError("repeats 必须大于 0")
        if rect is not None:
            # 先尝试把焦点落在聊天消息区域中下部，避免 End 键作用到搜索框/输入框。
            focus = rect.relative_point(0.58, 0.72)
            self._ensure_foreground()
            self._click_point_via_pyautogui(*focus) or self._click_point_via_powershell(*focus)
            time.sleep(0.15)
        ok = False
        for _ in range(repeats):
            ok = self._send_keys_via_pyautogui("{END}") or self._send_keys_via_powershell("{END}") or ok
            time.sleep(0.15)
        if ok:
            log.info("已尝试滚动当前会话到底部 repeats=%s", repeats)
        else:
            log.info("当前环境无法发送 End 键，跳过滚动到底部")
        return ok

    def click_relative(self, rect: WindowRect, x_ratio: float, y_ratio: float) -> bool:
        self._ensure_foreground()
        point = rect.relative_point(x_ratio, y_ratio)
        log.info(
            "点击窗口相对坐标：ratio=(%.3f, %.3f) abs=(%s, %s) rect=%s",
            x_ratio,
            y_ratio,
            point[0],
            point[1],
            rect,
        )
        return self._click_point_via_pyautogui(*point) or self._click_point_via_powershell(*point)

    def click_screen(self, x: int, y: int) -> bool:
        self._ensure_foreground()
        log.info("点击屏幕坐标：abs=(%s, %s)", x, y)
        return self._click_point_via_pyautogui(x, y) or self._click_point_via_powershell(x, y)

    def right_click_relative(self, rect: WindowRect, x_ratio: float, y_ratio: float) -> bool:
        self._ensure_foreground()
        point = rect.relative_point(x_ratio, y_ratio)
        log.info(
            "右键点击窗口相对坐标：ratio=(%.3f, %.3f) abs=(%s, %s) rect=%s",
            x_ratio,
            y_ratio,
            point[0],
            point[1],
            rect,
        )
        return self._right_click_point_via_pyautogui(*point) or self._right_click_point_via_powershell(*point)

    def send_keys(self, keys: str) -> bool:
        self._ensure_foreground()
        return self._send_keys_via_pyautogui(keys) or self._send_keys_via_powershell(keys)

    def mouse_wheel_relative(self, rect: WindowRect, x_ratio: float, y_ratio: float, delta: int) -> bool:
        self._ensure_foreground()
        point = rect.relative_point(x_ratio, y_ratio)
        log.info(
            "滚动窗口相对坐标：ratio=(%.3f, %.3f) abs=(%s, %s) delta=%s rect=%s",
            x_ratio,
            y_ratio,
            point[0],
            point[1],
            delta,
            rect,
        )
        return self._mouse_wheel_via_pyautogui(point[0], point[1], delta) or self._mouse_wheel_via_powershell(point[0], point[1], delta)

    def activate(self) -> bool:
        return self._activate_last_window()

    def _is_last_window_foreground(self) -> bool:
        if self._last_hwnd is None:
            return False
        try:
            import ctypes

            return int(ctypes.windll.user32.GetForegroundWindow()) == self._last_hwnd
        except Exception:
            return False

    def _ensure_foreground(self) -> bool:
        if self._is_last_window_foreground():
            return True
        return self._activate_last_window()

    def _activate_last_window(self) -> bool:
        if self._last_hwnd is None:
            return False
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hwnd = ctypes.c_void_p(self._last_hwnd)
            user32.ShowWindow(hwnd, 3)
            user32.BringWindowToTop(hwnd)
            activated = bool(user32.SetForegroundWindow(hwnd))
            if activated or self._is_last_window_foreground():
                time.sleep(0.08)
                log.info("Win32 激活企业微信窗口完成：hwnd=%s", self._last_hwnd)
                return True
        except Exception as exc:
            log.debug("Win32 激活企业微信窗口失败，尝试 PowerShell fallback：%s", exc)
        powershell = powershell_exe()
        if powershell is None:
            return False
        script = """
param([long]$Hwnd)
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class ActivateWeComWindow {
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
}
"@
$h = [IntPtr]$Hwnd
[void][ActivateWeComWindow]::ShowWindow($h, 3)
[void][ActivateWeComWindow]::BringWindowToTop($h)
[void][ActivateWeComWindow]::SetForegroundWindow($h)
Start-Sleep -Milliseconds 120
""".strip()
        return self._run_temp_powershell(script, ["-Hwnd", str(self._last_hwnd)])

    def _send_keys_via_pyautogui(self, keys: str) -> bool:
        try:
            import pyautogui

            normalized = keys.strip()
            if normalized.startswith("{") and normalized.endswith("}"):
                normalized = normalized[1:-1]
            key_name = normalized.lower()
            aliases = {
                "esc": "esc",
                "escape": "esc",
                "end": "end",
                "enter": "enter",
                "return": "enter",
            }
            if key_name not in aliases:
                return False
            pyautogui.press(aliases[key_name])
            log.info("pyautogui 按键完成：key=%s", aliases[key_name])
            return True
        except Exception as exc:
            log.debug("pyautogui 按键失败：%s", exc)
            return False

    def _send_keys_via_powershell(self, keys: str) -> bool:
        powershell = powershell_exe()
        if powershell is None:
            return False
        script = """
param([string]$Keys)
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.SendKeys]::SendWait($Keys)
""".strip()
        return self._run_temp_powershell(script, ["-Keys", keys])

    def _click_point_via_powershell(self, x: int, y: int) -> bool:
        powershell = powershell_exe()
        if powershell is None:
            return False
        script = """
param([int]$X, [int]$Y)
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class MouseClickSafe {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("shcore.dll")] public static extern int SetProcessDpiAwareness(int value);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);
}
"@
try { [void][MouseClickSafe]::SetProcessDpiAwareness(2) } catch { try { [void][MouseClickSafe]::SetProcessDPIAware() } catch {} }
[MouseClickSafe]::SetCursorPos($X, $Y) | Out-Null
Start-Sleep -Milliseconds 80
[MouseClickSafe]::mouse_event(0x0002,0,0,0,[UIntPtr]::Zero)
Start-Sleep -Milliseconds 50
[MouseClickSafe]::mouse_event(0x0004,0,0,0,[UIntPtr]::Zero)
""".strip()
        ok = self._run_temp_powershell(script, ["-X", str(x), "-Y", str(y)])
        log.info("PowerShell 点击完成：abs=(%s, %s) ok=%s", x, y, ok)
        return ok

    def _click_point_via_pyautogui(self, x: int, y: int) -> bool:
        try:
            import pyautogui

            log.info("pyautogui 点击开始：abs=(%s, %s)", x, y)
            pyautogui.moveTo(x, y, duration=0.05)
            pyautogui.click(x, y)
            current = pyautogui.position()
            log.info("pyautogui 点击完成：requested=(%s, %s) cursor=(%s, %s)", x, y, current.x, current.y)
            return True
        except Exception as exc:
            log.debug("pyautogui 点击失败：%s", exc)
            return False

    def _mouse_wheel_via_pyautogui(self, x: int, y: int, delta: int) -> bool:
        try:
            import pyautogui

            wheel_clicks = int(delta / 120)
            if wheel_clicks == 0:
                wheel_clicks = 1 if delta > 0 else -1
            log.info("pyautogui 滚轮开始：abs=(%s, %s) clicks=%s delta=%s", x, y, wheel_clicks, delta)
            pyautogui.moveTo(x, y, duration=0.05)
            pyautogui.scroll(wheel_clicks, x=x, y=y)
            current = pyautogui.position()
            log.info("pyautogui 滚轮完成：requested=(%s, %s) cursor=(%s, %s)", x, y, current.x, current.y)
            return True
        except Exception as exc:
            log.debug("pyautogui 滚轮失败：%s", exc)
            return False

    def _right_click_point_via_pyautogui(self, x: int, y: int) -> bool:
        try:
            import pyautogui

            log.info("pyautogui 右键点击开始：abs=(%s, %s)", x, y)
            pyautogui.moveTo(x, y, duration=0.05)
            pyautogui.rightClick(x, y)
            current = pyautogui.position()
            log.info("pyautogui 右键点击完成：requested=(%s, %s) cursor=(%s, %s)", x, y, current.x, current.y)
            return True
        except Exception as exc:
            log.debug("pyautogui 右键点击失败：%s", exc)
            return False

    def _right_click_point_via_powershell(self, x: int, y: int) -> bool:
        powershell = powershell_exe()
        if powershell is None:
            return False
        script = """
param([int]$X, [int]$Y)
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class MouseRightClickSafe {
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);
}
"@
[MouseRightClickSafe]::SetCursorPos($X, $Y) | Out-Null
Start-Sleep -Milliseconds 80
[MouseRightClickSafe]::mouse_event(0x0008,0,0,0,[UIntPtr]::Zero)
Start-Sleep -Milliseconds 50
[MouseRightClickSafe]::mouse_event(0x0010,0,0,0,[UIntPtr]::Zero)
""".strip()
        return self._run_temp_powershell(script, ["-X", str(x), "-Y", str(y)])

    def _mouse_wheel_via_powershell(self, x: int, y: int, delta: int) -> bool:
        powershell = powershell_exe()
        if powershell is None:
            return False
        script = """
param([int]$X, [int]$Y, [int]$Delta)
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class MouseWheelSafe {
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, int dwData, UIntPtr dwExtraInfo);
}
"@
[MouseWheelSafe]::SetCursorPos($X, $Y) | Out-Null
Start-Sleep -Milliseconds 80
[MouseWheelSafe]::mouse_event(0x0800,0,0,$Delta,[UIntPtr]::Zero)
""".strip()
        return self._run_temp_powershell(script, ["-X", str(x), "-Y", str(y), "-Delta", str(delta)])

    def _run_temp_powershell(self, script: str, args: list[str]) -> bool:
        try:
            result = run_powershell(script, args, timeout=10)
            if result is None:
                return False
            if result.returncode != 0:
                log.debug("PowerShell 操作失败：%s", decode_process_output(result.stderr).strip())
                return False
            return True
        except Exception as exc:
            log.debug("PowerShell 操作异常：%s", exc)
            return False
