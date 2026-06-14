# 企业微信 RPA 实机检查与校准指南

当前主流程会在 GUI“检查环境”阶段自动识别源消息蓝勾和“逐条转发”按钮。校准工具主要用于排查窗口定位、DPI 和截图区域，不是日常运行的必需步骤。

## 窗口截图自检

```powershell
.\.venv-paddle-win\Scripts\python.exe -m wecom_rpa.calibration probe --crop-suggestions
```

该命令只截图和裁剪，不点击企业微信。输出包括：

- 企业微信窗口矩形；
- `screenshots/checkpoints/wecom_window_probe_*.png`；
- `screenshots/calibration/` 下的窗口区域参考图。

如果找不到窗口：

1. 确认企业微信已打开并登录。
2. 确认主窗口没有最小化。
3. 手动点击企业微信后重试。

## GUI 检查前状态

1. 企业微信显示源消息所在会话。
2. 已进入消息多选状态。
3. 已勾选本次需要转发的全部消息，数量不限于固定 2 条。
4. 蓝色勾选框和底部“逐条转发”按钮均可见。
5. 不要在检查过程中操作鼠标或切换窗口。

点击“检查环境”后，程序会自动最大化企业微信并保存检查截图。识别完成后会弹窗并把 GUI 恢复到前台。

## 检查失败排查

### 未识别到源消息

- 查看最新 `gui_source_selection_check_*.png`。
- 确认蓝勾位于同一列。
- 确认消息没有超出当前可见区域。
- 检查 Windows 显示缩放或截图是否异常。

### 未识别到逐条转发

- 查看检查截图底部工具栏是否完整。
- 确认企业微信窗口已经最大化。
- 查看日志中的 PaddleOCR 文本和耗时。

### 滚动条拖动失败

- 查看 `recipient_scroll_before` 和 `recipient_scroll_after_drag_*`。
- 日志中拖动结果必须为 `moved=True` 且差异大于阈值。
- 后续复核截图应连续两轮 `difference=0.000`。
- 拖动无效时程序会停止，不会继续选择会话。

## 手动裁剪

仅在开发模板或分析截图时使用：

```powershell
.\.venv-paddle-win\Scripts\python.exe -m wecom_rpa.calibration crop `
  --source screenshots/checkpoints/wecom_window_probe_YYYYMMDD_HHMMSS.png `
  --out templates/example.png `
  --left 80 --top 20 --width 220 --height 40
```

## 安全边界

- `probe` 和 `crop` 不进行鼠标键盘操作。
- 默认 Dry-run。
- 真实发送需要完整显式授权。
- 不使用状态库；中断后重新运行会从列表底部重新开始。
