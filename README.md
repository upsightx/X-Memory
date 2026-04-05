# X-Memory — 结构化记忆系统

> 让 AI 拥有长期记忆的结构化存储与检索引擎。

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

## 是什么

X-Memory 是一个基于 SQLite + FTS5 的结构化记忆系统，核心使命是：**让 AI 从"每次会话失忆重启"变为"持续增长、可检索、可压缩"的长期记忆体**。

传统 AI 助手每次会话都是空白状态。X-Memory 提供：
- **结构化存储**：观察（observations）、决策（decisions）、会话摘要（session_summaries）分表存储，带元数据
- **全文检索**：FTS5 支持关键词搜索、类型过滤、时间范围查询
- **自动压缩**：冷记忆自动归档，关键信息提取到核心记忆库
- **热度管理**：基于访问频率和时间衰减计算记忆"热度"，自动识别冷记忆

## 核心架构

```
┌─────────────────────────────────────────┐
│          X-Memory 记忆系统               │
├─────────────────────────────────────────┤
│  memory_store.py    — 写入层            │
│  memory_retrieval.py — 检索层           │
│  memory_service.py  — 服务层封装        │
│  memory_db.py       — SQLite + FTS5    │
│  memory_lru.py      — 热度管理与归档     │
│  compact.py         — 记忆压缩          │
│  smart_recall.py    — 智能召回          │
│  db_common.py       — 数据库通用工具    │
└─────────────────────────────────────────┘
         ↓
   SQLite (memory.db)
   - observations 表
   - decisions 表
   - session_summaries 表
   - FTS5 全文索引
```

## 模块一览

| 模块 | 职责 | 核心接口 |
|------|------|----------|
| `memory_db.py` | 数据库初始化、表结构、FTS5 索引 | `init_db()`, `get_connection()` |
| `memory_store.py` | 写入层，提供结构化写入接口 | `add_observation()`, `add_decision()`, `add_session_summary()` |
| `memory_retrieval.py` | 检索层，支持关键词/类型/时间过滤 | `search()`, `get_by_type()`, `get_recent()` |
| `memory_service.py` | 服务层，封装常用操作 | `get_last_7_days()`, `get_decisions_by_topic()` |
| `memory_lru.py` | 热度管理，计算访问频率+时间衰减 | `calculate_hotness()`, `get_cold_memories()` |
| `compact.py` | 记忆压缩，归档冷记忆 | `archive_old_memories()`, `compress_to_summary()` |
| `smart_recall.py` | 智能召回，结合上下文检索相关记忆 | `recall_relevant()` |
| `db_common.py` | 数据库通用工具 | 连接池、事务管理 |

## 快速开始

```python
from X记忆.memory_store import add_observation, add_decision
from X记忆.memory_retrieval import search

# 写入观察
add_observation(
    content="子 Agent 用 MiniMax 模型重构代码成功率仅 40%",
    tags=["coding", "subagent", "minimax"],
    metadata={"source": "feedback_loop"}
)

# 写入决策
add_decision(
    content="重构任务改用 Opus 模型，一次性成功率提升至 100%",
    tags=["coding", "model-selection"],
    metadata={"alternatives_rejected": ["继续用 MiniMax"]}
)

# 检索
results = search("子 Agent", limit=10)
for r in results:
    print(r['content'], r['type'], r['created_at'])
```

## 数据库结构

### observations 表
```sql
CREATE TABLE observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    tags TEXT,                    -- JSON 数组
    metadata TEXT,                -- JSON 对象
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    accessed_at TIMESTAMP,
    access_count INTEGER DEFAULT 0
);
```

### decisions 表
```sql
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    tags TEXT,
    metadata TEXT,                -- 包含 rejected_alternatives
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    accessed_at TIMESTAMP,
    access_count INTEGER DEFAULT 0
);
```

### session_summaries 表
```sql
CREATE TABLE session_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    summary TEXT NOT NULL,
    key_points TEXT,              -- JSON 数组
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### FTS5 全文索引
```sql
CREATE VIRTUAL TABLE memory_fts USING fts5(
    content,
    content='observations',
    content_rowid='id'
);
```

## 设计原则

1. **结构化 > 纯文本**：SQLite 比 Markdown 更适合检索和统计
2. **自动压缩**：早期日志归档，关键信息提取到 memory_db
3. **热度感知**：基于访问频率和时间衰减，自动识别冷记忆
4. **零依赖**：纯 Python + SQLite，无外部库

## 更新日志

### 2026-03-29 — 初始版本
- 8 个核心模块完成
- SQLite + FTS5 结构化存储
- 全文检索、类型过滤、时间范围查询
- 冷记忆归档、热度计算

---

_Built with [OpenClaw](https://github.com/openclaw/openclaw)_
