# WeCom RPA 用户版说明

## 使用前准备

1. 使用 Windows 10/11 x64。
2. 安装并登录企业微信 Windows 客户端。
3. 解压整个 `WeComRPA` 目录，不要只复制其中的 exe，也不要在压缩包预览窗口里直接运行 bat。
4. 确认 `models/paddleocr` 目录存在；包内已包含 PaddleOCR 中文模型，不需要联网下载模型。

## 首次自检

双击 `check-ocr-models.bat`。

看到 `ocr_status=ok` 表示 PaddleOCR 依赖和离线模型可初始化。

## dry-run 自检

推荐双击 `run-gui.bat` 打开图形界面，选择 `Dry-run 自检` 后点击 `检查环境` 和 `启动运行`。

也可以双击 `run-dry-run.bat` 使用命令行模式。

dry-run 不会真实发送消息，只会验证配置、状态库和基础流程。

## 真实发送

1. 打开企业微信源会话。
2. 手动勾选需要转发的源消息。
3. 确认 `config/real_send_until_daxiaochen.yaml` 里的哨兵名称和坐标适合当前窗口。
4. 双击 `run-gui.bat` 打开图形界面。
5. 选择 `真实发送`，勾选两个真实发送确认项。
6. 点击 `检查环境`，确认群数量、哨兵、状态库和企业微信窗口状态。
7. 点击 `启动运行`，在确认提示中输入 `SEND` 后才会真实运行。

仍可双击 `run-real-send-until-daxiaochen.bat` 使用命令行模式。

## 图形界面急停

GUI 运行中会启用 `立即停止` 按钮。点击后程序会请求急停，并在下一个安全检查点停止；不会强制杀进程，以避免中断到半次点击或数据库写入。

## 运行产物

- `logs/`：运行日志。
- `screenshots/`：关键步骤截图和错误截图。
- `data/wecom_rpa.sqlite3`：断点续跑状态库。

重新运行时会读取同一个状态库，`sent` 和 `skipped` 的目标会自动跳过。遇到 `uncertain` 时请先人工核对截图和企业微信记录。
