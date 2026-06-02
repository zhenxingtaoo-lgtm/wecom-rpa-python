# 企业微信桌面端批量转发 RPA 设计文档

> 项目目录：`/home/zhenx/.openclaw/workspace/wecom-rpa`  
> 目标平台：Windows 桌面端企业微信

## 1. 目标

对 Windows 桌面端企业微信执行半自动 RPA：用户先确认/选中待转发的 2-5 条消息，程序在“选择聊天/发送给”弹窗中滚动到底部，连续勾选底部最多 9 个会话后分批转发。企业微信每次最多选择 9 个会话，因此系统以 9 个会话为一个批次，支持数量上限、断点续跑、失败记录和急停。

## 2. 非目标

第一版不做完全自动识别并勾选原始消息，避免误选消息。第一版默认由用户人工预选消息，并由 RPA 负责在转发弹窗里按底部列表批量选择目标会话和确认转发。默认不按群名搜索，也不校验客户群/员工/机器人类型。

## 3. 总体原则

1. **安全优先**：必须有 `max_total_send`，绝不无限循环。
2. **强状态机**：每一步操作后截图判断状态，不符合预期就暂停/失败。
3. **人工确认关键点**：开始前确认待转发消息；前一批建议人工确认。
4. **图像优先，坐标兜底**：UIA 不可用时采用截图模板匹配、OCR、窗口锚点和相对坐标。
5. **可恢复**：所有目标会话/占位记录的状态落库，失败后可从断点继续。

## 4. 技术栈

- Python 3.11+
- pyautogui：鼠标键盘操作
- mss / Pillow：截图
- opencv-python：模板匹配、按钮/状态识别
- paddleocr：中文群名/弹窗文字识别；Windows OCR 作为兜底
- keyboard：全局急停
- sqlite3：任务状态库
- PyYAML：配置
- pyinstaller：Windows exe 打包

## 5. 项目结构

```text
wecom-rpa/
├─ docs/
│  └─ DESIGN.md
├─ src/wecom_rpa/
│  ├─ __init__.py
│  ├─ main.py              # CLI 入口
│  ├─ config.py            # 配置加载与校验
│  ├─ forward_flow.py      # 转发主流程与状态机
│  ├─ storage.py           # sqlite/jsonl 记录
│  ├─ screen.py            # 截图、模板匹配、OCR 封装
│  ├─ wecom_window.py      # 企业微信窗口定位与锚点
│  ├─ forward_flow.py      # 转发主流程
│  ├─ safety.py            # 急停、数量限制、风控间隔
│  └─ models.py            # 数据模型
├─ config/
│  └─ config.example.yaml
├─ data/
│  └─ groups.example.csv
├─ templates/              # 按钮/状态模板图片
├─ logs/
├─ screenshots/
│  ├─ errors/
│  └─ checkpoints/
└─ tests/
```

## 6. 运行流程

### 6.1 用户准备

1. 打开企业微信 Windows 客户端。
2. 进入包含待转发消息的会话。
3. 手动选中 2-5 条待转发消息。
4. 启动 RPA。
5. RPA 截图保存检查点，提示用户确认“消息已正确选中”。

### 6.2 RPA 批量转发

```text
读取配置和目标/占位列表
去重并按 max_total_send 截断
按 batch_size=9 分批
对每批：
  检查急停
  打开转发窗口
  在选择聊天弹窗内滚动到底部
  从底部连续勾选最多 9 个会话
  不过滤员工/机器人/非客户群
  记录 selected
  校验已选数量
  点击发送
  校验发送后状态
  记录 sent / failed
  等待 batch_interval_sec
达到上限或列表结束后停止
```

## 7. 待转发消息可靠性设计

第一版采用“人工预选 + 截图留证 + 启动确认”：

- 程序不猜测原始消息。
- 开始前保存一张 `screenshots/checkpoints/message_selection_*.png`。
- 可选：通过模板识别选中标记数量，要求在 2-5 之间。
- 如果无法识别数量，则要求用户确认后继续。

第二版再考虑自动选消息：根据文本 OCR、发送时间、相对位置、消息卡片模板定位，但必须保留人工确认。

## 8. 企业微信控件树不可用时的定位方案

### 8.1 窗口锚点

先定位企业微信窗口矩形：

```text
window = (left, top, width, height)
```

所有点击点都用相对坐标：

```text
search_box = (left + dx, top + dy)
send_button = (left + dx, top + dy)
```

### 8.2 模板匹配

模板图片放在 `templates/`：

- `forward_button.png`
- `send_button.png`
- `search_box.png`
- `selected_checkmark.png`
- `confirm_dialog.png`
- `error_dialog.png`

操作前先找模板，找不到就不点击。

### 8.3 收件人选择策略

默认策略：`recipient_selection.mode: bottom_of_picker`。

依据用户当前真实操作流程：每次点击逐条转发后，在弹出的聊天选择框内滚动到最下面，选择底部 9 个会话发送；发送过的会话会自动排到最上面，下一次再滚到底部选择新的 9 个。

