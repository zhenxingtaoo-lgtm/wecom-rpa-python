# 企业微信批量转发 RPA

Windows 企业微信桌面端批量转发工具。用户先在企业微信中勾选待转发消息，程序负责分批选择收件会话、识别哨兵边界并执行逐条转发。

## 当前实现

- 不使用 YAML 配置文件、CSV 群列表或 SQLite 状态库。
- 每次运行前由 GUI 或 CLI 提供发送数量、每批数量、批次间隔和哨兵名称。
- 每批最多选择 9 个会话。
- 企业微信窗口会在操作前激活并最大化。
- “检查环境”会截图识别当前源消息蓝色勾选框和“逐条转发”按钮，并记录本次坐标。
- 后续批次按检查阶段记录的源消息行重新进入多选。
- 收件人弹窗打开后，程序拖动左侧会话列表滚动条到底部，并通过截图差异确认拖动生效。
- 拖动后点击底部轨道只用于确认列表已经稳定在底部；如果拖动本身没有产生移动，程序停止。
- 从列表底部向上选择会话，不区分群聊、员工或机器人。
- 命中哨兵时，哨兵及其上方会话会被取消勾选，只发送哨兵下方会话。
- 点击“立即停止”只停止当前运行，不关闭 GUI。
- 运行结束弹窗使用中文显示计划数量、实际发送数量和总耗时。
- 中断后重新运行会再次从会话列表底部开始，不做断点续跑。

## GUI 使用

```powershell
.\.venv-paddle-win\Scripts\python.exe -m wecom_rpa.gui
```

操作顺序：

1. 打开并登录企业微信，进入源消息所在会话。
2. 手动进入消息多选并勾选本次需要转发的消息。
3. 在 GUI 中填写发送数量、每批数量和批次间隔。
4. 按需启用哨兵，并填写一个或多个哨兵名称。
5. 选择 Dry-run 或真实发送；真实发送需要勾选两个确认项。
6. 点击“检查环境”。识别不到源消息蓝勾或“逐条转发”按钮时不会继续。
7. 检查成功后点击“启动运行”。真实发送还需要输入 `SEND`。

修改任何运行参数后，必须重新执行“检查环境”。

## 检查环境

“检查环境”会执行：

1. 校验 GUI 参数和真实发送确认项。
2. 定位、激活并最大化企业微信窗口。
3. 保存当前屏幕检查截图。
4. 识别源消息蓝色勾选框，数量以当前实际勾选结果为准。
5. 使用 PaddleOCR 识别底部工具栏的“逐条转发”按钮。
6. 将源消息行坐标和按钮坐标写入本次运行参数。
7. 把 GUI 恢复到前台并弹窗显示检查结果。

## CLI

Dry-run：

```powershell
.\.venv-paddle-win\Scripts\python.exe -m wecom_rpa.main `
  --send-count 10 `
  --batch-size 9 `
  --batch-interval-sec 0 `
  --yes `
  --dry-run
```

真实发送：

```powershell
.\.venv-paddle-win\Scripts\python.exe -m wecom_rpa.main `
  --send-count 50 `
  --batch-size 9 `
  --batch-interval-sec 0 `
  --sentinel-name 大小尘 `
  --yes `
  --no-dry-run `
  --real-send `
  --i-understand-this-will-send-messages
```

多个哨兵可重复使用 `--sentinel-name`。

## OCR 与图像识别

- 文本识别使用 PaddleOCR 中文模型。
- 开发环境默认查找 `%USERPROFILE%\.paddleocr\whl`。
- 打包版本查找随包携带的 `models/paddleocr`。
- PaddleOCR 正常返回 0 行表示截图中没有文字，不会触发 Windows OCR。
- 蓝色已选框和灰色未选框主要使用 OpenCV 识别。

## 安全机制

- 默认 Dry-run，不点击最终发送按钮。
- 真实发送必须显式授权；GUI 还要求输入 `SEND`。
- 每批最多 9 个会话。
- 找不到窗口、弹窗、源消息、逐条转发按钮或足够的会话复选框时停止。
- 滚动条拖动无效或无法证明已经到底时停止。
- 哨兵 OCR、截断或截断后复核失败时停止。
- 发送后仍检测到收件人弹窗时停止。
- 急停快捷键默认为 `Ctrl+Alt+Q`。

## 运行留痕

- `logs/wecom_rpa.log`：操作、坐标、识别结果和步骤耗时。
- `logs/run_snapshots/*.json`：每次启动前的最终参数快照。
- `screenshots/checkpoints/`：检查、滚动、选择和发送后的截图。
- `screenshots/errors/`：异常截图。

## 测试与打包

```powershell
.\.venv-paddle-win\Scripts\python.exe -m unittest discover -s tests -v
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_release.ps1
```
