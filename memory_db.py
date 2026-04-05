#!/usr/bin/env python3
"""
Structured Memory Database for AI Agents.

Core value: remember WHY, not just WHAT.
- Decisions with rejected alternatives and rationale
- Observations with type classification
- Session summaries for continuity
- Dual-path search (FTS5 + LIKE) for CJK/mixed content

Zero dependencies. Python 3.8+ and SQLite only.

Note: Embedding and context-builder functions have been extracted to
memory_embedding.py and memory_context.py respectively.
They are re-exported here for backward compatibility.
"""
from __future__ import annotations

import sqlite3
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from db_common import DB_PATH, get_db


def init_db():
    """Initialize the database. Safe to call multiple times."""
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            timestamp TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'change',
            title TEXT NOT NULL,
            narrative TEXT,
            facts TEXT,
            concepts TEXT,
            source TEXT,
            verified INTEGER DEFAULT 0,
            tags TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            title TEXT NOT NULL,
            decision TEXT NOT NULL,
            rejected_alternatives TEXT,
            rationale TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS session_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            timestamp TEXT NOT NULL,
            request TEXT,
            learned TEXT,
            completed TEXT,
            next_steps TEXT,
            importance_score REAL DEFAULT 0.5,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS task_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT NOT NULL,
            model TEXT NOT NULL,
            success BOOLEAN NOT NULL,
            description TEXT,
            notes TEXT,
            tags TEXT,
            critic_score INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
            title, narrative, facts, concepts,
            content=observations, content_rowid=id
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS decisions_fts USING fts5(
            title, decision, rejected_alternatives, rationale,
            content=decisions, content_rowid=id
        );

        CREATE TRIGGER IF NOT EXISTS obs_fts_insert AFTER INSERT ON observations BEGIN
            INSERT INTO observations_fts(rowid, title, narrative, facts, concepts)
            VALUES (new.id, new.title, new.narrative, new.facts, new.concepts);
        END;

        CREATE TRIGGER IF NOT EXISTS obs_fts_delete AFTER DELETE ON observations BEGIN
            INSERT INTO observations_fts(observations_fts, rowid, title, narrative, facts, concepts)
            VALUES ('delete', old.id, old.title, old.narrative, old.facts, old.concepts);
        END;

        CREATE TRIGGER IF NOT EXISTS dec_fts_insert AFTER INSERT ON decisions BEGIN
            INSERT INTO decisions_fts(rowid, title, decision, rejected_alternatives, rationale)
            VALUES (new.id, new.title, new.decision, new.rejected_alternatives, new.rationale);
        END;

        CREATE TRIGGER IF NOT EXISTS dec_fts_delete AFTER DELETE ON decisions BEGIN
            INSERT INTO decisions_fts(decisions_fts, rowid, title, decision, rejected_alternatives, rationale)
            VALUES ('delete', old.id, old.title, old.decision, old.rejected_alternatives, old.rationale);
        END;

        CREATE TRIGGER IF NOT EXISTS obs_fts_update AFTER UPDATE ON observations BEGIN
            INSERT INTO observations_fts(observations_fts, rowid, title, narrative, facts, concepts)
            VALUES ('delete', old.id, old.title, old.narrative, old.facts, old.concepts);
            INSERT INTO observations_fts(rowid, title, narrative, facts, concepts)
            VALUES (new.id, new.title, new.narrative, new.facts, new.concepts);
        END;

        CREATE TRIGGER IF NOT EXISTS dec_fts_update AFTER UPDATE ON decisions BEGIN
            INSERT INTO decisions_fts(decisions_fts, rowid, title, decision, rejected_alternatives, rationale)
            VALUES ('delete', old.id, old.title, old.decision, old.rejected_alternatives, old.rationale);
            INSERT INTO decisions_fts(rowid, title, decision, rejected_alternatives, rationale)
            VALUES (new.id, new.title, new.decision, new.rejected_alternatives, new.rationale);
        END;

        CREATE TABLE IF NOT EXISTS embeddings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_table TEXT NOT NULL,
            source_id INTEGER NOT NULL,
            text_hash TEXT NOT NULL,
            embedding BLOB NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(source_table, source_id)
        );
    """)
    db.commit()
    db.close()
    print(f"Database initialized at {DB_PATH}")


# ============ Write ============

def add_observation(type, title, narrative=None, facts=None, concepts=None,
                    session_id=None, source=None, verified=False, tags=None):
    """Add an observation. Types: decision, bugfix, feature, refactor, discovery, change"""
    db = get_db()
    db.execute(
        "INSERT INTO observations (session_id, timestamp, type, title, narrative, facts, concepts, source, verified, tags) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (session_id, datetime.now().isoformat(), type, title, narrative,
         json.dumps(facts, ensure_ascii=False) if facts else None,
         json.dumps(concepts, ensure_ascii=False) if concepts else None,
         source, 1 if verified else 0, json.dumps(tags, ensure_ascii=False) if tags else None)
    )
    db.commit()
    rid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.close()
    return rid


def add_decision(title, decision, rejected_alternatives=None, rationale=None):
    """Add a decision record. The key: record what you rejected and why."""
    db = get_db()
    db.execute(
        "INSERT INTO decisions (timestamp, title, decision, rejected_alternatives, rationale) VALUES (?,?,?,?,?)",
        (datetime.now().isoformat(), title, decision,
         json.dumps(rejected_alternatives, ensure_ascii=False) if rejected_alternatives else None,
         rationale)
    )
    db.commit()
    rid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.close()
    return rid


def add_session_summary(request, learned=None, completed=None, next_steps=None,
                        session_id=None, importance_score=0.5):
    """Add a session summary for continuity across sessions."""
    db = get_db()
    db.execute(
        "INSERT INTO session_summaries (session_id, timestamp, request, learned, completed, next_steps, importance_score) VALUES (?,?,?,?,?,?,?)",
        (session_id, datetime.now().isoformat(), request, learned, completed, next_steps, importance_score)
    )
    db.commit()
    db.close()


# ============ Search (dual-path: FTS5 + LIKE) ============

def _dual_search(db, fts_table, join_table, select_cols, fts_cols, like_cols, query, limit):
    """FTS5 for English tokens, LIKE fallback for CJK/mixed content. Merged, deduped."""
    seen = set()
    results = []

    # Path 1: FTS5
    try:
        for r in db.execute(f"""
            SELECT {select_cols} FROM {fts_table} f
            JOIN {join_table} t ON f.rowid = t.id
            WHERE {fts_table} MATCH ? ORDER BY rank LIMIT ?
        """, (query, limit)).fetchall():
            if r['id'] not in seen:
                seen.add(r['id'])
                results.append(dict(r))
    except Exception:
        pass

    # Path 2: LIKE
    like = f"%{query}%"
    where = " OR ".join(f"{c} LIKE ?" for c in like_cols)
    params = [like] * len(like_cols) + [limit]
    for r in db.execute(f"""
        SELECT {select_cols} FROM {join_table} t
        WHERE {where} ORDER BY timestamp DESC LIMIT ?
    """, params).fetchall():
        if r['id'] not in seen:
            seen.add(r['id'])
            results.append(dict(r))

    return results[:limit]


def search(query=None, type=None, limit=20):
    """Search observations. Returns id, type, title, narrative, facts, timestamp."""
    db = get_db()
    if query:
        results = _dual_search(db,
            fts_table="observations_fts", join_table="observations",
            select_cols="t.id, t.type, t.title, t.narrative, t.facts, t.timestamp",
            fts_cols=["title", "narrative", "facts", "concepts"],
            like_cols=["title", "narrative", "facts", "concepts"],
            query=query, limit=limit)
    else:
        where = "WHERE type = ?" if type else ""
        params = [type, limit] if type else [limit]
        results = [dict(r) for r in db.execute(f"""
            SELECT id, type, title, narrative, facts, timestamp FROM observations
            {where} ORDER BY timestamp DESC LIMIT ?
        """, params).fetchall()]
    db.close()

    # Auto-record LRU access for search results
    if results:
        try:
            from memory_lru import record_access
            for r in results[:5]:
                record_access(r["id"], "observations")
        except Exception:
            pass

    return results


def search_decisions(query=None, limit=20):
    """Search decisions."""
    db = get_db()
    if query:
        results = _dual_search(db,
            fts_table="decisions_fts", join_table="decisions",
            select_cols="t.id, t.title, t.decision, t.rationale, t.rejected_alternatives, t.timestamp",
            fts_cols=["title", "decision", "rejected_alternatives", "rationale"],
            like_cols=["title", "decision", "rejected_alternatives", "rationale"],
            query=query, limit=limit)
    else:
        results = [dict(r) for r in db.execute(
            "SELECT * FROM decisions ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()]
    db.close()

    # Auto-record LRU access for search results
    if results:
        try:
            from memory_lru import record_access
            for r in results[:5]:
                record_access(r["id"], "decisions")
        except Exception:
            pass

    return results


def get(id):
    """Get full observation by id."""
    db = get_db()
    row = db.execute("SELECT * FROM observations WHERE id = ?", (id,)).fetchone()
    db.close()
    return dict(row) if row else None


def stats():
    """Database statistics."""
    db = get_db()
    r = {
        "observations": db.execute("SELECT COUNT(*) FROM observations").fetchone()[0],
        "decisions": db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],
        "summaries": db.execute("SELECT COUNT(*) FROM session_summaries").fetchone()[0],
        "by_type": {r["type"]: r["cnt"] for r in db.execute(
            "SELECT type, COUNT(*) as cnt FROM observations GROUP BY type ORDER BY cnt DESC"
        ).fetchall()}
    }
    db.close()
    return r


def count_by_type(table="observations"):
    """Count records by type for the given table."""
    valid_tables = {"observations", "decisions", "session_summaries"}
    if table not in valid_tables:
        raise ValueError(f"Invalid table: {table}. Valid: {valid_tables}")

    db = get_db()
    if table == "observations":
        results = {r["type"]: r["cnt"] for r in db.execute(
            "SELECT type, COUNT(*) as cnt FROM observations GROUP BY type ORDER BY cnt DESC"
        ).fetchall()}
    elif table == "decisions":
        results = {"total": db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]}
    else:
        results = {"total": db.execute("SELECT COUNT(*) FROM session_summaries").fetchone()[0]}
    db.close()
    return results


def recent_by_days(days=7, table="observations"):
    """Get records from the last N days."""
    valid_tables = {"observations", "decisions", "session_summaries"}
    if table not in valid_tables:
        raise ValueError(f"Invalid table: {table}. Valid: {valid_tables}")

    db = get_db()
    cutoff = datetime.now() - timedelta(days=days)

    if table == "observations":
        results = [dict(r) for r in db.execute("""
            SELECT id, session_id, timestamp, type, title, narrative, source, verified, tags
            FROM observations WHERE timestamp >= ? ORDER BY timestamp DESC
        """, (cutoff.isoformat(),)).fetchall()]
    elif table == "decisions":
        results = [dict(r) for r in db.execute("""
            SELECT id, timestamp, title, decision, rejected_alternatives, rationale
            FROM decisions WHERE timestamp >= ? ORDER BY timestamp DESC
        """, (cutoff.isoformat(),)).fetchall()]
    else:
        results = [dict(r) for r in db.execute("""
            SELECT id, session_id, timestamp, request, learned, completed, next_steps, importance_score
            FROM session_summaries WHERE timestamp >= ? ORDER BY timestamp DESC
        """, (cutoff.isoformat(),)).fetchall()]
    db.close()
    return results


def import_json(data):
    """Import from JSON. Format: {observations: [...], decisions: [...], summary: "..."}"""
    imported = {"observations": 0, "decisions": 0, "summaries": 0}
    for obs in data.get("observations", []):
        add_observation(obs.get("type", "change"), obs["title"],
                        obs.get("narrative"), obs.get("facts"), obs.get("concepts"))
        imported["observations"] += 1
    for dec in data.get("decisions", []):
        add_decision(dec["title"], dec["decision"],
                     dec.get("rejected_alternatives"), dec.get("rationale"))
        imported["decisions"] += 1
    if data.get("summary"):
        add_session_summary(request=data["summary"])
        imported["summaries"] += 1
    return imported


# ============ Backward-compatible re-exports ============
# These were extracted to separate modules but are re-exported here
# so existing code that does `from memory_db import X` still works.

# Bridge old public entrypoints to the restored v6 memory stack where safe.
# Keep signatures backward-compatible so existing callers continue to work.

def remember(content, type="observation", title=None, narrative=None, tags=None,
             task_type=None, triggered_by_obs_id=None, supersedes_decision_id=None):
    from memory_service import remember as _remember
    return _remember(content=content, type=type, title=title, narrative=narrative,
                     tags=tags, task_type=task_type,
                     triggered_by_obs_id=triggered_by_obs_id,
                     supersedes_decision_id=supersedes_decision_id)


def recall(query, context="", tags=None, task_type=None, top_k=5):
    from memory_service import recall as _recall
    return _recall(query=query, context=context, tags=tags, task_type=task_type, top_k=top_k)


def add_observation(type, title, narrative=None, facts=None, concepts=None,
                    session_id=None, source=None, verified=False, tags=None, task_type=None):
    from memory_store import add_observation as _add_observation
    return _add_observation(type=type, title=title, narrative=narrative, facts=facts,
                            concepts=concepts, session_id=session_id, source=source,
                            verified=verified, tags=tags, task_type=task_type)


def add_decision(title, decision, rejected_alternatives=None, rationale=None,
                 triggered_by_obs_id=None, supersedes_decision_id=None):
    from memory_store import add_decision as _add_decision
    return _add_decision(title=title, decision=decision,
                         rejected_alternatives=rejected_alternatives,
                         rationale=rationale,
                         triggered_by_obs_id=triggered_by_obs_id,
                         supersedes_decision_id=supersedes_decision_id)


def search(query=None, type=None, limit=20, tags=None, task_type=None, time_range=None):
    from memory_store import search as _search
    return _search(query=query, type=type, tags=tags, task_type=task_type,
                   time_range=time_range, limit=limit)


def init_v6_stack():
    from memory_store import init_db as _init_v6
    return _init_v6()


def embed_text(texts):
    from memory_store import embed_text as _embed
    return _embed(texts)

def build_embeddings():
    from memory_store import build_embeddings as _build
    return _build()

def semantic_search(query, limit=10):
    from memory_store import semantic_search as _search
    return _search(query, limit)

def search_with_context(query, max_chars=6000, top_per_group=3, mode="keyword"):
    from memory_service import recall
    return recall(query, top_k=top_per_group)

def search_with_metadata(query, max_chars=6000, top_per_group=3):
    from memory_service import recall
    return recall(query, top_k=top_per_group)


# ============ CLI ============

def main():
    if len(sys.argv) < 2:
        print("""Self-Evolution Memory Database

