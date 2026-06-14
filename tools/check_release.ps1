param(
  [string]$ReleaseDir = ".\build\WeComRPA"
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$ReleasePath = Resolve-Path (Join-Path $Root $ReleaseDir)
$Exe = Join-Path $ReleasePath "app\wecom-rpa.exe"
$GuiExe = Join-Path $ReleasePath "app\wecom-rpa-gui.exe"

if (-not (Test-Path $Exe)) {
  throw "Release executable not found: $Exe"
}
if (-not (Test-Path $GuiExe)) {
  throw "Release GUI executable not found: $GuiExe"
}

Push-Location $ReleasePath
try {
  & $Exe `
    --log-file logs\ocr_check.log `
    --screenshot-dir screenshots\ocr_check `
    --check-ocr-models
  if ($LASTEXITCODE -ne 0) {
    throw "OCR model check failed with exit code $LASTEXITCODE"
  }

  & $Exe `
    --send-count 1 `
    --batch-size 9 `
    --log-file logs\release_dry_run.log `
    --screenshot-dir screenshots\release_dry_run `
    --yes `
    --dry-run
  if ($LASTEXITCODE -ne 0) {
    throw "Dry-run check failed with exit code $LASTEXITCODE"
  }
} finally {
  Pop-Location
}
