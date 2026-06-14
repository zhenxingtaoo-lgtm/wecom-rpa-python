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
  --log-file logs\ocr_check.log ^
  --screenshot-dir screenshots\ocr_check ^
  --check-ocr-models

pause
