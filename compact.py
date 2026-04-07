#!/usr/bin/env python3
"""
Compact — 多级上下文压缩。

参考 Claude Code 的 compact/prompt.ts 设计：
1. microCompact — 单条工具输出压缩（超过阈值时摘要化）
2. fullCompact — 全对话/全记忆压缩（9段式 prompt）
3. memoryCompact — 记忆文件压缩（提取关键信息，归档原文）

核心设计（来自 CC）：
- 压缩前先做 <analysis> 分析，然后输出 <summary>
- analysis 块被 strip 掉，不进入后续上下文
- 保留所有用户消息原文（防止意图漂移）
- 包含"直接引用最近对话"要求（防止任务偏移）

不做什么：
- 不调用 Anthropic API（用通用 OpenAI 兼容接口）
- 不修改对话历史（只生成摘要文本）
- 不自动触发（由调用方决定何时压缩）
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path

# ============ Config ============

LLM_API_BASE = os.environ.get("COMPACT_API_BASE", os.environ.get("SMART_RECALL_API_BASE", "https://api.siliconflow.cn/v1"))
LLM_API_KEY = os.environ.get("COMPACT_API_KEY", os.environ.get("SMART_RECALL_API_KEY", os.environ.get("SILICONFLOW_API_KEY", "")))
LLM_MODEL = os.environ.get("COMPACT_MODEL", os.environ.get("SMART_RECALL_MODEL", "Qwen/Qwen2.5-7B-Instruct"))

# Thresholds
MICRO_COMPACT_THRESHOLD = 2000  # chars, tool output above this gets summarized
FULL_COMPACT_MAX_TOKENS = 4096


# ============ Prompts (from CC compact/prompt.ts) ============

FULL_COMPACT_PROMPT = """Your task is to create a detailed summary of the conversation/content, paying close attention to explicit requests and previous actions.
This summary should be thorough in capturing technical details, decisions, and context essential for continuing work without losing context.

Before providing your final summary, wrap your analysis in <analysis> tags. In your analysis:

1. Chronologically analyze each section. For each, identify:
   - Explicit requests and intents
   - Approach taken to address requests
   - Key decisions, technical concepts and patterns
   - Specific details: file names, code snippets, function signatures
   - Errors encountered and how they were fixed
   - User feedback, especially corrections or changed direction
2. Double-check for technical accuracy and completeness.

Your summary (after analysis) should include these sections:

1. **Primary Request and Intent**: All explicit requests and intents in detail
2. **Key Technical Concepts**: Important technologies, frameworks, patterns discussed
3. **Files and Code Sections**: Specific files examined/modified/created, with code snippets and why
4. **Errors and Fixes**: All errors and how they were fixed. User feedback that changed direction.
5. **Problem Solving**: Problems solved and ongoing troubleshooting
6. **All User Messages**: List ALL user messages (not tool results). Critical for understanding feedback.
7. **Pending Tasks**: Tasks explicitly asked to work on
8. **Current Work**: What was being worked on immediately before this summary. Include file names and code.
9. **Next Step**: The next step directly in line with the most recent explicit request. Include direct quotes from recent conversation to prevent drift.

Output format:
<analysis>
[your detailed analysis here]
</analysis>

<summary>
[your structured summary here]
</summary>"""

MICRO_COMPACT_PROMPT = """Summarize this tool output concisely, preserving:
- Key data points and results
- Error messages (exact text)
- File paths and line numbers
- Any actionable information

Be concise but don't lose critical details. Output only the summary, no preamble."""

MEMORY_COMPACT_PROMPT = """You are compressing memory files for an AI agent. Extract and preserve:

1. **Decisions made** — what was chosen, what was rejected, and why
2. **Lessons learned** — what went wrong, what worked, what to avoid
3. **Key facts** — names, dates, configurations, API endpoints, file paths
4. **User preferences** — how the user likes things done
5. **Ongoing context** — unfinished tasks, pending items

Discard:
- Routine operations that succeeded without issues
- Verbose logs and debug output
- Duplicate information
- Temporary states that are no longer relevant

Output a clean, structured markdown summary. Use bullet points. Be concise but complete."""


# ============ Core Functions ============

def _call_llm(system: str, user: str, max_tokens: int = 2048) -> str:
    """Call LLM API."""
    if not LLM_API_KEY:
        return "[compact unavailable: no API key]"
    
    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
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
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"[compact error: {e}]"


def extract_summary(response: str) -> str:
    """Extract <summary> block from response, stripping <analysis>."""
    # Try to extract summary block
    m = re.search(r"<summary>(.*?)</summary>", response, re.DOTALL)
    if m:
        return m.group(1).strip()
    
    # If no tags, strip analysis block and return rest
    response = re.sub(r"<analysis>.*?</analysis>", "", response, flags=re.DOTALL)
    return response.strip()


