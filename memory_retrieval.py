#!/usr/bin/env python3
"""
Memory Retrieval — 自我进化记忆系统的检索层。

职责：纯函数检索，无副作用（无DB写入，无状态修改）。
接收 memory_store 的查询结果，进行多路检索和排序，返回加权结果。

不依赖：embedding生成、DB写入、文件IO。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta

from db_common import DB_PATH


# ============ Query Rewriting ============

# 关键词 → 同义表达映射（已删除，个人使用场景下 FTS5 + embedding 已足够）
_QUERY_EXPANSION = {}

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

    return result[:8]


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
    from memory_db import search as store_search

    # Detect time hint from original query string
    time_hint = None
    if isinstance(query_or_queries, str):
        from db_common import parse_time_hint
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


# ============ CLI ============

def cli():
    import argparse
    p = argparse.ArgumentParser(description="Memory retrieval (pure function, no DB writes)")
    p.add_argument("query", nargs="?", default=None)
    p.add_argument("--tags", default=None)
    p.add_argument("--task-type", default=None)
    p.add_argument("--time-range", default="auto")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--rewrite", action="store_true", help="Show rewritten queries")
    args = p.parse_args()

    if not args.query:
        print("Usage: memory_retrieval.py <query> [options]")
        return

    if args.rewrite:
        print("Rewritten queries:")
        for q in rewrite_query(args.query):
            print(f"  - {q}")
        print()

    results = retrieve(
        query_or_queries=args.query,
        tags=args.tags,
        task_type=args.task_type,
        time_range=args.time_range,
        top_k=args.top_k,
    )

    print(f"Retrieved {len(results)} results:")
    ctx = build_context(args.query, results)
    print(ctx)


if __name__ == "__main__":
    cli()
