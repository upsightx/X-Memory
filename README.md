# X-Memory：统一记忆层

## 是什么

`X-Memory` 是 AI Agent 的轻量级记忆系统。核心不是"存东西"，而是把观察、决策、检索、压缩、遗忘放在同一层，给上层模块提供稳定的记忆真源。

## 核心能力

- **结构化存储** — SQLite + FTS5 全文搜索（中英文双路索引）
- **语义检索** — 关键词 + 向量混合召回
- **生命周期管理** — LRU 热度追踪、自动归档建议、长文本摘要压缩
- **去重与溯源** — content hash 去重、lineage 追踪、反自激保护
- **统一入口** — `memory_db.py` 作为 facade，所有上层模块通过它访问

## 模块说明

| 文件 | 职责 |
|------|------|
| `memory_db.py` | 唯一 schema + migration owner，统一入口 facade |
| `db_common.py` | 数据库连接管理，通过 runtime_config 解析路径 |
| `memory_store.py` | 底层 CRUD、embedding 管理、LLM 描述生成 |
| `memory_service.py` | 高层写入、反思、记忆编排 |
| `memory_retrieval.py` | 组合检索、时间感知、多路排序 |
| `smart_recall.py` | 智能召回 + LLM 配置 |
| `memory_lru.py` | 冷热追踪、归档建议 |
| `compact.py` | 长对话/长文本压缩 |

## 快速开始

```python
from memory_db import init_db, add_observation, search

# 初始化
init_db()

# 写入
add_observation(
    type="decision",
    title="启用 auto_evolve 自动执行",
    narrative="每周日凌晨自动运行 auto_evolve，max_changes=1，有 protected_files 保护",
    source="self-evolution",
    tags=["architecture", "auto_evolve"],
    task_type="coding",
    verified=True,
)

# 检索
results = search("auto_evolve protected_files", limit=10)
```

```bash
# 查看统计
python3 memory_db.py stats

# 归档建议
python3 memory_lru.py archive-suggest
```

## 环境要求

- Python 3.8+
- SQLite 3.35+（FTS5 支持）
- 向量检索可选，需配置：
  ```bash
  export SILICONFLOW_API_KEY="sk-xxx"
  ```
  默认 embedding 模型：`BAAI/bge-m3`

## 测试

```bash
cd X-Memory
python3 -m pytest -q test_contracts.py test_memory_store_access_whitelist.py
```

3 个测试全绿，覆盖：
- schema owner 唯一性
- memory_store.init_db() 是否为 compat 转发
- 跨模块访问白名单

## 设计原则

1. **单一 schema owner** — 只有 `memory_db.py` 能定义和迁移表结构
2. **facade 模式** — 所有外部调用通过 `memory_db.py`，不直接碰 `memory_store.py`
3. **路径统一** — DB 路径通过 `runtime_config.MEMORY_DB_PATH` 解析，不硬编码
4. **兼容保留** — `memory_store.py` 保留为底层实现，逐步将高层调用迁移到 facade

## 2026-04-27 更新

- 删除被 shadow 的死代码（`add_observation` / `add_decision` / `search` 第一版）
- 清理过时注释和向后兼容说明
- 统一所有路径通过 runtime_config 解析
- `memory_store.py` LLM 描述生成器静默降级（不阻塞写入）
- 接口新增 `task_type` / `triggered_by_obs_id` / `supersedes_decision_id`