def micro_compact(tool_output: str, tool_name: str = "") -> str:
    """Compress a single tool output if it exceeds threshold.
    
    Args:
        tool_output: The raw tool output text
        tool_name: Optional tool name for context
        
    Returns:
        Original text if under threshold, otherwise compressed summary
    """
    if len(tool_output) <= MICRO_COMPACT_THRESHOLD:
        return tool_output
    
    context = f"Tool: {tool_name}\n\n" if tool_name else ""
    user_msg = f"{context}Output ({len(tool_output)} chars):\n\n{tool_output[:8000]}"
    
    result = _call_llm(MICRO_COMPACT_PROMPT, user_msg, max_tokens=512)
    return f"[compressed from {len(tool_output)} chars]\n{result}"


def full_compact(conversation: str) -> str:
    """Compress a full conversation using 9-section structured prompt.
    
    Args:
        conversation: The full conversation text (messages concatenated)
        
    Returns:
        Structured summary (analysis stripped)
    """
    response = _call_llm(FULL_COMPACT_PROMPT, conversation, max_tokens=FULL_COMPACT_MAX_TOKENS)
    return extract_summary(response)


def memory_compact(files_content: dict[str, str]) -> str:
    """Compress multiple memory files into a single summary.
    
    Args:
        files_content: {filename: content} dict
        
    Returns:
        Compressed markdown summary
    """
    parts = []
    for fname, content in files_content.items():
        parts.append(f"## {fname}\n{content[:3000]}")
    
    combined = "\n\n".join(parts)
    return _call_llm(MEMORY_COMPACT_PROMPT, combined, max_tokens=FULL_COMPACT_MAX_TOKENS)


def compact_memory_dir(memory_dir: str, days_old: int = 30) -> dict:
    """Scan memory dir, compact files older than N days.
    
    Returns:
        {
            "candidates": [filenames],
            "summary": "compressed content",
            "total_chars_before": int,
            "total_chars_after": int,
        }
    """
    from datetime import datetime, timedelta
    
    p = Path(memory_dir)
    cutoff = datetime.now().timestamp() - (days_old * 86400)
    
    candidates = {}
    for md_file in sorted(p.glob("*.md")):
        if md_file.name in ("MEMORY.md", "index.md"):
            continue
        if md_file.stat().st_mtime < cutoff:
            try:
                candidates[md_file.name] = md_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
    
    if not candidates:
        return {"candidates": [], "summary": "", "total_chars_before": 0, "total_chars_after": 0}
    
    total_before = sum(len(c) for c in candidates.values())
    summary = memory_compact(candidates)
    
    return {
        "candidates": list(candidates.keys()),
        "summary": summary,
        "total_chars_before": total_before,
        "total_chars_after": len(summary),
    }


# ============ CLI ============

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Compact — multi-level context compression")
    sub = parser.add_subparsers(dest="cmd")
    
    # micro
    p_micro = sub.add_parser("micro", help="Compress a single tool output")
    p_micro.add_argument("text", help="Text to compress (or - for stdin)")
    p_micro.add_argument("--tool", default="", help="Tool name")
    
    # full
    p_full = sub.add_parser("full", help="Compress a full conversation")
    p_full.add_argument("text", help="Conversation text (or - for stdin)")
    
    # memory
    p_mem = sub.add_parser("memory", help="Compress old memory files")
    p_mem.add_argument("--dir", default=_DEFAULT_MEMORY_DIR, help="Memory directory")
    p_mem.add_argument("--days", type=int, default=30, help="Files older than N days")
    
    args = parser.parse_args()
    
    if args.cmd == "micro":
        text = sys.stdin.read() if args.text == "-" else args.text
        print(micro_compact(text, args.tool))
    elif args.cmd == "full":
        text = sys.stdin.read() if args.text == "-" else args.text
        print(full_compact(text))
    elif args.cmd == "memory":
        result = compact_memory_dir(args.dir, args.days)
        if not result["candidates"]:
            print("No files to compact.")
        else:
            print(f"📦 Compacted {len(result['candidates'])} files: {result['total_chars_before']} → {result['total_chars_after']} chars")
            print(f"\nFiles: {', '.join(result['candidates'])}")
            print(f"\n{result['summary']}")
    else:
        parser.print_help()


_DEFAULT_MEMORY_DIR = str(Path(__file__).resolve().parent.parent / "memory")

if __name__ == "__main__":
    import sys
    main()
