from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


def _decode_process_output(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _windows_path(path: Path) -> str:
    wslpath = shutil.which("wslpath")
    if not wslpath:
        return str(path)
    converted = subprocess.run([wslpath, "-w", str(path)], check=False, capture_output=True, text=True)
    return converted.stdout.strip() if converted.returncode == 0 else str(path)


def _powershell_exe() -> Path | None:
    native = shutil.which("powershell.exe") or shutil.which("powershell")
    if native:
        return Path(native)
    wsl_path = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    return wsl_path if wsl_path.exists() else None


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
        try:
            if getattr(win, "isMaximized", False):
                win.restore()
                time.sleep(0.2)
        except Exception as exc:
            log.debug("还原企业微信窗口失败：%s", exc)
        try:
            win.resizeTo(1201, 801)
            win.moveTo(98, 78)
            time.sleep(0.2)
        except Exception as exc:
            log.debug("调整企业微信窗口尺寸失败：%s", exc)
        try:
            win.activate()
        except Exception as exc:
            log.debug("激活企业微信窗口失败：%s", exc)

    def _locate_via_powershell(self) -> WindowRect | None:
        powershell = _powershell_exe()
        if powershell is None:
            return None
        script = self._powershell_locator_script()
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", encoding="utf-8-sig", dir=Path(tempfile.gettempdir()), delete=False) as f:
            f.write(script)
            script_path = Path(f.name)
        try:
            result = subprocess.run(
                [str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", _windows_path(script_path)],
                check=False,
                capture_output=True,
                timeout=20,
            )
            stdout = _decode_process_output(result.stdout)
            stderr = _decode_process_output(result.stderr)
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
        finally:
            try:
                script_path.unlink()
            except OSError:
                pass

    def _powershell_locator_script(self) -> str:
        return """
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32Rect {
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
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
$targetWidth = [Math]::Min(1600, $workArea.Width)
$targetHeight = [Math]::Min(900, $workArea.Height)
[void][Win32Rect]::ShowWindow($h, 9)
[void][Win32Rect]::SetWindowPos($h, [IntPtr]::Zero, $workArea.Left, $workArea.Top, $targetWidth, $targetHeight, 0x0040)
[void][Win32Rect]::BringWindowToTop($h)
[void][Win32Rect]::SetForegroundWindow($h)
Start-Sleep -Milliseconds 300
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
            self._click_point_via_powershell(*focus)
            time.sleep(0.15)
        ok = False
        for _ in range(repeats):
            ok = self._send_keys_via_powershell("{END}") or ok
            time.sleep(0.15)
        if ok:
            log.info("已尝试滚动当前会话到底部 repeats=%s", repeats)
        else:
            log.info("当前环境无法发送 End 键，跳过滚动到底部")
        return ok

    def click_relative(self, rect: WindowRect, x_ratio: float, y_ratio: float) -> bool:
        self._activate_last_window()
        point = rect.relative_point(x_ratio, y_ratio)
        return self._click_point_via_powershell(*point)

    def click_screen(self, x: int, y: int) -> bool:
        self._activate_last_window()
        return self._click_point_via_pyautogui(x, y) or self._click_point_via_powershell(x, y)

    def right_click_relative(self, rect: WindowRect, x_ratio: float, y_ratio: float) -> bool:
        self._activate_last_window()
        point = rect.relative_point(x_ratio, y_ratio)
        return self._right_click_point_via_powershell(*point)

    def send_keys(self, keys: str) -> bool:
        self._activate_last_window()
        return self._send_keys_via_powershell(keys)

    def mouse_wheel_relative(self, rect: WindowRect, x_ratio: float, y_ratio: float, delta: int) -> bool:
        self._activate_last_window()
        point = rect.relative_point(x_ratio, y_ratio)
        return self._mouse_wheel_via_powershell(point[0], point[1], delta)

    def _activate_last_window(self) -> bool:
        if self._last_hwnd is None:
            return False
        powershell = _powershell_exe()
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
[void][ActivateWeComWindow]::ShowWindow($h, 9)
[void][ActivateWeComWindow]::BringWindowToTop($h)
[void][ActivateWeComWindow]::SetForegroundWindow($h)
Start-Sleep -Milliseconds 80
""".strip()
        return self._run_temp_powershell(script, ["-Hwnd", str(self._last_hwnd)])

    def _send_keys_via_powershell(self, keys: str) -> bool:
        powershell = _powershell_exe()
        if powershell is None:
            return False
        script = """
param([string]$Keys)
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.SendKeys]::SendWait($Keys)
""".strip()
        return self._run_temp_powershell(script, ["-Keys", keys])

    def _click_point_via_powershell(self, x: int, y: int) -> bool:
        powershell = _powershell_exe()
        if powershell is None:
            return False
        script = """
param([int]$X, [int]$Y)
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class MouseClickSafe {
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);
}
"@
[MouseClickSafe]::SetCursorPos($X, $Y) | Out-Null
Start-Sleep -Milliseconds 80
[MouseClickSafe]::mouse_event(0x0002,0,0,0,[UIntPtr]::Zero)
Start-Sleep -Milliseconds 50
[MouseClickSafe]::mouse_event(0x0004,0,0,0,[UIntPtr]::Zero)
""".strip()
        return self._run_temp_powershell(script, ["-X", str(x), "-Y", str(y)])

    def _click_point_via_pyautogui(self, x: int, y: int) -> bool:
        try:
            import pyautogui

            pyautogui.click(x, y)
            return True
        except Exception as exc:
            log.debug("pyautogui 点击失败：%s", exc)
            return False

    def _right_click_point_via_powershell(self, x: int, y: int) -> bool:
        powershell = _powershell_exe()
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
        powershell = _powershell_exe()
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
        powershell = _powershell_exe()
        if powershell is None:
            return False
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", encoding="utf-8-sig", dir=Path(tempfile.gettempdir()), delete=False) as f:
            f.write(script)
            script_path = Path(f.name)
        try:
            result = subprocess.run(
                [str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", _windows_path(script_path), *args],
                check=False,
                capture_output=True,
                timeout=10,
            )
            if result.returncode != 0:
                log.debug("PowerShell 操作失败：%s", _decode_process_output(result.stderr).strip())
                return False
            return True
        except Exception as exc:
            log.debug("PowerShell 操作异常：%s", exc)
            return False
        finally:
            try:
                script_path.unlink()
            except OSError:
                pass
