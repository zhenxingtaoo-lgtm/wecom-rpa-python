@echo off
setlocal
set "ROOT=%~dp0"
if not exist "%ROOT%config\real_send_until_daxiaochen.yaml" if exist "%ROOT%..\config\real_send_until_daxiaochen.yaml" set "ROOT=%ROOT%..\"
cd /d "%ROOT%"
if not exist "config\real_send_until_daxiaochen.yaml" (
  echo Cannot find config\real_send_until_daxiaochen.yaml.
  echo Please unzip and run this script from the complete WeComRPA package directory.
  echo Current directory: %CD%
  pause
  exit /b 1
)

echo This will really operate WeCom and send selected messages.
echo Confirm that WeCom is open, source messages are selected, and config is calibrated.
set /p CONFIRM=Type SEND to continue: 
if /I not "%CONFIRM%"=="SEND" (
  echo Cancelled.
  pause
  exit /b 1
)
set /p SEND_COUNT=Enter the maximum number of conversations to send:
if "%SEND_COUNT%"=="" (
  echo Send count is required.
  pause
  exit /b 1
)

app\wecom-rpa.exe ^
  --config config\real_send_until_daxiaochen.yaml ^
  --send-count %SEND_COUNT% ^
  --log-file logs\wecom_rpa.log ^
  --screenshot-dir screenshots\real_send ^
  --yes ^
  --no-dry-run ^
  --real-send ^
  --i-understand-this-will-send-messages

pause
