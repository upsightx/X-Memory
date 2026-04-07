#!/usr/bin/env python3
"""
Structured Memory Store — 自我进化记忆系统的存储层。

职责：CRUD + 查询。不做检索排序，不做 embedding，不做上下文拼接。

新增字段：
- observations.tags（逗号分隔字符串）
- observations.task_type（任务类型：coding/research/file_ops/reasoning）
- decisions.triggered_by_obs_id（触发该决策的观察ID）
- decisions.supersedes_decision_id（覆盖的旧决策ID）

向后兼容：原有接口保持不变，新增 search() 支持标签和时间过滤。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from db_common import DB_PATH, get_db
import hashlib
import math
import struct
import urllib.request
import urllib.error

# Import LLM config from smart_recall (same package)
from smart_recall import LLM_API_BASE, LLM_API_KEY, LLM_MODEL


# ============ LLM Description Generator ============

def _generate_description_llm(title: str, narrative: str | None) -> str:
    """Generate a one-sentence description from title + narrative via cheap LLM.

    Falls back to empty string on any failure (timeout, network, API error).
    Timeout is 15 seconds. Silently returns "" on failure — does not block writes.
    """
    if not title and not narrative:
        return ""

    prompt_parts = ["根据以下信息生成一句简短摘要（不超过50字）："]
    if title:
        prompt_parts.append(f"标题：{title}")
    if narrative:
        # Truncate narrative to avoid overly long prompts
        narrative_snippet = (narrative[:300] + "...") if len(narrative) > 300 else narrative
        prompt_parts.append(f"内容：{narrative_snippet}")
    prompt = "\n".join(prompt_parts)

    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 60,
        "temperature": 0.1,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            f"{LLM_API_BASE}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LLM_API_KEY}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            choices = body.get("choices", [])
            if choices:
                return choices[0]["message"]["content"].strip()
    except Exception:
        pass
    return ""


# ============ Schema ============

def init_db():
    """Initialize or migrate the database."""
    db = get_db()

    # Get existing columns
    obs_cols = {r[1] for r in db.execute("PRAGMA table_info(observations)").fetchall()}
    dec_cols = {r[1] for r in db.execute("PRAGMA table_info(decisions)").fetchall()}

    # Migrate observations
    if "description" not in obs_cols:
        db.execute("ALTER TABLE observations ADD COLUMN description TEXT DEFAULT ''")
    if "task_type" not in obs_cols:
        db.execute("ALTER TABLE observations ADD COLUMN task_type TEXT DEFAULT ''")
    if "tags" in obs_cols:
        # Convert JSON list to comma-separated string
        for row in db.execute("SELECT id, tags FROM observations WHERE tags IS NOT NULL").fetchall():
            row_id, tags = row
            try:
                parsed = json.loads(tags)
                if isinstance(parsed, list):
                    db.execute("UPDATE observations SET tags = ? WHERE id = ?",
                               (",".join(parsed), row_id))
            except Exception:
                pass
    db.execute("CREATE INDEX IF NOT EXISTS idx_obs_task_type ON observations(task_type)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_obs_created ON observations(created_at)")

    # Migrate observations: embedding_status
    if "embedding_status" not in obs_cols:
        db.execute("ALTER TABLE observations ADD COLUMN embedding_status INTEGER DEFAULT 0")
    dec_cols2 = {r[1] for r in db.execute("PRAGMA table_info(decisions)").fetchall()}
    if "embedding_status" not in dec_cols2:
        db.execute("ALTER TABLE decisions ADD COLUMN embedding_status INTEGER DEFAULT 0")
    db.execute("CREATE INDEX IF NOT EXISTS idx_obs_embed_status ON observations(embedding_status)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_dec_embed_status ON decisions(embedding_status)")

    # Migrate decisions
    if "triggered_by_obs_id" not in dec_cols:
        db.execute("ALTER TABLE decisions ADD COLUMN triggered_by_obs_id INTEGER DEFAULT NULL")
    if "supersedes_decision_id" not in dec_cols:
        db.execute("ALTER TABLE decisions ADD COLUMN supersedes_decision_id INTEGER DEFAULT NULL")
    db.execute("CREATE INDEX IF NOT EXISTS idx_dec_created ON decisions(created_at)")

    db.commit()
    db.close()
    print(f"[memory_store] Database migrated at {DB_PATH}")


# ============ Write ============

def add_observation(
    type: str = "change",
    title: str = "",
    narrative: str | None = None,
    facts: list | None = None,
    concepts: list | None = None,
    session_id: str | None = None,
    source: str | None = None,
    verified: bool = False,
    tags: list | str | None = None,
    task_type: str | None = None,
    description: str = "",
) -> int:
    """Add an observation.

    Types: bugfix, discovery, lesson, change, feature, refactor

    Args:
        tags: list of strings OR comma-separated string
        task_type: coding, research, file_ops, reasoning, general
        description: one-sentence summary. If empty, auto-generated via cheap LLM.
    """
    # description 不在主路径生成（避免同步 LLM 阻塞写入）
    # 留空即可，后续可由后台任务异步补充
    if not description:
        description = ""

    db = get_db()

    # Normalize tags to comma-separated string
    if tags is None:
        tags_str = ""
    elif isinstance(tags, str):
        tags_str = tags
    else:
        tags_str = ",".join(str(t).strip() for t in tags if t)

    db.execute(
        """INSERT INTO observations
           (session_id, timestamp, type, title, description, narrative, facts, concepts, source, verified, tags, task_type)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            session_id,
            datetime.now().isoformat(),
            type,
            title,
            description,
            narrative,
            json.dumps(facts, ensure_ascii=False) if facts else None,
            json.dumps(concepts, ensure_ascii=False) if concepts else None,
            source,
            1 if verified else 0,
            tags_str,
            task_type or "",
        ),
    )
    db.commit()
    rid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.close()
    return rid


