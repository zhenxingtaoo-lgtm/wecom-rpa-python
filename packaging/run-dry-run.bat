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
  --groups data\groups.example.csv ^
  --db data\wecom_rpa.sqlite3 ^
  --log-file logs\wecom_rpa.log ^
  --screenshot-dir screenshots\dry_run ^
  --yes ^
  --dry-run

pause
