@echo off
setlocal
set "ROOT=%~dp0"
if not exist "%ROOT%app\wecom-rpa.exe" if exist "%ROOT%..\app\wecom-rpa.exe" set "ROOT=%ROOT%..\"
cd /d "%ROOT%"
if not exist "app\wecom-rpa.exe" (
  echo Cannot find app\wecom-rpa.exe.
  echo Please unzip and run this script from the complete WeComRPA package directory.
  echo Current directory: %CD%
  pause
  exit /b 1
)

app\wecom-rpa.exe ^
  --send-count 1 ^
  --batch-size 9 ^
  --log-file logs\wecom_rpa.log ^
  --screenshot-dir screenshots\dry_run ^
  --yes ^
  --dry-run

pause