def add_decision(
    title: str,
    decision: str,
    rejected_alternatives: str | list | None = None,
    rationale: str | None = None,
    triggered_by_obs_id: int | None = None,
    supersedes_decision_id: int | None = None,
) -> int:
    """Add a decision record.

    Args:
        triggered_by_obs_id: ID of the observation that triggered this decision
        supersedes_decision_id: ID of the older decision this one supersedes
    """
    db = get_db()
    if isinstance(rejected_alternatives, list):
        rejected_alts_str = json.dumps(rejected_alternatives, ensure_ascii=False)
    else:
        rejected_alts_str = rejected_alternatives

    db.execute(
        """INSERT INTO decisions
           (timestamp, title, decision, rejected_alternatives, rationale,
            triggered_by_obs_id, supersedes_decision_id)
           VALUES (?,?,?,?,?,?,?)""",
        (
            datetime.now().isoformat(),
            title,
            decision,
            rejected_alts_str,
            rationale,
            triggered_by_obs_id,
            supersedes_decision_id,
        ),
    )
    db.commit()
    rid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.close()
    return rid


def add_session_summary(
    request: str,
    learned: str | None = None,
    completed: str | None = None,
    next_steps: str | None = None,
    session_id: str | None = None,
    importance_score: float = 0.5,
) -> None:
    """Add a session summary."""
    db = get_db()
    db.execute(
        """INSERT INTO session_summaries
           (session_id, timestamp, request, learned, completed, next_steps, importance_score)
           VALUES (?,?,?,?,?,?,?)""",
        (
            session_id,
            datetime.now().isoformat(),
            request,
            learned,
            completed,
            next_steps,
            importance_score,
        ),
    )
    db.commit()
    db.close()


# ============ Query ============

