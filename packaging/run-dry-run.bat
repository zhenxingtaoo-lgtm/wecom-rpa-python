@echo off
setlocal
cd /d "%~dp0"

app\wecom-rpa.exe ^
  --config config\config.example.yaml ^
  --groups data\groups.example.csv ^
  --db data\wecom_rpa.sqlite3 ^
  --log-file logs\wecom_rpa.log ^
  --screenshot-dir screenshots\dry_run ^
  --yes ^
  --dry-run

pause
