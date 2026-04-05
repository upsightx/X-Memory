#!/usr/bin/env python3
"""
Memory Service — 自我进化记忆系统的统一入口。

职责：
- remember(): 写入记忆 + 自动提取标签
- recall(): 检索记忆 + 构建上下文
- reflect(): 定期分析，生成洞察

所有外部调用方通过这个模块访问记忆系统，不需要知道内部细节。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

from db_common import DB_PATH


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
    """从内容中自动提取标签（规则匹配，不需要LLM）。

    Returns:
        list of tag strings, deduped, max 10
    """
    tags = []
    content_lower = content.lower()

    if task_type and task_type in TASK_TYPE_KEYWORDS:
        tags.append(task_type)

    # Match task type
    for tt, kws in TASK_TYPE_KEYWORDS.items():
        if tt == "general":
            continue
        if any(kw in content_lower for kw in kws):
            tags.append(tt)

    # Match model
    for model, kws in MODEL_KEYWORDS.items():
        if any(kw in content_lower for kw in kws):
            tags.append(model)

    # Match tech keywords
    for kw in TECH_KEYWORDS:
        if kw in content_lower:
            tags.append(kw)

    return list(set(tags))[:10]


# ============ Session-Level Working Memory ============

class SessionMemory:
    """单次会话的短期记忆，不写入长期存储。

    用于记住当前会话内的关键信息，不需要检索就有用。
    """
    def __init__(self):
        self.recent_decisions = []   # [(id, title, decision)]
        self.current_task = None      # 当前任务描述
        self.user_preferences = {}    # 用户偏好
        self.pending_todos = []       # 本会话产生的待办

    def add_decision(self, title: str, decision: str, decision_id: int | None = None):
        self.recent_decisions.append((decision_id, title, decision))
        if len(self.recent_decisions) > 10:
            self.recent_decisions.pop(0)

    def set_task(self, task: str):
        self.current_task = task

    def add_todo(self, todo: str):
        self.pending_todos.append(todo)

    def get_context(self, max_chars: int = 500) -> str:
        """获取本会话的上下文摘要。"""
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


# Global session memory instance
_session_memory = SessionMemory()


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
    """写入记忆 + 自动提取标签。

    流程：
    1. 自动提取标签（规则）
    2. 生成标题（取内容前20字符，或用提供的）
    3. 写入 memory_store
    4. 更新 embedding（异步，写入失败不影响主流程）
    5. 如果是决策，记录到 session_memory

    Returns:
        {"id": int, "tags": list, "title": str}
    """
    from memory_store import add_observation, add_decision

    # Auto-extract tags
    auto_tags = extract_tags(content, task_type)
    if tags:
        merged = list(set(auto_tags + [t for t in tags if t]))
    else:
        merged = auto_tags

    # Generate title
    if not title:
        title = content[:40].strip()
        if len(content) > 40:
            title += "..."

    # Store
    try:
        if type == "decision":
            record_id = add_decision(
                title=title,
                decision=content,
                rejected_alternatives=None,
                rationale=narrative,
                triggered_by_obs_id=triggered_by_obs_id,
                supersedes_decision_id=supersedes_decision_id,
            )
            _session_memory.add_decision(title, content, record_id)
        else:
            record_id = add_observation(
                type=type,
                title=title,
                narrative=narrative or content,
                tags=merged,
                task_type=task_type,
            )

        # Incremental embedding update: best-effort only, never block the write path.
        try:
            from memory_store import build_embeddings
            build_embeddings()
        except Exception as embed_err:
            print(f"[memory_service] embedding update skipped: {embed_err}")

        return {"id": record_id, "tags": merged, "title": title}

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

    流程：
    1. 如果有 session_context，优先在 session_memory 中找
    2. 用 memory_retrieval 多路检索
    3. 拼接为可读上下文

    Returns:
        上下文字符串，可以直接拼到 prompt 末尾
    """

    # Step 1: Check session memory first (no retrieval needed)
    session_ctx = _session_memory.get_context()
    if session_ctx and (len(query) < 5 or query in session_ctx):
        # Short query that matches session context
        return f"[Session Context]\n{session_ctx}\n\n"

    # Step 2: Multi-query retrieval
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
    """定期分析：生成洞察和建议。

    分析最近7天的记忆，生成：
    - 高频标签
    - 新发现/教训
    - 决策趋势

    Returns:
        dict with keys: new_insights, tags_frequency, recent_decisions
    """
    from memory_store import get_recent, search

    recent = get_recent(days=7, limit=100)

    # Count tag frequency
    tag_counts = {}
    for r in recent:
        tags_str = r.get("tags", "")
        if tags_str:
            for tag in tags_str.split(","):
                tag = tag.strip()
                if tag:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1

    top_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    # Find discoveries and lessons
    insights = [r for r in recent if r.get("type") in ("discovery", "lesson", "bugfix")]

    # Recent decisions
    decisions = [r for r in recent if r.get("kind") == "decision" or r.get("type") == "decision"]

    return {
        "new_insights": insights[:5],
        "tags_frequency": top_tags,
        "recent_decisions": decisions[:5],
        "total_recent": len(recent),
    }


