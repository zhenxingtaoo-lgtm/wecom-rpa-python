Add-Type @"
using System;
using System.Runtime.InteropServices;
public class ForegroundWindow {
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
}
"@

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$p = Get-Process WXWork -ErrorAction SilentlyContinue |
  Where-Object { $_.MainWindowHandle -ne 0 -and -not [string]::IsNullOrWhiteSpace($_.MainWindowTitle) -and $_.MainWindowTitle -notlike 'open.work.weixin.qq.com*' } |
  Select-Object -First 1

if (-not $p) {
  Write-Error "WXWork main window not found"
  exit 1
}

$shell = New-Object -ComObject WScript.Shell
[void][ForegroundWindow]::ShowWindow($p.MainWindowHandle, 9)
Start-Sleep -Milliseconds 200
[void][ForegroundWindow]::SetWindowPos($p.MainWindowHandle, [IntPtr]::Zero, 98, 78, 1201, 801, 0x0040)
Start-Sleep -Milliseconds 200
[void][ForegroundWindow]::BringWindowToTop($p.MainWindowHandle)
[void][ForegroundWindow]::SetForegroundWindow($p.MainWindowHandle)
[void]$shell.AppActivate($p.Id)
Start-Sleep -Milliseconds 500

[PSCustomObject]@{
  Id = $p.Id
  Title = $p.MainWindowTitle
  Handle = $p.MainWindowHandle
} | ConvertTo-Json -Compress
