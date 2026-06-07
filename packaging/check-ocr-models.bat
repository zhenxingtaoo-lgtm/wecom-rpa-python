@echo off
setlocal
set "ROOT=%~dp0"
if not exist "%ROOT%config\config.example.yaml" if exist "%ROOT%..\config\config.example.yaml" set "ROOT=%ROOT%..\"
cd /d "%ROOT%"
if not exist "config\config.example.yaml" (
  echo Cannot find config\config.example.yaml.
  echo Please unzip and run this script from the complete WeComRPA package directory.
  echo Current directory: %CD%
  pause
  exit /b 1
)

app\wecom-rpa.exe ^
  --config config\config.example.yaml ^
  --log-file logs\ocr_check.log ^
  --screenshot-dir screenshots\ocr_check ^
  --check-ocr-models

pause