def get_session_memory() -> SessionMemory:
    """获取当前会话的短期记忆实例。"""
    return _session_memory


def clear_session_memory():
    """清除会话记忆（通常在会话结束时调用）。"""
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


# ============ Retrieval (merged from memory_retrieval.py) ============

# ============ Query Rewriting ============

# 关键词 → 同义表达映射（用手写规则，不用LLM调用）
_QUERY_EXPANSION = {
    "爬虫": ["web scraper", "数据采集", "抓取", "scraping", "crawl"],
    "融资": ["funding", "投资", "投资方", "轮次", "capital"],
    "模型": ["model", "llm", "大模型", "ai", "语言模型"],
    "部署": ["deploy", "docker", "服务器", "上线", "production"],
    "测试": ["test", "测试", "验证", "qa", "验证"],
    "记忆": ["memory", "记忆", "观察", "经验", "lesson"],
    "决策": ["decision", "决策", "选择", "方案", "strategy"],
    "代码": ["code", "coding", "编程", "python", "javascript"],
    "搜索": ["search", "retrieval", "检索", "查找", "query"],
    "子agent": ["subagent", "子代理", "agent", "助手", "task"],
    "飞书": ["feishu", "lark", "日历", "文档", "云文档"],
    "github": ["git", "仓库", "repo", "代码托管", "trending"],
}

# 口语化表达 → 标准表达
_INFORMAL_MAP = {
    r"上次": "",
    r"之前": "",
    r"那个": "",
    r"这个": "",
    r"我之前": "",
    r"刚才": "",
}


def rewrite_query(query: str) -> list[str]:
    """将用户查询改写为多个检索角度。

    用规则扩展词汇，不调用LLM。
    例如："上次那个爬虫" → ["爬虫", "web scraper", "数据采集", "抓取"]
    时间表达会被识别但不从查询中移除（由 retrieve() 处理时间过滤）。

    Returns:
        包含原查询 + 改写表达的列表（去重，最多5个）
    """
    if not query or len(query.strip()) < 2:
        return [query] if query else []

    # 1. 去除口语化前缀
    cleaned = query
    for pattern, replacement in _INFORMAL_MAP.items():
        cleaned = re.sub(pattern, replacement, cleaned)
    cleaned = cleaned.strip()

    # 2. 去除时间表达词（检索时由时间过滤处理，不需要作为关键词）
    time_words = r"(今天|昨天|前天|上午|下午|最近|近期|上周|这周|上个月|这个月)"
    cleaned_no_time = re.sub(time_words, "", cleaned).strip()
    if cleaned_no_time and len(cleaned_no_time) >= 2:
        cleaned = cleaned_no_time

    # 2. 收集所有表达
    expressions = {cleaned}
    query_lower = query.lower()

    for keyword, synonyms in _QUERY_EXPANSION.items():
        if keyword in query_lower:
            expressions.add(keyword)
            for syn in synonyms:
                expressions.add(syn)

    # 3. 添加原始query本身
    expressions.add(query)

    # 4. 去重，保持顺序，取最多5个
    seen = set()
    result = []
    for expr in list(expressions):
        norm = expr.lower().strip()
        if norm and norm not in seen:
            seen.add(norm)
            result.append(expr.strip())

    return result[:5]


# ============ Time Decay ============

def time_decay_weight(created_at: str | None, half_life_days: int = 30) -> float:
    """指数衰减：30天半衰期。

    一条30天前的记忆权重是0.5，60天前是0.25。
    最近7天内的记忆权重接近1.0。
    """
    if not created_at:
        return 0.5
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        now = datetime.now()
        age_days = (now - created).total_seconds() / 86400
        return 0.5 ** (age_days / half_life_days)
    except (ValueError, TypeError):
        return 0.5


# ============ Retrieval ============

