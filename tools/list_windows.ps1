Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class Win32List {
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
  public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
"@
$keyword = if ($args.Length -gt 0) { $args[0] } else { "企业微信" }
[Win32List]::EnumWindows({ param($h,$l)
  if (-not [Win32List]::IsWindowVisible($h)) { return $true }
  $sb = New-Object System.Text.StringBuilder 512
  [void][Win32List]::GetWindowText($h, $sb, $sb.Capacity)
  $title = $sb.ToString()
  if ([string]::IsNullOrWhiteSpace($title)) { return $true }
  if ($title.Contains($keyword) -or $title.Contains("WeCom") -or $title.Contains("WXWork")) {
    $r = New-Object Win32List+RECT
    [void][Win32List]::GetWindowRect($h, [ref]$r)
    [PSCustomObject]@{
      Handle = $h.ToInt64()
      Title = $title
      Left = $r.Left
      Top = $r.Top
      Width = $r.Right - $r.Left
      Height = $r.Bottom - $r.Top
    } | ConvertTo-Json -Compress
  }
  return $true
}, [IntPtr]::Zero) | Out-Null
