# AGENTS.md

此文件为 Codex (Codex.ai/code) 提供代码库操作指引。

## 构建/测试/运行

```bash
# 安装依赖（开发 / dry-run）
python -m venv .venv && source .venv/bin/activate && pip install -e .[test]

# Windows 原生环境（真实 GUI 操作）
.\.tools\uv.exe python install 3.11
.\.tools\uv.exe venv .venv-paddle-win --python 3.11
.\.tools\uv.exe pip install --python .\.venv-paddle-win\Scripts\python.exe -i https://pypi.tuna.tsinghua.edu.cn/simple PyYAML Pillow numpy==1.26.4 opencv-python==4.6.0.66 paddlepaddle==2.6.2 paddleocr==2.7.3 mss pyautogui

# 运行测试
python -m pytest tests/ -v
# 或
python -m unittest discover -s tests -v

# 运行单个测试文件
python -m unittest tests.test_config_groups -v

# Dry-run（安全，不产生真实点击/发送）
python -m wecom_rpa.main --config config/config.example.yaml --groups data/groups.example.csv --db data/wecom_rpa.sqlite3 --yes --dry-run

# 校准探针（仅截图，不点击）
PYTHONPATH=src python -m wecom_rpa.calibration probe --crop-suggestions

# 真实发送（仅 Windows 原生环境，必须同时传入两个授权参数）
.\.venv-paddle-win\Scripts\python.exe -m wecom_rpa.main --config config/config.example.yaml --groups data/groups.example.csv --db data/wecom_rpa.sqlite3 --yes --no-dry-run --real-send --i-understand-this-will-send-messages
```

## 架构

企业微信 Windows 桌面端批量转发 RPA，安全优先。核心原则：**默认 dry-run，真实发送必须双重显式授权**。

### 模块职责

| 模块 | 职责 |
|---|---|
| `main.py` | CLI 入口，组装 config、groups、storage 和 ForwardFlow。 |
| `config.py` | YAML → 冻结 dataclass（`AppConfig`、`WindowConfig`、`OcrConfig`、`SentinelConfig` 等）。加载时校验所有安全约束。 |
| `forward_flow.py` | 核心转发状态机。分发到 `_run_bottom_picker_batch`（dry-run）和 `_run_real_bottom_picker_batch`（真实发送）。处理哨兵边界截断、跨批次源消息重选、OCR 校验。 |
| `screen.py` | `ScreenInspector`：截图、OpenCV 模板匹配、OCR（PaddleOCR/Windows OCR/Tesseract）、蓝色复选框检测。GUI 依赖缺失时安全降级。 |
| `wecom_window.py` | `WeComWindow`：在 Windows 原生桌面环境定位和控制企业微信窗口。所有坐标使用窗口相对比例（0..1）。 |
| `storage.py` | `StateStore`：SQLite 上下文管理器，含 `targets` 和 `runs` 两张表。支持 upsert、状态转换、重试计数、断点续跑。 |
| `safety.py` | `StopController`（全局急停热键）、`assert_send_limit`、`assert_batch_selection_count`。每批次硬限制最多 9 个选择。 |
| `models.py` | `TargetStatus` 枚举（pending/selected/sent/failed/skipped/uncertain）、`TargetGroup`、`Batch` dataclass。 |
| `groups.py` | CSV 加载（UTF-8-sig，按 group_name 去重）、`limit_groups`、`split_batches`。 |
| `calibration.py` | 只读校准 CLI，用于探测企业微信窗口和裁剪校准截图，不产生任何点击。 |

### 关键设计决策

- **客户机运行环境是 Windows 原生桌面**：不保留跨环境驱动 Windows GUI 的路径转换和挂载盘 PowerShell fallback。
- **Python 后端优先，PowerShell 只作 Windows 原生 fallback**：截图优先走 `mss/pyautogui`；部分窗口、OCR 或像素扫描能力仍可调用本机 PowerShell，但不支持跨环境桥接。
- **`bottom_of_picker` 模式**是默认且主要的收件人选择策略：点击"逐条转发"后弹出选择聊天窗口，脚本滚动到底部，然后选择底部 N 个会话。无需逐个搜索群名。已发送的会话发送后自然浮到顶部，下一轮继续选底部的新会话。
- **哨兵边界**：可选的员工名称列表，作为"停在这里"的标记。OCR 在已选列表中识别到哨兵名称时，取消勾选哨兵及其上方的所有项，仅发送哨兵下方的会话。
- **`uncertain` 状态**是断点续跑的硬阻断：真实发送后如果证据缺失，目标被标记为 `uncertain`，下次运行拒绝继续，直到人工复查并手动修改数据库。
- **源消息重选**：第一批之后，后续批次通过右键消息区域选择"多选"重新进入多选模式，然后点击蓝色复选框位置（通过 `find_selected_checkbox_ratios` 扫描蓝色像素检测）。

### 环境工作流

- **开发/测试**：使用普通 Python venv 运行单元测试和 dry-run。
- **Windows 原生**（`.venv-paddle-win`）：用于真实 GUI 操作、GUI 调试和客户机打包验证。使用 uv + 清华镜像安装 PaddlePaddle/PaddleOCR。

### 安全防线

1. `--dry-run`（默认）绝不点击发送按钮
2. 单独传入 `--no-dry-run` 会被拒绝——必须同时传入 `--real-send` + `--i-understand-this-will-send-messages`
3. `batch_size` 上限 9（企业微信限制）
4. `max_total_send` 为必填配置项
5. `require_confirm_before_start` 和 `require_confirm_first_batch` 要求输入 `YES` 确认（可通过 `--yes` 跳过）
6. 全局热键 `ctrl+alt+q` 触发急停
7. 哨兵边界检测加发送前复查校验