def retrieve(
    query_or_queries: str | list[str],
    tags: str | list | None = None,
    task_type: str | None = None,
    time_range: str = "auto",  # "auto"=7天优先, "all"=全部, "month"=30天
    top_k: int = 5,
    min_score: float = 0.3,
) -> list[dict]:
    """多路检索 + 动态阈值 + 时间感知。

    流程：
    1. 如果是字符串，检测时间暗示并 rewrite_query 生成多查询
    2. 标签精确过滤（第一层）
    3. 时间分区（第二层，auto=根据时间暗示或7天优先）
    4. 多查询各召回一批
    5. 加权排序（语义分 × 时间衰减）
    6. 动态阈值（确保至少返回top_k，不足则降低阈值）

    Returns:
        list of dicts with keys: id, type, title, narrative, tags, task_type,
                                 score, time_weight, source
    """
    # Lazy import to avoid circular dependency at module load
    from memory_store import search as store_search

    # Detect time hint from original query string
    time_hint = None
    if isinstance(query_or_queries, str):
        from agent_bridge import parse_time_hint
        time_hint = parse_time_hint(query_or_queries)

    # Normalize queries
    if isinstance(query_or_queries, str):
        queries = rewrite_query(query_or_queries)
    else:
        queries = query_or_queries

    # Override time_range based on time hint
    if time_hint and time_range == "auto":
        days = time_hint["days_ago"]
        if days <= 1:
            time_range = "recent"  # 7 days, will catch today/yesterday
        elif days <= 7:
            time_range = "recent"
        elif days <= 30:
            time_range = "month"
        else:
            time_range = None  # all

    # Normalize tags
    if tags:
        if isinstance(tags, str):
            tags_list = [t.strip() for t in tags.split(",") if t.strip()]
        else:
            tags_list = [str(t).strip() for t in tags if t]
    else:
        tags_list = []

    # Normalize time_range
    effective_time_range = time_range  # "auto" is handled below

    all_candidates = {}  # id -> candidate dict, deduped

    # Two-pass: first try recent, then expand if needed
    time_ranges_to_try = ["recent", "month", None] if time_range == "auto" else [time_range or None]

    for tr in time_ranges_to_try:
        if len(all_candidates) >= top_k:
            break

        for q in queries:
            results = store_search(
                query=q,
                tags=tags_list if tags_list else None,
                task_type=task_type,
                time_range=tr,
                limit=20,
            )
            for r in results:
                rid = r.get("id")
                if rid is None or rid in all_candidates:
                    continue
                tw = time_decay_weight(r.get("created_at"))
                # Simple score: 1.0 for exact tag match, 0.5 for partial
                tag_score = 1.0 if not tags_list else _tag_match_score(r.get("tags", ""), tags_list)
                score = tag_score * (0.5 + 0.5 * tw)  # hybrid score
                if score < min_score:
                    continue
                r["score"] = round(score, 3)
                r["time_weight"] = round(tw, 3)
                r["source"] = q
                all_candidates[rid] = r

    # Sort by score descending
    sorted_results = sorted(all_candidates.values(), key=lambda x: x["score"], reverse=True)

    # Dynamic threshold: ensure at least top_k, even if it means lowering threshold
    if len(sorted_results) < top_k:
        # Return all we have
        return sorted_results

    # Use the score of the k-th item as threshold, at least min_score
    threshold = max(sorted_results[top_k - 1]["score"] * 0.8, min_score)
    filtered = [r for r in sorted_results if r["score"] >= threshold]
    return filtered[:top_k]


def _tag_match_score(tags_str: str, target_tags: list[str]) -> float:
    """Score how well tags_str matches target tags. 1.0=exact, 0.5=partial, 0=nomatch."""
    if not tags_str or not target_tags:
        return 0.5
    present = {t.strip().lower() for t in tags_str.split(",") if t.strip()}
    if not present:
        return 0.3
    matches = sum(1 for t in target_tags if t.lower() in present)
    if matches == len(target_tags):
        return 1.0
    elif matches > 0:
        return 0.5 + 0.3 * (matches / len(target_tags))
    return 0.2


# ============ Context Builder ============

def build_context(query: str, candidates: list[dict], max_chars: int = 1500) -> str:
    """将检索结果拼接为可读的上下文字符串。

    格式：
    [observation] type | title
      narrative...

    [decision] title
      decision...

    [observation] type | title (score=0.xx)
      ...
    """
    if not candidates:
        return ""

    lines = []
    total_chars = 0

    for c in candidates:
        score_str = f" (score={c.get('score', '?')})"
        if c.get("kind") == "decision" or c.get("type") == "decision":
            block = (
                f"[decision] {c.get('title', '')}{score_str}\n"
                f"  {c.get('decision', '')}"
            )
        else:
            obs_type = c.get("type", "observation")
            title = c.get("title", "")
            narrative = c.get("narrative", "") or ""
            block = f"[{obs_type}] {title}{score_str}\n  {narrative[:200]}"

        if total_chars + len(block) > max_chars:
            remaining = max_chars - total_chars
            if remaining > 50:
                lines.append(block[:remaining] + "...")
            break

        lines.append(block)
        total_chars += len(block)

    return "\n\n".join(lines)

