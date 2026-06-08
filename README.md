# 企业微信 RPA

安全优先的企业微信 Windows 桌面端批量转发 RPA。默认 dry-run；真实发送必须显式关闭 dry-run，并同时提供两级真实发送授权参数。项目同时提供 CLI 和 Tkinter GUI 两种入口。

## 已实现

- YAML 配置加载与校验
- 目标群 CSV 读取、空行忽略、按群名去重
- `max_total_send` 限制
- `batch_size <= 9` 校验与批次切分
- SQLite 状态库：`targets`、`runs`
- 断点续跑：`sent/skipped` 自动跳过，`pending/failed/selected` 继续处理，`uncertain` 阻塞并要求人工确认
- dry-run 状态机：`pending -> selected -> skipped`
- 收件人选择策略已改为 `bottom_of_picker`：在“选择聊天/发送给”弹窗内滚到底部，连续选择底部最多 9 个会话；不按群名搜索，也不区分客户群/员工/机器人
- 可选哨兵边界：识别左侧已勾选候选列表，遇到配置的员工私聊哨兵时，只发送哨兵下面的会话
- 真实发送路径：在人工确认和双重授权后，通过 PowerShell/Win32 坐标操作打开转发弹窗、滚动到底部、勾选收件人、点击发送并截图复查
- 后续批次源消息重选：通过右键菜单重新进入多选，并用蓝色复选框检测校验源消息选中状态
- 日志输出到控制台和 `logs/wecom_rpa.log`
- 急停接口：CLI 支持 `keyboard` 可用时注册全局热键；GUI 运行时使用界面上的“立即停止”按钮
- 截图保存：支持 `mss/Pillow` 或 `pyautogui`，无 GUI 时写占位文件
- OpenCV 模板匹配框架：模板缺失或依赖缺失时返回未匹配，不执行危险动作
- 企业微信窗口定位：优先通过 PowerShell 枚举/激活/调整 Windows 企业微信主窗口，必要时回退 `pygetwindow`
- OCR 后端可配置：支持 PaddleOCR、Windows OCR、Tesseract；默认优先 PaddleOCR，失败时回退 Windows OCR
- 蓝色复选框检测：通过 PowerShell `System.Drawing` 扫描截图中的蓝色勾选框，用于源消息和收件人复查
- Tkinter GUI：提供参数选择、环境检查、运行摘要、日志面板、真实发送确认和急停按钮
- CLI 入口、GUI 入口、校准入口和基础测试

## 安装/运行

```bash
cd /path/to/wecom-rpa
python -m venv .venv
source .venv/bin/activate
pip install -e .[test]
# Windows 实机校准时再安装可选依赖：
# pip install -e .[windows]
```

本机已验证的 Windows 原生 PaddleOCR 调试环境：

```powershell
.\.tools\uv.exe python install 3.11
.\.tools\uv.exe venv .venv-paddle-win --python 3.11
$env:UV_HTTP_TIMEOUT='120'
.\.tools\uv.exe pip install --python .\.venv-paddle-win\Scripts\python.exe -i https://pypi.tuna.tsinghua.edu.cn/simple PyYAML Pillow numpy==1.26.4 opencv-python==4.6.0.66 paddlepaddle==2.6.2 paddleocr==2.7.3 mss pyautogui
```

WSL 中的 PaddlePaddle 预编译包可能因为 CPU 指令集触发 `Illegal instruction`。真实 GUI 调试建议使用上面的 Windows 原生环境运行。

GUI 启动：

```powershell
.\.venv-paddle-win\Scripts\python.exe -m wecom_rpa.gui
```

打包版客户电脑可双击 `run-gui.bat`。GUI 默认进入 `Dry-run 自检` 模式；切换到 `真实发送` 后必须勾选两个确认项，并在启动弹窗中输入 `SEND`。

校准截图自检（只截图/裁剪，不点击）：

```bash
PYTHONPATH=src python3 -m wecom_rpa.calibration probe --crop-suggestions
```

自检 dry-run：

```bash
python -m wecom_rpa.main \
  --config config/config.example.yaml \
  --groups data/groups.example.csv \
  --db data/wecom_rpa.sqlite3 \
  --yes \
  --dry-run
```

`--yes` 仅跳过人工确认，适合测试/cron；不会关闭 dry-run。

CLI 真实发送必须同时提供以下参数：

```powershell
$env:PYTHONPATH='src'
.\.venv-paddle-win\Scripts\python.exe -m wecom_rpa.main `
  --config config/config.example.yaml `
  --groups data/groups.example.csv `
  --db data/wecom_rpa.sqlite3 `
  --log-file logs/wecom_rpa.log `
  --screenshot-dir screenshots/real_run `
  --yes `
  --no-dry-run `
  --real-send `
  --i-understand-this-will-send-messages
```

WSL dry-run 仍可使用：

```bash
python -m wecom_rpa.main \
  --config config/config.example.yaml \
  --groups data/groups.example.csv \
  --db data/wecom_rpa.sqlite3 \
  --yes \
  --dry-run
```

真实发送前请确认企业微信窗口可见、待转发源消息已正确选中，并确保配置文件中的源消息坐标、转发按钮坐标、哨兵名称和 OCR 模型目录与当前窗口布局一致。实机推荐使用 `config/real_send_until_daxiaochen.yaml` 或按校准结果维护自己的真实发送配置。

## 配置

参考 `config/config.example.yaml`。关键安全规则：

- `max_total_send` 必须大于 0
- `batch_size` 必须在 `1..9` 之间
- `dry_run: false` 或 `--no-dry-run` 只有在同时传入 `--real-send` 和 `--i-understand-this-will-send-messages` 时才允许
- 默认 `recipient_selection.mode: bottom_of_picker`，真实发送会走“弹窗底部连续勾选”路线；`groups.csv` 在这个模式下主要作为发送数量/批次数占位记录，不用于搜索群名。

OCR 默认配置：

```yaml
ocr:
  engine: paddleocr
  lang: ch
  fallback: windows
  model_root: models/paddleocr
