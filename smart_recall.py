#!/usr/bin/env python3
"""
Smart Recall — LLM 驱动的记忆语义筛选。

参考 Claude Code 的 findRelevantMemories.ts 设计：
1. 扫描 memory/*.md 文件，只读 frontmatter（前30行）
2. 生成 manifest（filename + description + type + mtime）
3. 用便宜模型做 sideQuery，从 manifest 中选最相关的 ≤5 个文件
4. 返回被选中文件的路径

比 memory_search 的关键词匹配精准得多，成本可控（只调一次便宜模型）。

不做什么：
- 不读文件全文（只读 frontmatter）
- 不做 embedding（用 LLM 判断语义相关性）
- 不修改任何文件
"""
from __future__ import annotations

import json
import math
import os
import re
import struct
import urllib.request
from datetime import datetime
from pathlib import Path

# ============ Config ============

MAX_MEMORY_FILES = 200
FRONTMATTER_MAX_LINES = 30
MAX_SELECTED = 5
COARSE_CANDIDATE_LIMIT = 50  # Stage-1 keyword coarse filter candidate cap

# LLM config — 用便宜模型
LLM_API_BASE = os.environ.get("SMART_RECALL_API_BASE", "https://api.siliconflow.cn/v1")
LLM_API_KEY = os.environ.get("SMART_RECALL_API_KEY", os.environ.get("SILICONFLOW_API_KEY", ""))
# Model for semantic embedding/retrieval.
LLM_MODEL = os.environ.get("SMART_RECALL_MODEL", "BAAI/bge-m3")

# Default memory directory — resolved from X记忆 location
_DEFAULT_MEMORY_DIR = str(Path(__file__).resolve().parent.parent / "memory")


# ============ Frontmatter Parsing ============

