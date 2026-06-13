# 企业微信批量转发 RPA

Windows 企业微信桌面端批量转发工具。默认 dry-run；真实发送必须显式授权。

## 当前运行方式

- 不读取群列表 CSV。
- 不创建或读取 SQLite 状态库。
- 用户在 GUI 中填写本次“发送数量”。
- 程序按 `batch_size <= 9` 自动拆批。
- 每批打开“发送给”窗口、滚动到底部并从底部向上选择会话。
- 命中配置的哨兵会话后，只发送哨兵下方的会话并结束运行。
- 中断后重新运行会重新从列表底部开始，不做断点续跑。
- 每次正式启动前都会保存本次实际参数快照到 `logs/run_snapshots/`。

## GUI

```powershell
.\.venv-paddle-win\Scripts\python.exe -m wecom_rpa.gui
```

在 GUI 中填写发送数量、批次大小等参数，完成环境检查后再启动。

## CLI

Dry-run：

```powershell
.\.venv-paddle-win\Scripts\python.exe -m wecom_rpa.main `
  --config config/config.example.yaml `
  --send-count 10 `
  --yes `
  --dry-run
```

真实发送：

```powershell
.\.venv-paddle-win\Scripts\python.exe -m wecom_rpa.main `
  --config config/real_send_until_daxiaochen.yaml `
  --send-count 50 `
  --yes `
  --no-dry-run `
  --real-send `
  --i-understand-this-will-send-messages
```

## OCR

开发机默认使用 `%USERPROFILE%\.paddleocr\whl` 缓存。打包版会携带
`models/paddleocr`。配置中 `model_root: null` 时程序自动查找这些位置。

## 测试与打包

```powershell
.\.venv-paddle-win\Scripts\python.exe -m unittest discover -s tests -v
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_release.ps1
```

运行产物：

- `logs/wecom_rpa.log`：过程日志。
- `logs/run_snapshots/*.json`：每次运行的参数快照。
- `screenshots/`：检查点和错误截图。
