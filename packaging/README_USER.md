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

双击 `run-dry-run.bat`。

dry-run 不会真实发送消息，只会验证配置、状态库和基础流程。

## 真实发送

1. 打开企业微信源会话。
2. 手动勾选需要转发的源消息。
3. 确认 `config/real_send_until_daxiaochen.yaml` 里的哨兵名称和坐标适合当前窗口。
4. 双击 `run-real-send-until-daxiaochen.bat`。
5. 在确认提示中输入 `SEND` 后才会真实运行。

## 运行产物

- `logs/`：运行日志。
- `screenshots/`：关键步骤截图和错误截图。
- `data/wecom_rpa.sqlite3`：断点续跑状态库。

重新运行时会读取同一个状态库，`sent` 和 `skipped` 的目标会自动跳过。遇到 `uncertain` 时请先人工核对截图和企业微信记录。
