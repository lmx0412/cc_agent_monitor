# Agent Monitor

macOS 常驻浮动窗口，实时展示多个 Claude Code Agent 的执行状态。

## 效果

- 始终置顶，不遮挡全屏 App 外的工作区
- 最多显示 5 个 Agent 任务，每行展示：状态图标、任务名、状态文字、耗时
- 可拖动，位置自动持久化
- 每 10 秒自动刷新

## 快速开始

### 1. 安装依赖

```bash
cd cc_workspace/agent-monitor
pip install -r requirements.txt
```

### 2. 启动监控窗口

```bash
python monitor_app.py
```

窗口默认出现在屏幕右下角，可拖动到任意位置。

---

## Agent 侧接入

### 方式 A：Python 函数调用（推荐）

```python
import sys
sys.path.insert(0, "/path/to/agent-monitor")
from agent_monitor import update_status, clear_agent

# 任务开始
update_status("session-abc", "需求解析", "running", "正在读取接口文档")

# 任务完成
update_status("session-abc", "需求解析", "done")

# 任务出错
update_status("session-abc", "需求解析", "error", "接口文档不存在")

# 清除（从列表移除）
clear_agent("session-abc")
```

### 方式 B：命令行调用

```bash
# 更新状态
python /path/to/agent-monitor/agent_monitor.py \
  --id "session-abc" \
  --name "需求解析" \
  --status running \
  --detail "正在读取接口文档"

# 清除
python /path/to/agent-monitor/agent_monitor.py \
  --id "session-abc" \
  --clear
```

---

## 状态说明

| 状态 | 图标 | 颜色 | 含义 |
|------|------|------|------|
| `running` | ● | 蓝色 | 执行中 |
| `done` | ✓ | 绿色 | 已完成 |
| `error` | ✗ | 红色 | 出错 |
| `waiting` | ○ | 灰色 | 等待中 |

耗时列：执行中显示「从开始到现在」的分钟数；完成/出错后显示「从开始到结束」的固定时长；等待中显示 `—`。

---

## 状态文件格式

共享状态文件路径：`~/.claude/agent-status.json`

```json
{
  "updated_at": "2026-04-13T14:32:01",
  "agents": [
    {
      "id": "session-abc",
      "name": "需求解析",
      "status": "running",
      "detail": "正在读取接口文档",
      "started_at": "2026-04-13T14:28:00",
      "ended_at": null,
      "updated_at": "2026-04-13T14:32:01"
    }
  ]
}
```

- Agent 侧只写文件，Monitor 侧只读文件，完全解耦
- 最多保留 5 条记录，超出时自动移除最旧的已完成任务
- 写入使用原子操作（先写 `.tmp` 再 `rename`），避免读到半截数据

---

## 文件结构

```
agent-monitor/
├── monitor_app.py      # 浮动窗口主程序（PyQt6）
├── agent_monitor.py    # Agent 侧状态写入工具
├── pricing.json        # Token 计费配置（可自定义定价）
├── requirements.txt    # PyQt6>=6.4.0
└── README.md
```

### 自定义定价

编辑 `pricing.json` 可配置每百万 token 的费用（USD）。默认为标准 Anthropic claude-sonnet 定价：

```json
{
  "currency": "USD",
  "per_million_tokens": {
    "input":          3.00,
    "cache_creation": 3.75,
    "cache_read":     0.30,
    "output":        15.00
  }
}
```

如使用第三方代理或其他模型，按实际费率修改对应字段即可。

持久化文件（自动生成）：
```
~/.claude/
├── agent-status.json       # 共享状态（Agent 写，Monitor 读）
└── monitor-position.json   # 窗口位置持久化
```