优点：

- 不需要逐个搜索群名；
- 不需要 OCR 校验群名；
- 不受同名群搜索结果影响；
- 速度显著快于搜索方案。

边界：

- 用户已确认底部出现员工、机器人也可以发送，因此默认 `allow_staff_and_bots: true`，不做类型过滤。
- CSV 在该模式下主要用于控制总数量、批次数和状态落库；不作为搜索关键词。
- 仍需校验每批勾选数量不能超过 9。

### 8.4 OCR 校验（旧搜索策略备用）

仅当启用 `recipient_selection.mode: search_by_name` 时使用。搜索群聊时不直接点击第一个结果，而是：

1. 截图搜索结果区域；
2. OCR 识别候选群名；
3. 与目标群名做严格/模糊匹配；
4. 匹配失败则标记 `failed` 并截图。

OCR 后端由配置控制：

```yaml
ocr:
  engine: paddleocr
  lang: ch
  fallback: windows
```

当前默认优先使用 PaddleOCR 解析中文、小字号列表文本，并把结果统一映射成 `OcrLine`。PaddleOCR 不可用或未识别到文本时，如果 `fallback: windows`，再尝试 Windows OCR。

## 9. 数量控制

配置必须包含：

```yaml
max_total_send: 100
batch_size: 9
batch_interval_sec: 5
max_retry_per_group: 2
recipient_selection:
  mode: bottom_of_picker
  allow_staff_and_bots: true
  scroll_to_bottom_repeats: 5
stop_hotkey: ctrl+alt+q
dry_run: true
require_confirm_before_start: true
require_confirm_first_batch: true
```

规则：

- `batch_size` 最大不能超过 9。
- `max_total_send` 必须大于 0。
- `dry_run=true` 时不点击最终发送按钮，只走选择和校验流程。
- `bottom_of_picker` 模式下不按群名搜索；每批只表示从弹窗底部连续选择 N 个会话。
- 任何时候按急停快捷键，当前批次立即停止并记录状态。

## 10. 数据状态

SQLite 表：

```sql
CREATE TABLE targets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  batch_no INTEGER,
  retry_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  updated_at TEXT
);

CREATE TABLE runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT,
  finished_at TEXT,
  config_json TEXT,
  status TEXT
);
```

目标状态：

- `pending`
- `selected`
- `sent`
- `failed`
- `skipped`
- `uncertain`

断点续跑规则：

- 使用同一个 SQLite 状态库重新启动时，程序先读取当前 CSV 目标的已有状态。
- `sent` 和 `skipped` 被视为已完成，不再重新处理。
- `pending`、`selected`、`failed` 会进入本次批次列表。
- 只要当前输入目标中存在 `uncertain`，本次运行立即停止并截图，要求人工确认后再继续。

`uncertain` 用于真实发送中的不确定边界：程序已经进入可能产生真实发送影响的阶段，但无法证明后续状态安全。例如已点击发送但发送后截图证据不可用，或哨兵截断后无法复查左侧勾选数量。该状态默认不能自动重试，避免重复发送。

## 10.1 哨兵边界与 OCR 复查

启用 `recipient_selection.sentinel.enabled` 后，每批真实发送会读取左侧已勾选候选列表并按从下往上的顺序解析已选会话。识别到哨兵名称时：

1. 保留哨兵下面的会话。
2. 点击左侧复选框取消哨兵本身以及哨兵上方所有已选项。
3. 再次复查左侧蓝色勾选数量，确认剩余数量等于将要发送的数量。
4. 复查失败时标记本批可发送目标为 `uncertain` 并停止，不点击发送。

OCR 没有读到文本、读到数量与预期不一致、无法定位左侧行、或取消勾选后复查失败，都属于不能安全继续的情况。

## 11. 异常处理

遇到以下情况立即暂停或失败：

- 找不到企业微信窗口
- 找不到转发/发送按钮
- 搜索结果与目标群名不匹配
- 已选会话数量异常
- 弹出错误/风控/登录失效提示
- 启用哨兵时左侧已勾选候选列表 OCR 失败或截断后复查失败
- 发送后截图证据不可用
- 急停触发

失败时保存：

- 错误截图
- 当前群名
- 当前批次
- 错误原因

## 12. MVP 实现顺序

1. 创建 CLI、配置加载、群列表读取。
2. 实现状态库和断点续跑。
3. 实现截图、窗口定位、相对坐标配置。
4. 实现 dry-run 流程。
5. 实现搜索并选择群聊。
6. 实现每批最多 9 个目标。
7. 实现发送确认、日志、失败截图。
8. 打包 Windows exe。

## 13. 第一版验收标准

- 可读取 `groups.csv`。
- 能限制本次最多发送数量。
- 能按 9 个一批处理。
- 能人工确认后开始。
- 能 dry-run 不实际发送。
- 能记录 sent/failed/skipped。
- 中断后能继续未完成目标。
- 急停可用。
- 关键失败有截图。
