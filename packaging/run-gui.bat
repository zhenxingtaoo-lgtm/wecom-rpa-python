@echo off
setlocal
set "ROOT=%~dp0"
if not exist "%ROOT%app\wecom-rpa-gui.exe" if exist "%ROOT%..\app\wecom-rpa-gui.exe" set "ROOT=%ROOT%..\"
cd /d "%ROOT%"
if not exist "app\wecom-rpa-gui.exe" (
  echo Cannot find app\wecom-rpa-gui.exe.
  echo Please unzip and run this script from the complete WeComRPA package directory.
  echo Current directory: %CD%
  pause
  exit /b 1
)

app\wecom-rpa-gui.exe
