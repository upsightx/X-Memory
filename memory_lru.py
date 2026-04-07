#!/usr/bin/env python3
"""
LRU Memory Cache Strategy Module.

Track access frequency for memories, identify hot/cold records,
suggest archival candidates, and generate access heatmaps.

Zero dependencies. Python 3.8+ and SQLite only.
"""

from __future__ import annotations

import re
import sqlite3
import json
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path

from db_common import DB_PATH, get_db as _get_db_common

SUPPORTED_TABLES = ("observations", "decisions")

# Safe table name mapping — prevents SQL injection via f-string table names
_SAFE_TABLE = {t: t for t in SUPPORTED_TABLES}


def _safe_table(table: str) -> str:
    """Validate and return safe table name. Raises ValueError if invalid."""
    if table not in _SAFE_TABLE:
        raise ValueError(f"Invalid table: {table}. Valid: {SUPPORTED_TABLES}")
    return _SAFE_TABLE[table]


def _get_db(db_path=None):
    if db_path:
        db = sqlite3.connect(str(db_path))
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db
    return _get_db_common()


# 模块级标志：ensure_columns 只在进程生命周期内执行一次
_columns_ensured = False


def ensure_columns(db_path=None):
    """Idempotently add access_count and last_accessed columns to both tables.
    
    优化（2026-04-07）：使用模块级标志，避免在热路径上重复执行 ALTER TABLE。
    首次调用时执行迁移，后续调用直接返回。
    """
    global _columns_ensured
    if _columns_ensured:
        return
    db = _get_db(db_path)
    # 检查列是否已存在，如果已存在则跳过 ALTER TABLE
    all_exist = True
    for table in SUPPORTED_TABLES:
        tbl = _safe_table(table)
        existing_cols = {r[1] for r in db.execute(f"PRAGMA table_info({tbl})").fetchall()}
        for col, typedef in [("access_count", "INTEGER DEFAULT 0"), ("last_accessed", "TEXT")]:
            if col not in existing_cols:
                all_exist = False
                try:
                    db.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {typedef}")
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
    if not all_exist:
        db.commit()
    db.close()
    _columns_ensured = True


def record_access(record_id, table="observations", db_path=None):
    """Record one access, incrementing access_count and updating last_accessed."""
    tbl = _safe_table(table)
    db = _get_db(db_path)
    ensure_columns(db_path)
    now = datetime.now().isoformat()
    cur = db.execute(
        f"UPDATE {tbl} SET access_count = COALESCE(access_count, 0) + 1, last_accessed = ? WHERE id = ?",
        (now, record_id),
    )
    db.commit()
    updated = cur.rowcount
    db.close()
    return updated > 0


def get_hot_memories(limit=20, db_path=None):
    """Get most frequently accessed memories across observations and decisions."""
    db = _get_db(db_path)
    ensure_columns(db_path)
    results = []
    for table in SUPPORTED_TABLES:
        tbl = _safe_table(table)
        title_col = "title"
        rows = db.execute(
            f"SELECT id, '{tbl}' as tbl, {title_col}, "
            f"COALESCE(access_count, 0) as access_count, last_accessed, created_at "
            f"FROM {tbl} WHERE COALESCE(access_count, 0) > 0 "
            f"ORDER BY access_count DESC LIMIT ?",
            (limit,),
        ).fetchall()
        for r in rows:
            results.append({
                "id": r["id"],
                "table": r["tbl"],
                "title": r[title_col],
                "access_count": r["access_count"],
                "last_accessed": r["last_accessed"],
                "created_at": r["created_at"],
            })
    results.sort(key=lambda x: x["access_count"], reverse=True)
    db.close()
    return results[:limit]


def get_cold_memories(days_unused=30, limit=50, db_path=None):
    """Get memories not accessed in days_unused days, excluding records created in last 7 days."""
    db = _get_db(db_path)
    ensure_columns(db_path)
    cutoff = (datetime.now() - timedelta(days=days_unused)).isoformat()
    recent_cutoff = (datetime.now() - timedelta(days=7)).isoformat()
    results = []
    for table in SUPPORTED_TABLES:
        tbl = _safe_table(table)
        rows = db.execute(
            f"SELECT id, '{tbl}' as tbl, title, "
            f"COALESCE(access_count, 0) as access_count, last_accessed, created_at "
            f"FROM {tbl} "
            f"WHERE (last_accessed IS NULL OR last_accessed < ?) "
            f"AND created_at < ? "
            f"ORDER BY created_at ASC LIMIT ?",
            (cutoff, recent_cutoff, limit),
        ).fetchall()
        for r in rows:
            results.append({
                "id": r["id"],
                "table": r["tbl"],
                "title": r["title"],
                "access_count": r["access_count"],
                "last_accessed": r["last_accessed"],
                "created_at": r["created_at"],
            })
    results.sort(key=lambda x: x["created_at"] or "")
    db.close()
    return results[:limit]


