#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: 未找到 python3，请先安装 Python 3.11+" >&2
  exit 1
fi

if [ ! -d .venv ]; then
  echo "[1/4] 创建 Python 虚拟环境 .venv ..."
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

if [ ! -f .venv/.wecom_rpa_installed ]; then
  echo "[2/4] 安装依赖（第一次会慢一点）..."
  python -m pip install --upgrade pip
  python -m pip install -e '.[windows]'
  touch .venv/.wecom_rpa_installed
else
  echo "[2/4] 依赖已安装，跳过"
fi

echo "[3/4] 截图校准自检：只截图/裁剪，不点击"
PYTHONPATH=src python -m wecom_rpa.calibration probe --crop-suggestions || true

echo "[4/4] 启动 dry-run：模拟处理测试群，不会真实发送"
python -m wecom_rpa.main \
  --config config/config.example.yaml \
  --groups data/test_groups_1_9.csv \
  --db data/wecom_rpa.sqlite3 \
  --dry-run

echo
echo "完成。日志：logs/wecom_rpa.log"
echo "截图：screenshots/"
if command -v explorer.exe >/dev/null 2>&1; then
  explorer.exe screenshots >/dev/null 2>&1 || true
fi
