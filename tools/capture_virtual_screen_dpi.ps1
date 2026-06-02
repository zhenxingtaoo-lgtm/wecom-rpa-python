param([string]$OutPath)
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class DpiFix {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("shcore.dll")] public static extern int SetProcessDpiAwareness(int value);
}
"@
try { [void][DpiFix]::SetProcessDpiAwareness(2) } catch { try { [void][DpiFix]::SetProcessDPIAware() } catch {} }
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bounds.Size)
$bmp.Save($OutPath, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
$screens = [System.Windows.Forms.Screen]::AllScreens | ForEach-Object { "$($_.DeviceName):$($_.Bounds.X),$($_.Bounds.Y),$($_.Bounds.Width)x$($_.Bounds.Height)" }
Write-Output "saved $OutPath virtual=$($bounds.Left),$($bounds.Top),$($bounds.Width)x$($bounds.Height) screens=$($screens -join ';')"