def suggest_archive(days_unused=30, db_path=None):
    """Suggest cold memories for archival."""
    return get_cold_memories(days_unused=days_unused, limit=50, db_path=db_path)


# ============ Archive Management (File-based + DB-marker) ============

def _archive_dir():
    """Return the archive directory path (memory/archive/)."""
    base = Path(__file__).resolve().parent
    archive = base / "archive"
    archive.mkdir(exist_ok=True)
    return archive


def auto_archive(days_unused=30, min_age_days=60, db_path=None):
    """Automatically archive cold memories.

    Criteria: last_accessed < days_unused ago AND created_at < min_age_days ago.

    For each candidate:
      1. Generates a compressed summary file in memory/archive/
      2. Marks the DB record as archived (archived=1)
      3. Logs a warning on failure (never blocks)

    Returns:
        list of archived record dicts (id, table, title)
    """
    import sys, warnings

    archive_dir = _archive_dir()
    now = datetime.now()
    cutoff_access = (now - timedelta(days=days_unused)).isoformat()
    cutoff_created = (now - timedelta(days=min_age_days)).isoformat()

    db = _get_db(db_path)
    ensure_columns(db_path)

    # Ensure archived column exists
    for table in SUPPORTED_TABLES:
        tbl = _safe_table(table)
        try:
            db.execute(f"ALTER TABLE {tbl} ADD COLUMN archived INTEGER DEFAULT 0")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

    archived = []
    for table in SUPPORTED_TABLES:
        tbl = _safe_table(table)
        title_col = "title"
        rows = db.execute(
            f"SELECT id, {title_col}, created_at, last_accessed FROM {tbl} "
            f"WHERE archived = 0 "
            f"AND (last_accessed IS NULL OR last_accessed < ?) "
            f"AND created_at < ?",
            (cutoff_access, cutoff_created),
        ).fetchall()

        for r in rows:
            record_id = r["id"]
            title = r[title_col] or f"{table}_{record_id}"
            safe_name = f"{table}_{record_id}_{title[:30].replace('/', '_').replace(' ', '_')}.summary.txt"
            summary_path = archive_dir / safe_name

            try:
                # Write compressed summary
                summary_content = (
                    f"[{table}#{record_id}] {title}\n"
                    f"created: {r['created_at']}\n"
                    f"last_accessed: {r['last_accessed']}\n"
                    f"archived_at: {now.isoformat()}\n"
                )
                with open(summary_path, "w", encoding="utf-8") as f:
                    f.write(summary_content)

                # Mark in DB
                db.execute(f"UPDATE {tbl} SET archived = 1 WHERE id = ?", (record_id,))

                archived.append({
                    "id": record_id,
                    "table": table,
                    "title": title,
                    "summary_path": str(summary_path),
                })
            except Exception as e:
                warnings.warn(f"[memory_lru] Failed to archive {table}#{record_id}: {e}")

    db.commit()
    db.close()
    return archived


def thaw_memory(filename: str, db_path=None):
    """Restore an archived memory from archive back to active.

    filename: the .summary.txt filename in memory/archive/ that was created
              during archival (or the original record identifier like "observations_42").

    Returns:
        dict with keys: restored (bool), record_id, table, error
    """
    import sys

    archive_dir = _archive_dir()

    # Resolve filename → summary file path
    summary_file = archive_dir / filename
    if not summary_file.exists():
        # Try to find by prefix (table_id pattern)
        for f in archive_dir.iterdir():
            if filename in f.name or f.stem.startswith(filename):
                summary_file = f
                break

    if not summary_file.exists():
        return {"restored": False, "error": f"Archive file not found: {filename}"}

    # Parse summary to get table and id
    record_id = None
    table = None
    try:
        content = summary_file.read_text(encoding="utf-8")
        # e.g. "[observations#42] Some Title"
        m = re.search(r"\[(\w+)#(\d+)\]", content)
        if m:
            table = m.group(1)
            record_id = int(m.group(2))
    except Exception as e:
        return {"restored": False, "error": f"Failed to parse summary: {e}"}

    if not record_id or not table:
        return {"restored": False, "error": "Could not extract record id/table from summary"}

    if table not in SUPPORTED_TABLES:
        return {"restored": False, "error": f"Unknown table: {table}"}

    db = _get_db(db_path)
    ensure_columns(db_path)

    # Un-archive in DB
    tbl = _safe_table(table)
    cur = db.execute(f"UPDATE {tbl} SET archived = 0, last_accessed = ? WHERE id = ?",
                     (datetime.now().isoformat(), record_id))
    db.commit()
    updated = cur.rowcount > 0
    db.close()

    if updated:
        # Optionally remove the summary file (or keep it for audit)
        try:
            summary_file.unlink()
        except Exception:
            pass  # Non-fatal

    return {
        "restored": updated,
        "record_id": record_id,
        "table": table,
        "error": None if updated else "Record not found in DB",
    }