def parse_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter from markdown content.
    
    Returns dict with keys like description, type, tags.
    """
    if not content.startswith("---"):
        return {}
    
    end = content.find("---", 3)
    if end == -1:
        return {}
    
    fm_text = content[3:end].strip()
    result = {}
    for line in fm_text.split("\n"):
        line = line.strip()
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if val:
                result[key] = val
    return result


# ============ Memory Scanning ============

def scan_memory_files(memory_dir: str | None = None) -> list[dict]:
    """Scan memory directory for .md files, read frontmatter only.
    
    Returns list of headers sorted by mtime (newest first), capped at MAX_MEMORY_FILES.
    Each header: {filename, filepath, mtime_iso, description, type}
    """
    memory_dir = memory_dir or _DEFAULT_MEMORY_DIR
    p = Path(memory_dir)
    if not p.exists():
        return []
    
    headers = []
    for md_file in p.rglob("*.md"):
        # Skip MEMORY.md (already loaded in system prompt)
        if md_file.name == "MEMORY.md":
            continue
        # Skip archive
        if "archive" in str(md_file):
            continue
        
        try:
            # Only read first N lines
            lines = []
            with open(md_file, "r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f):
                    if i >= FRONTMATTER_MAX_LINES:
                        break
                    lines.append(line)
            
            content = "".join(lines)
            fm = parse_frontmatter(content)
            stat = md_file.stat()
            
            # Extract description from frontmatter or first non-empty line after frontmatter
            description = fm.get("description")
            if not description:
                # Try first heading or non-empty line
                for line in lines:
                    line = line.strip()
                    if line.startswith("# "):
                        description = line[2:].strip()
                        break
                    elif line and not line.startswith("---"):
                        description = line[:100]
                        break
            
            headers.append({
                "filename": str(md_file.relative_to(p)),
                "filepath": str(md_file),
                "mtime_iso": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "mtime_ms": stat.st_mtime * 1000,
                "description": description,
                "type": fm.get("type"),
            })
        except Exception:
            continue
    
    # Sort by mtime descending, cap at MAX
    headers.sort(key=lambda h: h["mtime_ms"], reverse=True)
    return headers[:MAX_MEMORY_FILES]


def format_manifest(headers: list[dict]) -> str:
    """Format headers as text manifest for LLM selection."""
    lines = []
    for h in headers:
        tag = f"[{h['type']}] " if h.get("type") else ""
        desc = f": {h['description']}" if h.get("description") else ""
        lines.append(f"- {tag}{h['filename']} ({h['mtime_iso']}){desc}")
    return "\n".join(lines)


# ============ LLM Selection ============

# ============ Semantic Coarse Filter via SiliconFlow BGE-M3 ============

SILICONFLOW_API_KEY = os.environ.get("SILICONFLOW_API_KEY", "")
SILICONFLOW_EMBED_URL = "https://api.siliconflow.cn/v1/embeddings"
EMBED_MODEL = "BAAI/bge-m3"

def _get_embedding(text: str) -> list[float]:
    """Get embedding from SiliconFlow."""
    if not SILICONFLOW_API_KEY or not text:
        return []
    try:
        payload = json.dumps({"model": EMBED_MODEL, "input": text}).encode("utf-8")
        req = urllib.request.Request(SILICONFLOW_EMBED_URL, data=payload, 
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {SILICONFLOW_API_KEY}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["data"][0]["embedding"]
    except Exception:
        return []

def _cosine_sim(a: list[float], b: list[float]) -> float:
    if not a or not b: return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0

def _semantic_filter(query: str, headers: list[dict], limit: int = 50) -> list[dict]:
    """Stage-1: Semantic Coarse Filter using BGE-M3.
    
    Since we don't have pre-built vectors for all files in DB yet, 
    we'll use a hybrid approach:
    1. If memory.db has embeddings, use them.
    2. Else, fallback to keyword filter.
    """
    try:
        import sqlite3
        db_path = Path(__file__).resolve().parent / "memory.db"
        if not db_path.exists():
            return _keyword_filter(query, headers)
        
        # Get query vector
        q_vec = _get_embedding(query)
        if not q_vec:
            return _keyword_filter(query, headers)
        
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT source_id, embedding FROM embeddings WHERE source_table = 'observations'").fetchall()
        conn.close()
        
        # Build a map from filepath -> header for fast lookup
        filepath_to_header = {h["filepath"]: h for h in headers}
        
        # Also get observation titles to match against header descriptions/filenames
        obs_map = {}
        for row in rows:
            obs_map[row[0]] = row[1]  # source_id -> embedding blob
        
        # Get titles for all observation ids
        obs_titles = {}
        try:
            title_rows = conn.execute(
                f"SELECT id, title FROM observations WHERE id IN ({','.join('?' * len(obs_map))})",
                list(obs_map.keys())
            ).fetchall() if obs_map else []
            for r in title_rows:
                obs_titles[r[0]] = r[1] or ""
        except Exception:
            pass
        conn.close()
        
        scored = []
        seen_headers = set()
        for source_id, emb_blob in obs_map.items():
            title = obs_titles.get(source_id, "").lower()
            vec = struct.unpack(f'{len(emb_blob)//4}f', emb_blob)
            score = _cosine_sim(q_vec, list(vec))
            # Match header by title appearing in description or filename
            for h in headers:
                hkey = h["filepath"]
                if hkey in seen_headers:
                    continue
                desc = (h.get("description") or "").lower()
                fname = (h.get("filename") or "").lower()
                if title and (title[:20] in desc or title[:20] in fname):
                    scored.append((score, h))
                    seen_headers.add(hkey)
                    break
        
        scored.sort(key=lambda x: x[0], reverse=True)
        return [h for _, h in scored[:limit]]
    except Exception as e:
        print(f"[smart_recall] Semantic filter failed, falling back to keyword: {e}")
        return _keyword_filter(query, headers)

def _keyword_filter(query: str, headers: list[dict]) -> list[dict]:
    """Stage-1 coarse filter: use SQLite FTS5 to find ~top-50 keyword-matching candidates.

    Returns filtered list of headers sorted by FTS rank.
    Falls back to returning all headers if FTS fails (no new dependencies).
    """
    if not query or not headers:
        return headers

    try:
        import sqlite3
        from pathlib import Path

        # Point at the memory.db in the X记忆 package
        db_path = Path(__file__).resolve().parent / "memory.db"
        if not db_path.exists():
            return headers  # No DB — skip keyword filter, return all

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")

        # Try FTS5 search on observations_fts
        fts_results = set()
        try:
            rows = conn.execute(
                """
                SELECT o.id, o.title, o.description, f.rank
                FROM observations_fts f
                JOIN observations o ON f.rowid = o.id
                WHERE observations_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (query, COARSE_CANDIDATE_LIMIT),
            ).fetchall()
            for r in rows:
                fts_results.add(r["id"])
        except Exception:
            pass

        # Try FTS5 on decisions_fts
        try:
            rows = conn.execute(
                """
                SELECT d.id, d.title, f.rank
                FROM decisions_fts f
                JOIN decisions d ON f.rowid = d.id
                WHERE decisions_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (query, COARSE_CANDIDATE_LIMIT),
            ).fetchall()
            for r in rows:
                fts_results.add(f"dec-{r['id']}")
        except Exception:
            pass

        conn.close()

        if not fts_results:
            return headers  # No FTS matches — skip filter, return all

        # Filter headers: match by id encoded in filename or by title/description match
        # Headers contain filepath/filename like "YYYY-MM-DD.md" or "id_NNN.md"
        # We match by checking if the query keywords appear in the description
        scored = []
        query_lower = query.lower()
        for h in headers:
            score = 0
            desc = (h.get("description") or "").lower()
            fname = (h.get("filename") or "").lower()
            if query_lower in desc:
                score += 2
            if query_lower in fname:
                score += 1
            # Also check for title-like words from query
            for word in query_lower.split():
                if word and len(word) > 1:
                    if word in desc:
                        score += 0.5
                    if word in fname:
                        score += 0.25
            if score > 0:
                scored.append((score, h))
            elif not fts_results:
                # No FTS results at all, include everything
                scored.append((0, h))

        # If FTS had results but none of our headers matched, still include all headers
        # (archived files or files not in DB still get considered)
        if not scored and fts_results:
            return headers[:COARSE_CANDIDATE_LIMIT]

        scored.sort(key=lambda x: x[0], reverse=True)
        return [h for _, h in scored[:COARSE_CANDIDATE_LIMIT]]

    except Exception as e:
        # Keyword filter failure is non-fatal — return all headers
        import sys
        print(f"[smart_recall] Keyword filter failed: {e}", file=sys.stderr)
        return headers


SELECT_SYSTEM_PROMPT = """You are selecting memory files that will be useful to an AI agent processing a user's query.
You will be given the query and a list of available memory files with filenames and descriptions.

Return a JSON object with a "selected" key containing a list of filenames (up to 5).
Only include memories you are CERTAIN will be helpful. If unsure, don't include it.
If none are relevant, return an empty list.

Rules:
- Be selective. Quality over quantity.
- Prefer recent files over old ones when relevance is similar.
- If recently-used tools are listed, skip their reference docs (already in context), but DO select warnings/gotchas about those tools.

Return ONLY valid JSON: {"selected": ["file1.md", "file2.md"]}"""


def _call_llm(system: str, user: str) -> str:
    """Call LLM API. Returns response text."""
    if not LLM_API_KEY:
        return '{"selected": []}'
    
    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": 256,
        "temperature": 0,
    }).encode("utf-8")
    
    req = urllib.request.Request(
        f"{LLM_API_BASE}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        },
    )
    
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[smart_recall] LLM call failed: {e}")
        return '{"selected": []}'


def select_relevant(query: str, memory_dir: str | None = None,
                    recent_tools: list[str] | None = None,
                    already_surfaced: set[str] | None = None) -> list[dict]:
    """Find memory files relevant to a query using two-stage selection.

    Stage 1 — Keyword Coarse Filter: use SQLite FTS5 to get ~50 keyword-matching
               candidates from the manifest. Falls back to full manifest if
               keyword search returns nothing (backward-compatible).

    Stage 2 — LLM Fine-Grained Selection: ask cheap LLM to pick ≤5 from the
               Stage-1 candidates. Falls back to full LLM selection if Stage-1
               yields no results.

    Args:
        query: user query or task description
        memory_dir: path to memory directory
        recent_tools: tools recently used (skip their docs)
        already_surfaced: filenames already shown (skip them)

    Returns:
        list of {filename, filepath, mtime_iso, description} for selected files
    """
    headers = scan_memory_files(memory_dir)

    if already_surfaced:
        headers = [h for h in headers if h["filename"] not in already_surfaced]

    if not headers:
        return []

    # ---- Stage 1: Semantic Coarse Filter ----
    coarse_candidates = _semantic_filter(query, headers)

    # ---- Stage 2: LLM Fine Selection ----
    llm_candidates = coarse_candidates if coarse_candidates else headers

    manifest = format_manifest(llm_candidates)

    tools_section = ""
    if recent_tools:
        tools_section = f"\n\nRecently used tools: {', '.join(recent_tools)}"

    user_msg = f"Query: {query}\n\nAvailable memories:\n{manifest}{tools_section}"

    response = _call_llm(SELECT_SYSTEM_PROMPT, user_msg)

    # Parse response
    try:
        # Handle markdown code blocks
        if "```" in response:
            response = re.search(r"```(?:json)?\s*(.*?)```", response, re.DOTALL)
            response = response.group(1) if response else '{"selected": []}'

        parsed = json.loads(response)
        selected_names = parsed.get("selected", [])
    except (json.JSONDecodeError, AttributeError):
        print(f"[smart_recall] Failed to parse LLM response: {response[:200]}")
        return []

    # Map back to headers (map from both coarse and full lists)
    by_name = {h["filename"]: h for h in headers}
    result = []
    for name in selected_names[:MAX_SELECTED]:
        if name in by_name:
            result.append(by_name[name])

    return result


# ============ CLI ============

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Smart Recall — LLM-driven memory selection")
    sub = parser.add_subparsers(dest="cmd")
    
    # scan — list all memory files
    scan_p = sub.add_parser("scan", help="Scan and list memory files")
    scan_p.add_argument("--dir", help="Memory directory path")
    
    # recall — find relevant memories for a query
    recall_p = sub.add_parser("recall", help="Find relevant memories for a query")
    recall_p.add_argument("query", help="Query string")
    recall_p.add_argument("--dir", help="Memory directory path")
    recall_p.add_argument("--tools", nargs="*", help="Recently used tools")
    
    args = parser.parse_args()
    
    if args.cmd == "scan":
        headers = scan_memory_files(args.dir)
        print(f"Found {len(headers)} memory files:\n")
        print(format_manifest(headers))
    
    elif args.cmd == "recall":
        results = select_relevant(args.query, args.dir, args.tools)
        if results:
            print(f"Selected {len(results)} relevant memories:\n")
            for r in results:
                print(f"  📄 {r['filename']}")
                if r.get("description"):
                    print(f"     {r['description']}")
                print()
        else:
            print("No relevant memories found.")
    
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
