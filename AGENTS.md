# AGENTS.md

## 构建/测试/运行

```powershell
.\.venv-paddle-win\Scripts\python.exe -m unittest discover -s tests -v
.\.venv-paddle-win\Scripts\python.exe -m wecom_rpa.gui
.\.venv-paddle-win\Scripts\python.exe -m wecom_rpa.main --send-count 10 --batch-size 9 --yes --dry-run
```

真实发送必须同时使用：

```text
--no-dry-run --real-send --i-understand-this-will-send-messages
```

## 架构

- `config.py`：运行时参数结构、默认值与安全校验。
- `forward_flow.py`：数量驱动的批量转发流程、哨兵截断和 OCR 复查。
- `gui.py`：GUI 参数输入、环境检查、运行快照和工作线程。
- `screen.py`：截图、OCR、蓝色勾选框和灰色复选框检测。
- `wecom_window.py`：企业微信窗口定位、激活、点击和滚动。
- `safety.py`：急停和单批最多 9 个会话限制。

## 当前设计

- 不使用 CSV 群列表。
- 不使用 SQLite 状态库或断点续跑。
- 发送数量由 GUI 或 CLI `--send-count` 提供。
- 不使用 YAML 配置文件；GUI 或 CLI 在每次运行前提供可变参数。
- 每次运行从会话列表底部重新开始。
- 每次启动前写入 `logs/run_snapshots/*.json` 参数快照。
- `bottom_of_picker` 是唯一支持的收件人选择模式。
- 默认 dry-run；真实发送保留双重显式授权。