def get_recent(days: int = 7, limit: int = 50) -> list[dict]:
    """Get recent observations and decisions."""
    db = get_db()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    obs = [
        dict(r)
        for r in db.execute(
            """SELECT id, 'observation' as kind, type, title, description, narrative, tags, task_type, created_at
               FROM observations WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
    ]
    dec = [
        dict(r)
        for r in db.execute(
            """SELECT id, 'decision' as kind, title, decision, rationale,
                      triggered_by_obs_id, supersedes_decision_id, created_at
               FROM decisions WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
    ]
    db.close()

    result = obs + dec
    result.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return result[:limit]


def search(
    query: str | None = None,
    type: str | None = None,
    tags: str | list | None = None,
    task_type: str | None = None,
    time_range: str | None = None,  # "recent"=7d, "month"=30d, "all"=None
    limit: int = 20,
) -> list[dict]:
    """Search observations with optional filters.

    Args:
        query: keyword search (FTS5 + LIKE)
        type: observation type filter
        tags: comma-separated string OR list of tags (AND logic)
        task_type: filter by task type
        time_range: "recent"(7d), "month"(30d), or None(all)
        limit: max results
    """
    db = get_db()
    conditions = []
    params = []

    if type:
        conditions.append("type = ?")
        params.append(type)

    if task_type:
        conditions.append("task_type = ?")
        params.append(task_type)

    if tags:
        if isinstance(tags, str):
            tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        else:
            tag_list = [str(t).strip() for t in tags if t]
        for tag in tag_list:
            conditions.append("tags LIKE ?")
            params.append(f"%{tag}%")

    if time_range == "recent":
        cutoff = (datetime.now() - timedelta(days=7)).isoformat()
        conditions.append("created_at >= ?")
        params.append(cutoff)
    elif time_range == "month":
        cutoff = (datetime.now() - timedelta(days=30)).isoformat()
        conditions.append("created_at >= ?")
        params.append(cutoff)

    where = " AND ".join(conditions) if conditions else "1=1"
    params.append(limit)

    # Keyword search via FTS5 + LIKE fallback
    if query:
        seen = set()
        results = []

        # Build query variants for backward compatibility:
        # old callers often pass natural phrases like "主流程 桥接" instead of exact keywords.
        query_variants = []
        q = str(query).strip()
        if q:
            query_variants.append(q)
            if " " in q:
                query_variants.extend([part.strip() for part in q.split() if part.strip()])

        # FTS5
        for qv in query_variants[:5]:
            try:
                for r in db.execute(f"""
                    SELECT t.id, t.type, t.title, t.narrative, t.tags, t.task_type,
                           t.created_at, 'observation' as kind
                    FROM observations_fts f
                    JOIN observations t ON f.rowid = t.id
                    WHERE observations_fts MATCH ? AND {where}
                    ORDER BY rank LIMIT ?
                """, [qv] + params[:-1] + [limit]).fetchall():
                    if r["id"] not in seen:
                        seen.add(r["id"])
                        results.append(dict(r))
            except Exception:
                pass

        # LIKE fallback (full query + split terms)
        for qv in query_variants[:5]:
            like = f"%{qv}%"
            like_where = " OR ".join(f"t.{c} LIKE ?" for c in ["title", "narrative", "tags"])
            for r in db.execute(f"""
                SELECT t.id, t.type, t.title, t.narrative, t.tags, t.task_type,
                       t.created_at, 'observation' as kind
                FROM observations t
                WHERE ({like_where}) AND {where}
                ORDER BY t.created_at DESC LIMIT ?
            """, [like, like, like] + params[:-1] + [limit]).fetchall():
                if r["id"] not in seen:
                    seen.add(r["id"])
                    results.append(dict(r))

        db.close()
        return results[:limit]
    else:
        rows = db.execute(f"""
            SELECT id, type, title, narrative, tags, task_type, created_at,
                   'observation' as kind
            FROM observations
            WHERE {where}
            ORDER BY created_at DESC LIMIT ?
        """, params).fetchall()
        db.close()
        return [dict(r) for r in rows]


def get_by_id(table: str, record_id: int) -> dict | None:
    """Get a single record by ID. table: 'observations' or 'decisions'."""
    db = get_db()
    row = db.execute(f"SELECT * FROM {table} WHERE id = ?", (record_id,)).fetchone()
    db.close()
    return dict(row) if row else None


# ============ Stats ============

def stats() -> dict:
    """Return basic memory counts."""
    db = get_db()
    s = {
        "observations": db.execute(
            "SELECT COUNT(*) FROM observations").fetchone()[0],
        "decisions": db.execute(
            "SELECT COUNT(*) FROM decisions").fetchone()[0],
        "summaries": db.execute(
            "SELECT COUNT(*) FROM session_summaries").fetchone()[0],
    }
    db.close()
    return s


# ============ CLI ============

def cli():
    import argparse
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()

    init_p = sub.add_parser("init", help="Initialize/migrate database")
    init_p.set_defaults(cmd="init")

    search_p = sub.add_parser("search", help="Search observations")
    search_p.add_argument("query", nargs="?", default=None)
    search_p.add_argument("--type", "-t", default=None)
    search_p.add_argument("--tags", default=None)
    search_p.add_argument("--task-type", default=None)
    search_p.add_argument("--time-range", default=None)
    search_p.add_argument("--limit", "-n", type=int, default=10)
    search_p.set_defaults(cmd="search")

    args = p.parse_args()

    if not hasattr(args, "cmd"):
        p.print_help()
        return

    if args.cmd == "init":
        init_db()
    elif args.cmd == "search":
        results = search(
            query=args.query,
            type=args.type,
            tags=args.tags,
            task_type=args.task_type,
            time_range=args.time_range,
            limit=args.limit,
        )
        for r in results:
            print(f"[{r['type']}] {r['title'][:60]}")
            if r.get("tags"):
                print(f"  tags: {r['tags']}")


if __name__ == "__main__":
    cli()


# ============ Embedding & Semantic Search (merged from memory_embedding.py) ============

SILICONFLOW_API_KEY = os.environ.get("SILICONFLOW_API_KEY", "")
SILICONFLOW_ENDPOINT = "https://api.siliconflow.cn/v1/embeddings"
EMBED_MODEL = "BAAI/bge-m3"
EMBED_DIM = 1024
EMBED_BATCH_SIZE = 20


def _text_hash(text: str) -> str:
    """SHA256 hash of text for change detection."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pack_embedding(vec: list[float]) -> bytes:
    """Pack a list of floats into a BLOB using struct (float32)."""
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack_embedding(blob: bytes) -> list[float]:
    """Unpack a BLOB back into a list of floats."""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def embed_text(texts: list[str]) -> list[list[float]]:
    """Call SiliconFlow BGE-M3 API to get embeddings.

    Args:
        texts: list[str] — texts to embed

    Returns:
        list[list[float]] — one embedding per input text
    """
    if not texts:
        return []

    if not SILICONFLOW_API_KEY:
        print("Warning: SILICONFLOW_API_KEY not set, skipping embedding")
        return []

    all_embeddings = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i:i + EMBED_BATCH_SIZE]
        payload = json.dumps({
            "model": EMBED_MODEL,
            "input": batch,
        }).encode("utf-8")

        req = urllib.request.Request(
            SILICONFLOW_ENDPOINT,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {SILICONFLOW_API_KEY}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        sorted_data = sorted(body["data"], key=lambda x: x["index"])
        all_embeddings.extend([item["embedding"] for item in sorted_data])

    return all_embeddings


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors. Pure Python, no numpy."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def build_embeddings():
    """增量构建 embedding，只处理 embedding_status=0 的记录。

    优化（2026-04-07）：
    - 原来全量扫描所有记录做 hash 对比，数据量大时极慢
    - 现在只查 embedding_status=0 的记录，写入后标记为 1
    - 避免了 O(N) 全表扫描，性能随数据增长保持稳定
    """
    db = get_db()

    tasks = []  # (source_table, source_id, text, text_hash)

    # 只查未向量化的记录
    for row in db.execute(
        "SELECT id, title, narrative, facts FROM observations WHERE COALESCE(embedding_status, 0) = 0"
    ).fetchall():
        parts = [row["title"] or ""]
        if row["narrative"]:
            parts.append(row["narrative"])
        if row["facts"]:
            parts.append(row["facts"])
        text = "\n".join(parts)
        tasks.append(("observations", row["id"], text, _text_hash(text)))

    for row in db.execute(
        "SELECT id, title, decision, rationale FROM decisions WHERE COALESCE(embedding_status, 0) = 0"
    ).fetchall():
        parts = [row["title"] or ""]
        if row["decision"]:
            parts.append(row["decision"])
        if row["rationale"]:
            parts.append(row["rationale"])
        text = "\n".join(parts)
        tasks.append(("decisions", row["id"], text, _text_hash(text)))

    if not tasks:
        db.close()
        return

    to_embed = tasks  # 全部都是新记录，无需 hash 对比

    print(f"Embedding {len(to_embed)} records...")
    texts_to_embed = [t[2] for t in to_embed]
    vectors = embed_text(texts_to_embed)

    for (source_table, source_id, text, th), vec in zip(to_embed, vectors):
        blob = _pack_embedding(vec)
        db.execute("""
            INSERT INTO embeddings (source_table, source_id, text_hash, embedding)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_table, source_id) DO UPDATE SET
                text_hash = excluded.text_hash,
                embedding = excluded.embedding,
                created_at = datetime('now')
        """, (source_table, source_id, th, blob))
        # 标记为已向量化，避免下次重复处理
        db.execute(
            f"UPDATE {source_table} SET embedding_status = 1 WHERE id = ?",
            (source_id,)
        )

    db.commit()
    db.close()
    print(f"Done. Embedded {len(to_embed)} records.")


def semantic_search(query: str, limit: int = 10) -> list[dict]:
    """Semantic search using cosine similarity against stored embeddings.

    Returns:
        list[dict] with keys: source_table, source_id, title, timestamp, score
    """
    query_embeddings = embed_text([query])
    if not query_embeddings:
        print("Warning: semantic_search skipped because embeddings are unavailable")
        return []
    query_vec = query_embeddings[0]

    db = get_db()
    rows = db.execute("SELECT source_table, source_id, embedding FROM embeddings").fetchall()

    scored = []
    for row in rows:
        vec = _unpack_embedding(row["embedding"])
        score = _cosine_similarity(query_vec, vec)
        scored.append((score, row["source_table"], row["source_id"]))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:limit]

    results = []
    for score, source_table, source_id in top:
        if source_table == "observations":
            r = db.execute("SELECT title, timestamp FROM observations WHERE id = ?", (source_id,)).fetchone()
        else:
            r = db.execute("SELECT title, timestamp FROM decisions WHERE id = ?", (source_id,)).fetchone()
        if r:
            results.append({
                "source_table": source_table,
                "source_id": source_id,
                "title": r["title"],
                "timestamp": r["timestamp"],
                "score": round(score, 4),
            })

    db.close()
    return results