def memory_heatmap(db_path=None):
    """Return access heatmap data grouped by type and by month."""
    db = _get_db(db_path)
    ensure_columns(db_path)

    # by_type: sum access_count per observation type
    by_type = {}
    for r in db.execute(
        "SELECT type, SUM(COALESCE(access_count, 0)) as total "
        "FROM observations GROUP BY type"
    ).fetchall():
        by_type[r["type"]] = r["total"]

    # by_month: sum access_count per month across both tables
    by_month = {}
    for table in SUPPORTED_TABLES:
        tbl = _safe_table(table)
        for r in db.execute(
            f"SELECT SUBSTR(last_accessed, 1, 7) as month, SUM(COALESCE(access_count, 0)) as total "
            f"FROM {tbl} WHERE last_accessed IS NOT NULL GROUP BY month"
        ).fetchall():
            m = r["month"]
            if m:
                by_month[m] = by_month.get(m, 0) + r["total"]

    db.close()
    return {"by_type": by_type, "by_month": by_month}


# ============ CLI ============

def _cli():
    usage = """LRU Memory Cache Strategy

Usage: memory_lru.py <command> [args]

  access <table> <id>              Record an access
  hot [--limit 20]                 Show hot memories
  cold [--days 30] [--limit 50]    Show cold memories
  archive-suggest [--days 30]      Suggest archival candidates
  auto-archive [--days 30] [--min-age 60]  Auto-archive cold memories
  thaw <filename>                  Restore an archived memory
  heatmap                          Show access heatmap
  test                             Run tests
"""
    if len(sys.argv) < 2:
        print(usage)
        return

    cmd = sys.argv[1]

    if cmd == "test":
        print("Tests moved to tests/test_memory_lru.py — run: python3 tests/test_memory_lru.py")
        return

    if cmd == "access":
        if len(sys.argv) < 4:
            print("Usage: access <table> <id>")
            return
        ok = record_access(int(sys.argv[3]), table=sys.argv[2])
        print(f"Access recorded: {ok}")

    elif cmd == "hot":
        limit = 20
        if "--limit" in sys.argv:
            limit = int(sys.argv[sys.argv.index("--limit") + 1])
        for r in get_hot_memories(limit=limit):
            print(f"  #{r['id']} [{r['table']}] {r['title']}  (count={r['access_count']}, last={r['last_accessed']})")

    elif cmd == "cold":
        days, limit = 30, 50
        if "--days" in sys.argv:
            days = int(sys.argv[sys.argv.index("--days") + 1])
        if "--limit" in sys.argv:
            limit = int(sys.argv[sys.argv.index("--limit") + 1])
        for r in get_cold_memories(days_unused=days, limit=limit):
            print(f"  #{r['id']} [{r['table']}] {r['title']}  (count={r['access_count']}, created={r['created_at']})")

    elif cmd == "archive-suggest":
        days = 30
        if "--days" in sys.argv:
            days = int(sys.argv[sys.argv.index("--days") + 1])
        candidates = suggest_archive(days_unused=days)
        print(f"Archive candidates: {len(candidates)}")
        for r in candidates:
            print(f"  #{r['id']} [{r['table']}] {r['title']}")

    elif cmd == "auto-archive":
        days_unused, min_age = 30, 60
        if "--days" in sys.argv:
            days_unused = int(sys.argv[sys.argv.index("--days") + 1])
        if "--min-age" in sys.argv:
            min_age = int(sys.argv[sys.argv.index("--min-age") + 1])
        archived = auto_archive(days_unused=days_unused, min_age_days=min_age)
        print(f"Archived {len(archived)} records:")
        for r in archived:
            print(f"  #{r['id']} [{r['table']}] {r['title']}")

    elif cmd == "thaw":
        if len(sys.argv) < 3:
            print("Usage: thaw <filename>")
            return
        result = thaw_memory(sys.argv[2])
        if result["restored"]:
            print(f"Restored: {result['table']}#{result['record_id']}")
        else:
            print(f"Failed: {result['error']}")

    elif cmd == "heatmap":
        hm = memory_heatmap()
        print(json.dumps(hm, indent=2, ensure_ascii=False))

    else:
        print(f"Unknown command: {cmd}")
        print(usage)


if __name__ == "__main__":
    _cli()
