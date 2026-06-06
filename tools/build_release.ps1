param(
  [string]$Python = ".\.venv-paddle-win\Scripts\python.exe",
  [string]$ReleaseName = "WeComRPA",
  [string]$ModelSource = "$env:USERPROFILE\.paddleocr\whl"
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$BuildRoot = Join-Path $Root "build"
$DistRoot = Join-Path $Root "dist"
$ReleaseRoot = Join-Path $BuildRoot $ReleaseName
$AppRoot = Join-Path $ReleaseRoot "app"
$ZipPath = Join-Path $BuildRoot "$ReleaseName.zip"

if (-not (Test-Path $Python)) {
  throw "Python not found: $Python"
}
if (-not (Test-Path $ModelSource)) {
  throw "PaddleOCR model source not found: $ModelSource"
}

Push-Location $Root
try {
  & $Python -m PyInstaller --version | Out-Null

  Remove-Item -Recurse -Force $ReleaseRoot, $ZipPath, (Join-Path $DistRoot "wecom-rpa") -ErrorAction SilentlyContinue

  & $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --name wecom-rpa `
    --paths src `
    --collect-all paddle `
    --collect-all paddleocr `
    --collect-all Cython `
    --collect-all cv2 `
    --collect-all skimage `
    --collect-all imgaug `
    --copy-metadata imageio `
    --copy-metadata imgaug `
    --hidden-import imghdr `
    --hidden-import pyclipper `
    --hidden-import lmdb `
    --hidden-import rapidfuzz `
    --hidden-import pyautogui `
    --hidden-import mss `
    --hidden-import pygetwindow `
    --hidden-import pytesseract `
    tools\pyinstaller_entry.py

  New-Item -ItemType Directory -Force $ReleaseRoot | Out-Null
  Copy-Item -Recurse -Force (Join-Path $DistRoot "wecom-rpa") $AppRoot
  Copy-Item -Recurse -Force config (Join-Path $ReleaseRoot "config")
  Copy-Item -Recurse -Force templates (Join-Path $ReleaseRoot "templates")
  New-Item -ItemType Directory -Force (Join-Path $ReleaseRoot "data") | Out-Null
  Copy-Item -Force data\*.csv (Join-Path $ReleaseRoot "data")
  Copy-Item -Recurse -Force $ModelSource (Join-Path $ReleaseRoot "models\paddleocr")
  Copy-Item -Force packaging\*.bat $ReleaseRoot
  Copy-Item -Force packaging\README_USER.md (Join-Path $ReleaseRoot "README_USER.md")

  New-Item -ItemType Directory -Force (Join-Path $ReleaseRoot "logs"), (Join-Path $ReleaseRoot "screenshots") | Out-Null

  Compress-Archive -Path (Join-Path $ReleaseRoot "*") -DestinationPath $ZipPath -Force
  Write-Host "Release directory: $ReleaseRoot"
  Write-Host "Release zip: $ZipPath"
} finally {
  Pop-Location
}
