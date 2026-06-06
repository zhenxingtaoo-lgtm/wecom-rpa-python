@echo off
setlocal
cd /d "%~dp0"

app\wecom-rpa.exe ^
  --config config\config.example.yaml ^
  --log-file logs\ocr_check.log ^
  --screenshot-dir screenshots\ocr_check ^
  --check-ocr-models

pause
