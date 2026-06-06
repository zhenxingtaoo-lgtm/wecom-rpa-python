@echo off
setlocal
cd /d "%~dp0"

echo This will really operate WeCom and send selected messages.
echo Confirm that WeCom is open, source messages are selected, and config is calibrated.
set /p CONFIRM=Type SEND to continue: 
if /I not "%CONFIRM%"=="SEND" (
  echo Cancelled.
  pause
  exit /b 1
)

app\wecom-rpa.exe ^
  --config config\real_send_until_daxiaochen.yaml ^
  --groups data\real_send_sentinel_50.csv ^
  --db data\wecom_rpa.sqlite3 ^
  --log-file logs\wecom_rpa.log ^
  --screenshot-dir screenshots\real_send ^
  --yes ^
  --no-dry-run ^
  --real-send ^
  --i-understand-this-will-send-messages

pause
