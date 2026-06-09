# 企业微信 RPA 实机校准指南

> 校准命令只做截图、裁剪、dry-run 自检；真实发送只允许通过主程序显式授权参数运行。

## 1. 基础自检

在 Windows 原生项目目录执行：

```bash
cd C:\path\to\wecom-rpa
$env:PYTHONPATH='src'
python -m wecom_rpa.calibration probe --crop-suggestions
```

成功时会输出：

- 企业微信窗口矩形
- `screenshots/checkpoints/wecom_window_probe_*.png` 窗口截图
- `screenshots/calibration/*.png` 建议区域裁剪图

如果提示未找到窗口：

1. 确认企业微信已打开且不是最小化。
2. 点击一下企业微信主窗口，让它在前台。
3. 再运行 probe。

## 2. 建议区域说明

`probe --crop-suggestions` 会自动生成这些区域：

- `window_full.png`：窗口整图，用于判断定位是否正确。
- `search_box_area.png`：左上搜索框/加号区域。
- `conversation_list.png`：会话列表区域。
- `chat_header.png`：聊天标题栏。
- `chat_content.png`：聊天内容区域。
- `input_area.png`：输入框/工具栏区域。
- `nav_bar.png`：左侧功能导航栏。

这些不是最终按钮模板，只是校准参考图。

## 3. 手动裁剪模板

当用户把企业微信切到对应状态后，可以从窗口截图里裁剪模板：

```bash
$env:PYTHONPATH='src'
python -m wecom_rpa.calibration crop `
  --source screenshots/checkpoints/wecom_window_probe_YYYYMMDD_HHMMSS.png `
  --out templates/search_box.png `
  --left 80 --top 20 --width 220 --height 40
```

注意：如果 `source` 是窗口局部截图，`left/top` 是相对这张截图左上角的坐标；如果 `source` 是全屏截图，`left/top` 是屏幕坐标。

推荐模板文件名：

- `forward_button.png`
- `send_button.png`
- `search_box.png`
- `selected_checkmark.png`
- `confirm_dialog.png`
- `error_dialog.png`

## 4. 当前仍需要人工配合的状态

以下状态必须由用户手动摆好界面后再截图，不自动乱点：

1. 已人工选中 2-5 条待转发消息。
2. 已打开转发弹窗，但还没有点击发送。
3. 搜索某个目标群后，搜索结果可见。
4. 异常弹窗/确认弹窗出现时。

## 5. 安全边界

- `dry_run=false` 只有在主程序同时传入 `--real-send` 和 `--i-understand-this-will-send-messages` 时才允许。
- `probe` 和 `crop` 只截图/裁剪，不点击鼠标键盘。
- dry-run 流程只写状态库：`selected -> skipped`，不会写 `sent`。
- 真实发送失败后如果状态变成 `uncertain`，续跑会阻塞，必须人工确认后再恢复。
