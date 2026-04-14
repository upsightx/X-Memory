#!/usr/bin/env python3
"""
Memory Service — 自我进化记忆系统的统一入口。

职责：
- remember(): 写入记忆 + 自动提取标签 + 同主题去重（新覆盖旧）
- recall(): 检索记忆 + 构建上下文
- reflect(): 定期分析，生成洞察

所有外部调用方通过这个模块访问记忆系统，不需要知道内部细节。

修复记录（2026-04-07）：
- 修复 `from agent_bridge import parse_time_hint` → 改为 db_common
- 删除与 memory_retrieval.py 重复的 rewrite_query/retrieve/build_context/time_decay_weight
- remember() 加入同主题去重：写入前检查同 title 是否已存在，有则更新而非新增
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

from db_common import DB_PATH

# 全局单工作线程池，防止高并发下线程爆炸
_embed_executor = None

def _get_embed_executor():
    global _embed_executor
    if _embed_executor is None:
        from concurrent.futures import ThreadPoolExecutor
        _embed_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="embed")
    return _embed_executor


# ============ Tag Extraction ============

TASK_TYPE_KEYWORDS = {
    "coding": ["python", "javascript", "爬虫", "api", "docker", "git", "代码", "编程", "脚本"],
    "research": ["融资", "投资", "论文", "调研", "分析", "market", "报告"],
    "file_ops": ["文件", "上传", "下载", "复制", "移动", "删除", "file"],
    "reasoning": ["决策", "判断", "评估", "比较", "分析", "权衡"],
    "general": [],  # fallback
}

MODEL_KEYWORDS = {
    "kimi": ["kimi", "moonshot", "月之暗面"],
    "minimax": ["minimax", "智谱", "glm"],
    "opus": ["opus", "claude"],
    "sonnet": ["sonnet"],
    "gpt": ["gpt", "chatgpt", "openai"],
}

TECH_KEYWORDS = [
    "python", "javascript", "typescript", "rust", "go",
    "sqlite", "fastapi", "docker", "kubernetes",
    "github", "feishu", "lark", "飞书",
    "api", "http", "websocket", "grpc",
    "embedding", "vector", "rag", "llm", "bge",
    "36kr", "trending", "hackernews",
    "subagent", "agent", "workflow",
    "skill", "tool", "memory",
]


def extract_tags(content: str, task_type: str | None = None) -> list[str]:
    """从内容中自动提取标签（规则匹配，不需要LLM）。"""
    tags = []
    content_lower = content.lower()

    if task_type and task_type in TASK_TYPE_KEYWORDS:
        tags.append(task_type)

    for tt, kws in TASK_TYPE_KEYWORDS.items():
        if tt == "general":
            continue
        if any(kw in content_lower for kw in kws):
            tags.append(tt)

    for model, kws in MODEL_KEYWORDS.items():
        if any(kw in content_lower for kw in kws):
            tags.append(model)

    for kw in TECH_KEYWORDS:
        if kw in content_lower:
            tags.append(kw)

    return list(set(tags))[:10]


# ============ Session-Level Working Memory ============

class SessionMemory:
    """单次会话的短期记忆，不写入长期存储。"""
    def __init__(self):
        self.recent_decisions = []
        self.current_task = None
        self.user_preferences = {}
        self.pending_todos = []

    def add_decision(self, title: str, decision: str, decision_id: int | None = None):
        self.recent_decisions.append((decision_id, title, decision))
        if len(self.recent_decisions) > 10:
            self.recent_decisions.pop(0)

    def set_task(self, task: str):
        self.current_task = task

    def add_todo(self, todo: str):
        self.pending_todos.append(todo)

    def get_context(self, max_chars: int = 500) -> str:
        parts = []
        if self.current_task:
            parts.append(f"当前任务: {self.current_task}")
        if self.recent_decisions:
            dec_lines = [f"- {t}: {d}" for _, t, d in self.recent_decisions[-3:]]
            parts.append("最近决策:\n" + "\n".join(dec_lines))
        if self.pending_todos:
            parts.append(f"待办: {', '.join(self.pending_todos)}")
        context = "\n".join(parts)
        return context[:max_chars]


_session_memory = SessionMemory()


# ============ Dedup Helper ============

def _find_existing_by_title(title: str, table: str = "observations") -> int | None:
    """查找同 title 的已有记录 ID，用于去重（新覆盖旧）。"""
    from db_common import get_db
    db = get_db()
    row = db.execute(
        f"SELECT id FROM {table} WHERE title = ? ORDER BY created_at DESC LIMIT 1",
        (title,)
    ).fetchone()
    db.close()
    return row["id"] if row else None


def _update_observation(record_id: int, narrative: str | None, tags: str, task_type: str | None):
    """更新已有 observation 的内容（新覆盖旧）。"""
    from db_common import get_db
    db = get_db()
    db.execute(
        "UPDATE observations SET narrative = ?, tags = ?, task_type = ?, timestamp = ? WHERE id = ?",
        (narrative, tags, task_type or "", datetime.now().isoformat(), record_id)
    )
    db.commit()
    db.close()


# ============ Memory Service ============

def remember(
    content: str,
    type: str = "observation",
    title: str | None = None,
    narrative: str | None = None,
    tags: list | None = None,
    task_type: str | None = None,
    triggered_by_obs_id: int | None = None,
    supersedes_decision_id: int | None = None,
) -> dict:
    """写入记忆 + 自动提取标签 + 同主题去重（新覆盖旧）。

    流程：
    1. 自动提取标签
    2. 生成标题
    3. 检查同 title 是否已存在 → 有则更新，无则新增
    4. 更新 embedding（异步，失败不影响主流程）
    5. 如果是决策，记录到 session_memory
    """
    from memory_db import add_observation, add_decision, build_embeddings

    auto_tags = extract_tags(content, task_type)
    if tags:
        merged = list(set(auto_tags + [t for t in tags if t]))
    else:
        merged = auto_tags

    if not title:
        title = content[:40].strip()
        if len(content) > 40:
            title += "..."

    tags_str = ",".join(merged)

    try:
        if type == "decision":
            # 决策不做 title 去重（每次决策都是独立事件）
            record_id = add_decision(
                title=title,
                decision=content,
                rejected_alternatives=None,
                rationale=narrative,
                triggered_by_obs_id=triggered_by_obs_id,
                supersedes_decision_id=supersedes_decision_id,
            )
            _session_memory.add_decision(title, content, record_id)
            action = "created"
        else:
            # Route through memory_governor for dedup + lineage
            _gov_ok = False
            try:
                import sys as _sys
                _ws = str(__import__('pathlib').Path(__file__).resolve().parent.parent)
                _mods = _ws + "/modules"
                for _p in [_ws, _mods]:
                    if _p not in _sys.path:
                        _sys.path.insert(0, _p)
                from memory_governor import add_observation as _gov_add
                _gov_result = _gov_add(
                    type=type, title=title,
                    narrative=narrative or content,
                    tags=merged, task_type=task_type,
                    origin_module="memory_service",
                )
                if _gov_result.get("action") == "duplicate":
                    record_id = _gov_result["observation_id"]
                    action = "deduplicated"
                    _gov_ok = True
                elif _gov_result.get("success"):
                    record_id = _gov_result["observation_id"]
                    action = "created"
                    _gov_ok = True
            except (ImportError, Exception) as _gov_err:
                import logging as _logging
                _logging.getLogger("memory_service").warning(
                    "memory_governor unavailable, falling back to direct write: %s", _gov_err
                )

            if not _gov_ok:
                # Fallback: direct write if governor unavailable
                import logging as _logging2
                _logging2.getLogger("memory_service").warning(
                    "Writing observation without governor (no dedup/lineage): title=%s", title
                )
                existing_id = _find_existing_by_title(title, "observations")
                if existing_id:
                    _update_observation(existing_id, narrative or content, tags_str, task_type)
                    record_id = existing_id
                    action = "updated"
                else:
                    record_id = add_observation(
                        type=type, title=title,
                        narrative=narrative or content,
                        tags=merged, task_type=task_type,
                    )
                    action = "created"

        # Incremental embedding update: 线程池异步执行，防止高并发下线程爆炸
        try:
            _get_embed_executor().submit(build_embeddings)
        except Exception as embed_err:
            print(f"[memory_service] embedding update skipped: {embed_err}")

        return {"id": record_id, "tags": merged, "title": title, "action": action}

    except Exception as e:
        print(f"[memory_service] remember failed: {e}")
        return {"id": None, "error": str(e)}


def recall(
    query: str,
    context: str = "",
    tags: list | None = None,
    task_type: str | None = None,
    top_k: int = 5,
) -> str:
    """检索记忆 + 构建上下文字符串。

    直接使用 memory_retrieval 模块，不重复实现。
    """
    # Step 1: Check session memory first
    session_ctx = _session_memory.get_context()
    if session_ctx and (len(query) < 5 or query in session_ctx):
        return f"[Session Context]\n{session_ctx}\n\n"

    # Step 2: Multi-query retrieval via memory_retrieval
    from memory_retrieval import rewrite_query, retrieve, build_context
    queries = rewrite_query(query)
    candidates = retrieve(
        query_or_queries=queries,
        tags=tags,
        task_type=task_type,
        top_k=top_k,
    )

    # Step 3: Build context
    ctx = build_context(query, candidates)
    if not ctx:
        return ""

    return f"[Relevant Memory]\n{ctx}\n\n"


def reflect() -> dict:
    """定期分析：生成洞察和建议。"""
    from memory_db import get_recent, search

    recent = get_recent(days=7, limit=100)

    tag_counts = {}
    for r in recent:
        tags_str = r.get("tags", "")
        if tags_str:
            for tag in tags_str.split(","):
                tag = tag.strip()
                if tag:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1

    top_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:5]
    insights = [r for r in recent if r.get("type") in ("discovery", "lesson", "bugfix")]
    decisions = [r for r in recent if r.get("kind") == "decision" or r.get("type") == "decision"]

    return {
        "new_insights": insights[:5],
        "tags_frequency": top_tags,
        "recent_decisions": decisions[:5],
        "total_recent": len(recent),
    }


def get_session_memory() -> SessionMemory:
    return _session_memory


def clear_session_memory():
    global _session_memory
    _session_memory = SessionMemory()


# ============ CLI ============

def cli():
    import argparse
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()

    remember_p = sub.add_parser("remember", help="Write a memory")
    remember_p.add_argument("type", choices=["observation", "decision", "bugfix", "discovery", "lesson"])
    remember_p.add_argument("content")
    remember_p.add_argument("--title", default=None)
    remember_p.add_argument("--tags", default=None)
    remember_p.add_argument("--task-type", default=None)
    remember_p.set_defaults(cmd="remember")

    recall_p = sub.add_parser("recall", help="Recall memories")
    recall_p.add_argument("query")
    recall_p.add_argument("--top-k", type=int, default=5)
    recall_p.set_defaults(cmd="recall")

    reflect_p = sub.add_parser("reflect", help="Generate insights from recent memories")
    reflect_p.set_defaults(cmd="reflect")

    args = p.parse_args()

    if not hasattr(args, "cmd"):
        p.print_help()
        return

    if args.cmd == "remember":
        tags = args.tags.split(",") if args.tags else None
        result = remember(
            content=args.content,
            type=args.type,
            title=args.title,
            tags=tags,
            task_type=args.task_type,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.cmd == "recall":
        ctx = recall(args.query, top_k=args.top_k)
        print(ctx if ctx else "(no results)")

    elif args.cmd == "reflect":
        result = reflect()
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    cli()
