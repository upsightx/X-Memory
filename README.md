# X-Memory：统一记忆层

## 是什么

`X-Memory` 是一套给 AI Agent 用的轻量级记忆系统，核心目标不是单纯“存东西”，而是把观察、决策、检索、压缩、遗忘这些动作放到同一层里，让上层模块有稳定的记忆真源可依赖。

这次收口之后，`X-Memory` 的职责边界更明确了：
- `memory_db.py`：唯一 schema 与 migration owner
- `memory_store.py`：底层存储实现与兼容层
- `memory_service.py` / `memory_retrieval.py`：高层服务与检索逻辑

## 核心能力

- 语义检索：支持关键词 + 向量召回
- 主动遗忘：支持 LRU、归档、压缩
- 结构化存储：SQLite 存索引与结构化字段
- 高层统一入口：通过 `memory_db` facade 暴露稳定接口

## 目录说明

| 文件 | 职责 |
|------|------|
| `memory_db.py` | 唯一 schema+migration owner，负责初始化与兼容导出 |
| `memory_store.py` | 底层 SQLite CRUD、embedding 与兼容 init wrapper |
| `memory_service.py` | 高层写入、反思、记忆编排 |
| `memory_retrieval.py` | 组合检索、时间感知、多路排序 |
| `smart_recall.py` | 智能召回逻辑 |
| `memory_lru.py` | 热度管理与归档 |
| `compact.py` | 长记忆压缩 |
| `db_common.py` | 数据库连接与基础工具 |

## 快速开始

### 1. 初始化数据库

```python
from memory_db import init_db

init_db()
```

### 2. 构建向量

```python
from memory_store import build_embeddings

build_embeddings()
```

### 3. 写入记忆

```python
from memory_service import remember

remember(
    content="proposal 状态统一由 proposal_lifecycle_manager 管理",
    type="decision",
    title="proposal 状态真源收口",
    tags=["architecture", "proposal"],
    task_type="coding",
)
```

### 4. 检索记忆

```python
from memory_service import recall

ctx = recall("proposal 状态机是怎么统一的？", top_k=5)
print(ctx)
```

## 环境要求

- Python 3.8+
- 如需向量能力，配置 SiliconFlow API Key：

```bash
export SILICONFLOW_API_KEY="sk-xxxxxxxxxxxxxxxx"
```

默认使用：
- Embedding：`BAAI/bge-m3`
- 总结/检索：`Qwen/Qwen2.5-7B-Instruct`

## 测试

```bash
cd /root/.openclaw/workspace/X-Memory
python3 -m pytest -q test_contracts.py test_memory_store_access_whitelist.py
```

当前测试重点：
- schema owner 是否仍然唯一
- `memory_store.init_db()` 是否只做 compat 转发
- 高层模块是否继续绕过 `memory_db` 直接访问 `memory_store`

## 设计说明

### 为什么保留 `memory_store.py`

因为它仍然承担底层存储实现，直接删除会打断旧调用。现在的策略不是粗暴移除，而是：
- 先把 owner 收到 `memory_db.py`
- 再把高层调用逐步往 `memory_db` facade 迁移
- 最后让 `memory_store.py` 退到“底层实现 + 兼容层”位置

### 为什么强调单一真源

记忆系统最怕的是：
- schema 在 A 文件定义
- migration 在 B 文件补丁
- 高层直接绕过 facade 去碰底层

这样改几轮之后就会出现 contract drift。现在这次重构，核心就是把这些漂移压回去。

## 当前状态

这版已经完成：
- schema/migration owner 收口
- 高层调用开始统一走 facade
- 基础 contract tests 补齐

剩下保留的是可控兼容层，不是主干性问题。
