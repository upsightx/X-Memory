# X-Memory: 智能体语义记忆系统

**X-Memory** 是一个专为 AI Agent 设计的轻量级、语义增强型记忆管理系统。它不仅仅是存储数据，更具备**主动遗忘**、**语义联想**和**自我进化**的能力。

## 🌟 核心特性

*   **语义检索 (Semantic Search)**：集成硅基流动 `BAAI/bge-m3` 模型，支持基于向量相似度的深度记忆召回，不再局限于关键词匹配。
*   **主动遗忘 (LRU & Compaction)**：内置 `memory_lru.py` 和 `compact.py`，自动清理低价值记忆，保持系统轻量化。
*   **混合存储架构**：采用 SQLite 进行结构化索引，Markdown 进行内容存储，兼顾检索效率与人类可读性。
*   **智能召回 (Smart Recall)**：通过“关键词粗筛 + 语义向量精排 + LLM 总结”的三级漏斗，精准命中相关记忆。

## 🚀 快速开始

### 1. 环境准备

确保你已安装 Python 3.8+。

### 2. 配置硅基流动 (SiliconFlow)

本系统默认使用硅基流动的免费 Embedding 模型进行向量化。你需要设置以下环境变量：

```bash
# 获取 Key: https://cloud.siliconflow.cn/
export SILICONFLOW_API_KEY="sk-xxxxxxxxxxxxxxxx"

# 可选：自定义模型（默认为 BAAI/bge-m3）
export EMBED_MODEL="BAAI/bge-m3"
```

### 3. 初始化与构建向量

首次使用时，建议为存量记忆构建向量索引：

```python
from memory_store import init_db, build_embeddings

# 初始化数据库表结构
init_db()

# 遍历所有记忆并生成向量嵌入
build_embeddings()
```

### 4. 语义检索示例

```python
from smart_recall import select_relevant

# 系统会自动调用 SilconFlow API 进行语义匹配
results = select_relevant("我们之前关于自主进化的讨论有哪些？")
for r in results:
    print(f"📄 {r['filename']}: {r['description']}")
```

## 📂 项目结构

| 文件 | 职责 |
| :--- | :--- |
| `memory_store.py` | 核心存储层，支持 SQLite CRUD 及 Embedding 构建 |
| `smart_recall.py` | 智能召回引擎，集成关键词、向量与 LLM 筛选 |
| `memory_lru.py` | 记忆热度管理，实现自动归档与清理 |
| `compact.py` | 记忆压缩工具，将流水账提炼为核心原则 |
| `db_common.py` | 数据库连接池与基础工具 |

## 🤝 贡献

欢迎提交 Issue 或 PR。如果你有更高效的向量化方案或检索算法，欢迎交流！

## 📄 许可证

MIT License
