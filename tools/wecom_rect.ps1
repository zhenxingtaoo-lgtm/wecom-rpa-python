Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32Rect {
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
"@
[Console]::OutputEncoding=[Text.Encoding]::UTF8
$p = Get-Process WXWork -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 -and -not [string]::IsNullOrWhiteSpace($_.MainWindowTitle) } | Select-Object -First 1
if (-not $p) { Write-Output "NO_WINDOW"; exit 2 }
$h = $p.MainWindowHandle
[void][Win32Rect]::ShowWindow($h, 9) # SW_RESTORE
[void][Win32Rect]::SetForegroundWindow($h)
Start-Sleep -Milliseconds 500
$r = New-Object Win32Rect+RECT
[void][Win32Rect]::GetWindowRect($h, [ref]$r)
[PSCustomObject]@{ ProcessName=$p.ProcessName; Id=$p.Id; Title=$p.MainWindowTitle; Handle=$h.ToInt64(); Left=$r.Left; Top=$r.Top; Width=$r.Right-$r.Left; Height=$r.Bottom-$r.Top } | ConvertTo-Json -Compress