```

`paddleocr` 用于中文会话名识别；打包版会随程序携带离线模型目录 `models/paddleocr`，不会要求客户电脑联网下载模型。依赖不可用、模型未就绪或识别失败时，`fallback: windows` 会尝试 Windows OCR。

## Windows 打包

打包目标是 Windows 10/11 x64 普通办公电脑。构建机需要先准备 `.venv-paddle-win`，并确保本机已有 PaddleOCR 模型缓存 `%USERPROFILE%\.paddleocr\whl`。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_release.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tools\check_release.ps1
```

输出目录：

- `build\WeComRPA\`：可直接运行的完整目录。
- `build\WeComRPA.zip`：发给客户测试的压缩包。

客户电脑只需要解压整个目录，先运行 `check-ocr-models.bat` 验证离线 OCR 模型，再按 `README_USER.md` 运行 dry-run 或真实发送脚本。企业微信 Windows 客户端仍需客户本机提前安装并登录。
打包目录会同时包含 CLI 可执行文件和 GUI 可执行文件，推荐普通用户优先使用 `run-gui.bat`。

## 群列表 CSV

参考 `data/groups.example.csv`：

```csv
group_name
示例客户群A
示例客户群B
```

只读取 `group_name` 列；重复群名只保留第一次。

在默认 `bottom_of_picker` 模式下，程序不按这些名称搜索企业微信；这些行用于表示“本次预计处理多少个会话”和落库追踪。正式使用时可以填客户群名，也可以填 `slot-0001` 这类占位名。

## 默认目标选择流程

当前方案已按实际操作习惯调整为：

```text
逐条转发 / 分别转发
  ↓
弹出“选择聊天/发送给”窗口
  ↓
滚动到底部
  ↓
从底部连续勾选最多 9 个会话
  ↓
发送后已发送会话自动排到上面
  ↓
下一轮再次滚到底部，继续选择底部未发送会话
```

用户已确认：底部如果出现员工、机器人也可以选择，因此该策略不做客户群类型过滤。

## 状态说明

- `pending`：待处理
- `selected`：已模拟选择
- `sent`：已完成真实发送或已由人工确认为完成
- `failed`：流程失败，可按策略重试
- `skipped`：跳过；dry-run 下代表最终发送被跳过
- `uncertain`：程序已经进入可能真实发送的阶段，但发送后证据或哨兵截断复查不可靠；续跑会停止，需人工确认后处理

## 断点续跑

重新使用同一个 `--db` 启动时，程序会读取已有目标状态：

- `sent` / `skipped`：认为已完成，自动跳过。
- `pending` / `selected` / `failed`：继续处理。
- `uncertain`：立即停止，不自动重发。

`uncertain` 一般来自两类情况：

- 已点击发送，但发送后截图证据不可用。
- 启用哨兵后，取消勾选哨兵及其上方项之后，左侧勾选数量复查失败。

恢复方式需要人工查看日志、截图和企业微信聊天记录。如果确认已经发送，可在 SQLite 中把对应目标改成 `sent`；如果确认未发送，可改回 `pending` 后再续跑。

## 哨兵边界

启用 `recipient_selection.sentinel.enabled` 后，程序会在每批勾选后 OCR 左侧已勾选候选列表：

```yaml
recipient_selection:
  sentinel:
    enabled: true
    names: ["员工哨兵1", "员工哨兵2", "员工哨兵3"]
    stop_on_detection_failure: true
```

识别到哨兵时，程序只保留哨兵下面的会话，点击左侧复选框取消哨兵及其上方已选项，然后再次复查左侧勾选数量。识别失败、数量不一致或复查失败时，真实发送不会继续。

**注意**：哨兵名称不要包含"外部""全员"等企业微信标签字眼。OCR 识别时会自动去除这些标记再做匹配，含此类字眼的哨兵名称可能导致匹配偏差。

## Windows 实机校准准备

详见 `docs/CALIBRATION.md`。

1. 安装可选依赖：`pip install -e .[windows]`。在 WSL 环境下，工具也可通过 Windows PowerShell 做截图和窗口定位。
2. 打开企业微信 Windows 客户端，保持主窗口可见。
3. 运行校准截图自检，确认 `screenshots/checkpoints/` 和 `screenshots/calibration/` 下能保存 PNG。
4. 将按钮模板图片放入 `templates/`，建议文件名：
   - `forward_button.png`
   - `send_button.png`
   - `search_box.png`
   - `selected_checkmark.png`
   - `confirm_dialog.png`
   - `error_dialog.png`
5. 校准 `config/config.example.yaml` 的 `window.anchors` 相对锚点。

## 当前限制与后续待做

1. 继续在更多 Windows 分辨率、DPI、企业微信版本上验证并微调相对坐标、OCR 区域和蓝色复选框阈值。
2. 若重新启用 `search_by_name` 策略，需要补齐按群名搜索、OCR 校验搜索结果和 dry-run 模拟路径；当前主路径是 `bottom_of_picker`。
3. 当前仍要求用户人工预选源消息；后续可评估自动定位最后 N 条消息，但必须保留人工确认和截图复查。
4. 模板匹配框架已接入，但主流程主要依赖相对坐标、OCR 和蓝色复选框检测；如要改成模板优先，需要采集企业微信按钮模板并实机验证阈值。
5. GUI 已提供启动和急停控制，后续可增加配置编辑器、状态库人工复查/修复界面和最近截图预览。
