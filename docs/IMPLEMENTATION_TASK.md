# 04:00 实现任务说明

到 Asia/Shanghai `2026-05-22 04:00:00` 后执行。

## 工作目录

`/home/zhenx/.openclaw/workspace/wecom-rpa`

## 必读文件

- `docs/DESIGN.md`
- `config/config.example.yaml`
- `data/groups.example.csv`

## 实现范围

实现企业微信 Windows 桌面端 RPA 第一版代码骨架：

1. 配置加载与校验
2. 目标/占位 CSV 读取、去重（默认 bottom_of_picker 模式下不按群名搜索）
3. `max_total_send` 数量限制
4. `batch_size <= 9` 批次切分
5. SQLite 状态库
6. dry-run 状态机
7. 默认收件人选择策略：在选择聊天弹窗滚到底部，连续选择底部最多 9 个会话；不区分客户群/员工/机器人
8. 日志
9. 急停接口占位
10. 截图/模板识别接口占位
11. CLI 入口
12. README、requirements 或 pyproject、基础测试

## 安全要求

- 默认 `dry_run=true`。
- 不执行真实企业微信点击发送。
- 不做无限循环。
- 所有真实点击发送相关逻辑必须是占位或受 dry-run 保护。

## 完成后验证

运行基础测试或自检命令，并汇报：

- 修改/新增文件
- 验证命令和结果
- 剩余待做事项
