#!/usr/bin/env python3
"""
Fetch all Cursor IDE conversations from local SQLite databases.
Output: JSON to stdout, or Markdown files to --output-dir.

Usage:
  python fetch_cursor_conversations.py
  python fetch_cursor_conversations.py --output-dir ./cursor-chats
  python fetch_cursor_conversations.py --format json > conversations.json
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path


WORKSPACE_STORAGE = Path.home() / "Library/Application Support/Cursor/User/workspaceStorage"
GLOBAL_STORAGE = Path.home() / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"


def find_databases() -> list[Path]:
    dbs = []
    if GLOBAL_STORAGE.exists():
        dbs.append(GLOBAL_STORAGE)
    if WORKSPACE_STORAGE.exists():
        for db in WORKSPACE_STORAGE.glob("*/state.vscdb"):
            dbs.append(db)
    return dbs


def query_db(db_path: Path) -> list[dict]:
    """Extract conversations from a single state.vscdb."""
    conversations = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Check which tables exist
        tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

        if "cursorDiskKV" in tables:
            conversations.extend(_extract_cursor_kv(cur, db_path))
        elif "ItemTable" in tables:
            conversations.extend(_extract_item_table(cur, db_path))

        conn.close()
    except sqlite3.OperationalError as e:
        print(f"[warn] {db_path}: {e}", file=sys.stderr)
    return conversations


def _extract_cursor_kv(cur: sqlite3.Cursor, db_path: Path) -> list[dict]:
    """Parse cursorDiskKV table — main conversation store."""
    conversations = []

    # composerData keys hold session metadata + message lists
    rows = cur.execute(
        "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
    ).fetchall()

    for row in rows:
        try:
            data = json.loads(row["value"])
            conv = _normalize_composer(data, row["key"], db_path)
            if conv:
                conversations.append(conv)
        except (json.JSONDecodeError, KeyError):
            continue

    # If no composerData, try bubbleId keys (older format)
    if not conversations:
        rows = cur.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%' LIMIT 1000"
        ).fetchall()
        bubbles: dict[str, dict] = {}
        for row in rows:
            try:
                bubbles[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                continue
        if bubbles:
            conversations.append({
                "id": str(db_path.parent.name),
                "source_db": str(db_path),
                "messages": list(bubbles.values()),
            })

    return conversations


def _extract_item_table(cur: sqlite3.Cursor, db_path: Path) -> list[dict]:
    """Fallback: ItemTable format used in some Cursor versions."""
    conversations = []
    rows = cur.execute(
        "SELECT key, value FROM ItemTable WHERE key LIKE '%chat%' OR key LIKE '%conversation%'"
    ).fetchall()
    for row in rows:
        try:
            data = json.loads(row["value"])
            conversations.append({
                "id": row["key"],
                "source_db": str(db_path),
                "raw": data,
            })
        except json.JSONDecodeError:
            continue
    return conversations


def _normalize_composer(data: dict, key: str, db_path: Path) -> dict | None:
    """Normalize composerData blob into a consistent conversation shape."""
    # Different Cursor versions use different field names
    messages = (
        data.get("conversation")
        or data.get("messages")
        or data.get("bubbles")
        or []
    )
    if not messages:
        return None

    normalized_messages = []
    for m in messages:
        role = (
            m.get("role")
            or ("user" if m.get("type") == "human" else "assistant")
        )
        text = (
            m.get("content")
            or m.get("text")
            or m.get("rawText")
            or ""
        )
        if isinstance(text, list):
            # Content can be a list of blocks
            text = "\n".join(
                b.get("text", "") if isinstance(b, dict) else str(b)
                for b in text
            )
        normalized_messages.append({"role": role, "content": text})

    return {
        "id": key,
        "title": data.get("title") or data.get("name") or "",
        "created_at": data.get("createdAt") or data.get("timestamp") or "",
        "source_db": str(db_path),
        "messages": normalized_messages,
    }


def to_markdown(conv: dict) -> str:
    lines = [f"# {conv.get('title') or conv['id']}", ""]
    if conv.get("created_at"):
        lines += [f"_Created: {conv['created_at']}_", ""]
    for msg in conv.get("messages", []):
        role = msg.get("role", "unknown").capitalize()
        content = msg.get("content", "").strip()
        lines += [f"**{role}**", "", content, ""]
    return "\n".join(lines)


def safe_filename(s: str) -> str:
    return re.sub(r"[^\w\-.]", "_", s)[:80]


def main():
    parser = argparse.ArgumentParser(description="Fetch Cursor IDE conversations")
    parser.add_argument("--output-dir", "-o", help="Write Markdown files here instead of stdout")
    parser.add_argument("--format", choices=["json", "markdown"], default="json",
                        help="Output format when writing to stdout (default: json)")
    parser.add_argument("--db", help="Path to a specific state.vscdb (skip auto-discovery)")
    args = parser.parse_args()

    dbs = [Path(args.db)] if args.db else find_databases()

    if not dbs:
        print("No Cursor databases found.", file=sys.stderr)
        print(f"Expected: {WORKSPACE_STORAGE}", file=sys.stderr)
        sys.exit(1)

    all_conversations = []
    for db in dbs:
        all_conversations.extend(query_db(db))

    if not all_conversations:
        print("Databases found but no conversations extracted.", file=sys.stderr)
        sys.exit(0)

    if args.output_dir:
        out = Path(args.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        for conv in all_conversations:
            name = safe_filename(conv.get("title") or conv["id"]) + ".md"
            (out / name).write_text(to_markdown(conv), encoding="utf-8")
        print(f"Wrote {len(all_conversations)} conversations to {out}/", file=sys.stderr)
    elif args.format == "markdown":
        for conv in all_conversations:
            print(to_markdown(conv))
            print("\n---\n")
    else:
        json.dump(all_conversations, sys.stdout, indent=2, ensure_ascii=False)
        print()


if __name__ == "__main__":
    main()
