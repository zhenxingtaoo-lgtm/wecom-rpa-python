Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class Win32ListAll {
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
  public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
"@
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Win32ListAll]::EnumWindows({ param($h,$l)
  if (-not [Win32ListAll]::IsWindowVisible($h)) { return $true }
  $sb = New-Object System.Text.StringBuilder 512
  [void][Win32ListAll]::GetWindowText($h, $sb, $sb.Capacity)
  $title = $sb.ToString()
  if ([string]::IsNullOrWhiteSpace($title)) { return $true }
  $r = New-Object Win32ListAll+RECT
  [void][Win32ListAll]::GetWindowRect($h, [ref]$r)
  if (($r.Right - $r.Left) -lt 100 -or ($r.Bottom - $r.Top) -lt 100) { return $true }
  [PSCustomObject]@{ Handle=$h.ToInt64(); Title=$title; Left=$r.Left; Top=$r.Top; Width=$r.Right-$r.Left; Height=$r.Bottom-$r.Top } | ConvertTo-Json -Compress
  return $true
}, [IntPtr]::Zero) | Out-Null
