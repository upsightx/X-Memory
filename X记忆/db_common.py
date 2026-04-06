"""公共数据库连接模块

所有模块统一通过此模块获取 DB 连接，避免重复定义 DB_PATH 和连接逻辑。
"""

import os
import re
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("SELF_EVOLUTION_DB", Path(__file__).parent / "memory.db"))


def get_db(db_path=None):
    """获取数据库连接，启用 WAL 模式和 Row 工厂"""
    path = str(db_path or DB_PATH)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


# ============ Time hint parsing ============

_TIME_PATTERNS = {
    r"今天|today": 0,
    r"昨天|yesterday": 1,
    r"前天": 2,
    r"上午": 0,
    r"下午": 0,
    r"最近|近期": 7,
    r"上周|last\s*week": 7,
    r"这周|this\s*week": 7,
    r"上个月|last\s*month": 30,
    r"这个月|this\s*month": 30,
}


def parse_time_hint(query: str) -> dict | None:
    """从查询中提取时间暗示。

    Returns:
        {"days_ago": int, "matched": str} or None
    """
    if not query:
        return None
    for pattern, days in _TIME_PATTERNS.items():
        if re.search(pattern, query, re.IGNORECASE):
            return {"days_ago": days, "matched": pattern}
    return None
