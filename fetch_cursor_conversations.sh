#!/usr/bin/env bash
# Fetch all Cursor IDE conversations from local SQLite databases.
#
# Usage:
#   ./fetch_cursor_conversations.sh                  # JSON to stdout
#   ./fetch_cursor_conversations.sh --output-dir ./cursor-chats  # Markdown files
#   ./fetch_cursor_conversations.sh --db /path/to/state.vscdb

set -euo pipefail

WORKSPACE_STORAGE="$HOME/Library/Application Support/Cursor/User/workspaceStorage"
GLOBAL_STORAGE="$HOME/Library/Application Support/Cursor/User/globalStorage/state.vscdb"
OUTPUT_DIR=""
SPECIFIC_DB=""
FORMAT="json"

usage() {
  echo "Usage: $0 [--output-dir DIR] [--format json|markdown] [--db PATH]"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --output-dir|-o) OUTPUT_DIR="$2"; shift 2 ;;
    --format|-f)     FORMAT="$2";     shift 2 ;;
    --db)            SPECIFIC_DB="$2"; shift 2 ;;
    *) usage ;;
  esac
done

command -v sqlite3 >/dev/null 2>&1 || { echo "sqlite3 not found" >&2; exit 1; }

# Collect DB paths
declare -a DBS=()
if [[ -n "$SPECIFIC_DB" ]]; then
  DBS=("$SPECIFIC_DB")
else
  [[ -f "$GLOBAL_STORAGE" ]] && DBS+=("$GLOBAL_STORAGE")
  while IFS= read -r -d '' db; do
    DBS+=("$db")
  done < <(find "$WORKSPACE_STORAGE" -name "state.vscdb" -print0 2>/dev/null)
fi

if [[ ${#DBS[@]} -eq 0 ]]; then
  echo "No Cursor databases found." >&2
  echo "Expected: $WORKSPACE_STORAGE" >&2
  exit 1
fi

# Extract conversations from one DB, emit newline-delimited JSON objects
extract_db() {
  local db="$1"

  # Check available tables
  local tables
  tables=$(sqlite3 "$db" "SELECT name FROM sqlite_master WHERE type='table';" 2>/dev/null)

  if echo "$tables" | grep -q "cursorDiskKV"; then
    # composerData keys = conversations
    sqlite3 -separator $'\x1f' "$db" \
      "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%';" 2>/dev/null \
    | while IFS=$'\x1f' read -r key value; do
        # emit one JSON line per row; post-processing in Python/jq below
        printf '{"id":%s,"source_db":%s,"data":%s}\n' \
          "$(printf '%s' "$key"   | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" \
          "$(printf '%s' "$db"    | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" \
          "$value"
      done
  elif echo "$tables" | grep -q "ItemTable"; then
    sqlite3 -separator $'\x1f' "$db" \
      "SELECT key, value FROM ItemTable WHERE key LIKE '%chat%' OR key LIKE '%conversation%';" 2>/dev/null \
    | while IFS=$'\x1f' read -r key value; do
        printf '{"id":%s,"source_db":%s,"data":%s}\n' \
          "$(printf '%s' "$key" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" \
          "$(printf '%s' "$db"  | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" \
          "$value"
      done
  fi
}

# Normalize + render via inline Python (sqlite3 shell can't do JSON well enough alone)
normalize_and_render() {
  python3 - "$OUTPUT_DIR" "$FORMAT" <<'PYEOF'
import json, sys, re, os
from pathlib import Path

output_dir = sys.argv[1]
fmt = sys.argv[2]

def normalize(raw):
    data = raw.get("data", {})
    if isinstance(data, str):
        try: data = json.loads(data)
        except: return None
    messages_raw = data.get("conversation") or data.get("messages") or data.get("bubbles") or []
    if not messages_raw:
        return None
    messages = []
    for m in messages_raw:
        role = m.get("role") or ("user" if m.get("type") == "human" else "assistant")
        text = m.get("content") or m.get("text") or m.get("rawText") or ""
        if isinstance(text, list):
            text = "\n".join(b.get("text","") if isinstance(b,dict) else str(b) for b in text)
        messages.append({"role": role, "content": text.strip()})
    return {
        "id": raw.get("id",""),
        "title": data.get("title") or data.get("name") or "",
        "created_at": data.get("createdAt") or data.get("timestamp") or "",
        "source_db": raw.get("source_db",""),
        "messages": messages,
    }

def to_markdown(conv):
    lines = [f"# {conv['title'] or conv['id']}", ""]
    if conv.get("created_at"):
        lines += [f"_Created: {conv['created_at']}_", ""]
    for m in conv["messages"]:
        lines += [f"**{m['role'].capitalize()}**", "", m["content"], ""]
    return "\n".join(lines)

convs = []
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        raw = json.loads(line)
        conv = normalize(raw)
        if conv: convs.append(conv)
    except json.JSONDecodeError:
        continue

if output_dir:
    os.makedirs(output_dir, exist_ok=True)
    for c in convs:
        name = re.sub(r"[^\w\-.]", "_", c["title"] or c["id"])[:80] + ".md"
        Path(output_dir, name).write_text(to_markdown(c), encoding="utf-8")
    print(f"Wrote {len(convs)} conversations to {output_dir}/", file=sys.stderr)
elif fmt == "markdown":
    for c in convs:
        print(to_markdown(c))
        print("\n---\n")
else:
    json.dump(convs, sys.stdout, indent=2, ensure_ascii=False)
    print()
PYEOF
}

# Stream all DBs through normalizer
for db in "${DBS[@]}"; do
  extract_db "$db"
done | normalize_and_render
