param(
  [string]$Python = "C:\Users\ASUS\AppData\Local\Programs\Python\Python311\python.exe",
  [string]$IndexUrl = "https://pypi.tuna.tsinghua.edu.cn/simple"
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvPython = Join-Path $Root ".venv-paddle-win\Scripts\python.exe"

if (-not (Test-Path $Python)) {
  throw "Python not found: $Python"
}

Push-Location $Root
try {
  if (-not (Test-Path $VenvPython)) {
    & $Python -m venv .venv-paddle-win
  }

  & $VenvPython -m pip install --upgrade pip setuptools wheel -i $IndexUrl
  & $VenvPython -m pip install -i $IndexUrl `
    PyInstaller `
    Pillow `
    numpy==1.26.4 `
    opencv-python==4.6.0.66 `
    paddlepaddle==2.6.2 `
    paddleocr==2.7.3 `
    mss `
    pyautogui `
    keyboard `
    PyGetWindow `
    pytesseract `
    pyclipper `
    lmdb `
    rapidfuzz `
    scikit-image `
    imgaug `
    imageio `
    Cython

  & $VenvPython -m pip install -e .

  & $VenvPython -m PyInstaller --version

  New-Item -ItemType Directory -Force (Join-Path $Root "build") | Out-Null
  $CheckScriptPath = Join-Path $Root "build\check_packaging_deps.py"
  $CheckScript = @'
import importlib

modules = [
    "PIL",
    "numpy",
    "cv2",
    "paddle",
    "paddleocr",
    "mss",
    "pyautogui",
    "keyboard",
    "pygetwindow",
    "pytesseract",
    "pyclipper",
    "lmdb",
    "rapidfuzz",
    "skimage",
    "imgaug",
    "imageio",
    "Cython",
]

missing = []
for name in modules:
    try:
        importlib.import_module(name)
    except Exception as exc:
        missing.append((name, exc))

if missing:
    for name, exc in missing:
        print(f"MISSING {name}: {exc}")
    raise SystemExit(1)

print("All packaging dependencies import OK.")
'@
  Set-Content -Path $CheckScriptPath -Value $CheckScript -Encoding UTF8
  & $VenvPython $CheckScriptPath
} finally {
  Pop-Location
}