Usage: memory_db.py <command> [args]

  init                                    Initialize database
  add <type> <title> [narrative]          Add observation
  decision <title> <decision> [rejected] [rationale]  Add decision
  search [query]                          Search observations
  decisions [query]                       Search decisions
  get <id>                                Get full observation
  stats                                   Statistics
  import <json_file>                      Import from JSON
  embed                                   Build/update all embeddings
  semantic <query>                        Semantic search
""")
        return

    cmd = sys.argv[1]

    if cmd == "init":
        init_db()
    elif cmd == "add":
        if len(sys.argv) < 4:
            print("Usage: add <type> <title> [narrative]")
            return
        rid = add_observation(sys.argv[2], sys.argv[3],
                              sys.argv[4] if len(sys.argv) > 4 else None)
        print(f"#{rid} [{sys.argv[2]}] {sys.argv[3]}")
    elif cmd == "decision":
        if len(sys.argv) < 4:
            print("Usage: decision <title> <decision> [rejected] [rationale]")
            return
        rid = add_decision(sys.argv[2], sys.argv[3],
                           [sys.argv[4]] if len(sys.argv) > 4 else None,
                           sys.argv[5] if len(sys.argv) > 5 else None)
        print(f"Decision #{rid}: {sys.argv[2]}")
    elif cmd == "search":
        for r in search(sys.argv[2] if len(sys.argv) > 2 else None):
            print(f"  #{r['id']} [{r['type']}] {r['title']}")
            if r.get('narrative'):
                print(f"    {r['narrative']}")
    elif cmd == "decisions":
        for r in search_decisions(sys.argv[2] if len(sys.argv) > 2 else None):
            print(f"  #{r['id']} {r['title']}: {r['decision']}")
            if r.get('rejected_alternatives'):
                print(f"    rejected: {r['rejected_alternatives']}")
            if r.get('rationale'):
                print(f"    why: {r['rationale']}")
    elif cmd == "get":
        r = get(int(sys.argv[2]))
        print(json.dumps(r, indent=2, ensure_ascii=False, default=str) if r else "Not found")
    elif cmd == "stats":
        print(json.dumps(stats(), indent=2))
    elif cmd == "import":
        with open(sys.argv[2]) as f:
            print(f"Imported: {import_json(json.load(f))}")
    elif cmd == "embed":
        build_embeddings()
    elif cmd == "semantic":
        if len(sys.argv) < 3:
            print("Usage: semantic <query>")
            return
        results = semantic_search(sys.argv[2])
        if not results:
            print("No results. Run 'embed' first to build embeddings.")
            return
        for r in results:
            print(f"  [{r['source_table']}#{r['source_id']}] (score={r['score']}) {r['title']}")
    else:
        print(f"Unknown: {cmd}")


if __name__ == "__main__":
    main()


# ============ Class-based compatibility shim ============
# Some callers do `from memory_db import MemoryDB`. This wraps the functional API.

class MemoryDB:
    """Thin wrapper around the module-level functional API for class-based callers."""

    def add_observation(self, type, title, narrative=None, facts=None, concepts=None, **kwargs):
        return add_observation(type, title, narrative=narrative, facts=facts, concepts=concepts, **kwargs)

    def add_decision(self, title, decision, rejected_alternatives=None, rationale=None, **kwargs):
        return add_decision(title, decision, rejected_alternatives=rejected_alternatives, rationale=rationale, **kwargs)

    def search(self, query=None, type=None, limit=20, **kwargs):
        return search(query=query, type=type, limit=limit, **kwargs)

    def count(self, table="observations"):
        return count_by_type(table)

    def stats(self):
        return stats()

    def get(self, id):
        return get(id)


# ============ Task Outcomes ============

def add_task_outcome(task_type, model, success, description="", notes="", tags="", critic_score=None):
    """记录任务执行结果。

    Returns:
        outcome_id (int)
    """
    db = get_db()
    cursor = db.execute(
        "INSERT INTO task_outcomes (task_type, model, success, description, notes, tags, critic_score) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_type, model, success, description, notes, tags, critic_score),
    )
    db.commit()
    return cursor.lastrowid
