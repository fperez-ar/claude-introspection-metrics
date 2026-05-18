# claude-introspection-metrics

Interactive HTML dashboard from local Claude conversation logs. Reads JSONL
transcripts from both **Claude Code** (`~/.claude/projects/**/*.jsonl`) and
**Claude Desktop** local-agent sessions
(`~/Library/Application Support/Claude/local-agent-mode-sessions/**/.claude/projects/**/*.jsonl`).

## Usage

```bash
python3 claude_metrics.py                       # all projects, both sources
python3 claude_metrics.py <project-path>        # filter by project path
python3 claude_metrics.py --output foo.html     # custom output file
python3 claude_metrics.py --no-fetch            # skip rewriting per-session HTMLs
python3 claude_metrics.py --source code         # Claude Code only
python3 claude_metrics.py --source desktop      # Claude Desktop only
```

### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `project` (positional) | all | Filter by project path; encoded `/`→`-` Claude on-disk key. |
| `--output` | `claude_report.html` | Output dashboard HTML path. |
| `--no-fetch` | off | Reuse existing `_conversations/` folder; only rewrite dashboard. |
| `--source {both,code,desktop}` | `both` | Which transcript source(s) to scan. Each session in the report carries a `source` tag. |

## How it works

1. **Scan** Code (`~/.claude/projects/**/*.jsonl`) and/or Desktop
   (`~/Library/.../local-agent-mode-sessions/**/.claude/projects/**/*.jsonl`)
   per `--source`. `audit.jsonl` files in Desktop trees skipped. With a project
   path arg, encodes `/` → `-` (Claude's on-disk convention) and filters that
   key across enabled sources.

2. **Parse each session** (`parse_session`). Walks the JSONL stream and aggregates
   per-session metrics:
   - user / assistant / tool-use counts
   - `input_tokens`, `output_tokens`, `cache_read_input_tokens`,
     `cache_creation_input_tokens` (summed across assistant turns)
   - timestamps → start, end, duration, hour-of-day, day-of-week (JST)
   - models seen, tool-call histogram
   - first user prompt + first assistant reply snippets (for search/deep-dive)
   - title from `ai-title` message, else session-id prefix

3. **Render per-session HTML** (`render_conversation_html`). `extract_turns`
   normalises user/assistant messages, attaches `tool_result` parts to the
   preceding assistant turn, then `_render_part` emits text / thinking /
   tool_use / tool_result blocks. Result: a self-contained page with search,
   role/tool/thinking filter chips, and a metrics side panel. Written to
   `<output_stem>_conversations/<session_id>.html`.

4. **Render dashboard** (`build_report_html`). Loads `report_template.html`,
   substitutes `__GENERATED__`, `__SESSION_COUNT__`, `__PROJECT_COUNT__`,
   `__PROJECT_OPTIONS__`, `__DATA_JSON__`. All sessions ship to browser as
   JSON; Chart.js draws charts client-side. Clicking chart elements opens the
   matching session HTML(s).

Running `--no-fetch` reuses existing `_conversations/` folder — regenerates only the top-level dashboard (fast iteration on chart code). Useful when you want to fetch only certain project or you want to delete other projects.

## Tagging conversations (freeform tags + notes)

Per-session freeform tags + notes persist in browser `localStorage` under key
`claudeReport.tags`:

```json
{
  "<session_id>": {
    "tags": ["refactor", "wins"],
    "notes": "ran out of context",
    "updated_at": "2026-05-18T10:30:00+09:00"
  }
}
```

Edit anywhere:

- Per-session viewer side panel — type a tag and hit Enter, click ✕ to remove,
  notes auto-save on blur.
- Dashboard sessions table — `+ tag` button per row prompts for a value; click
  the ✕ on any pill to remove.

Edits save instantly to `localStorage`; the **Top Tags** chart and the
*Tag contains* advanced filter pick up changes on the next render. The
dashboard also listens for cross-tab `storage` events, so edits in an open
viewer reflect in the dashboard live.

Caveat: storage is per-browser. Clearing site data wipes tags. Different
machines / profiles see different tags. No server-side persistence by design.

## Outputs

- `claude_report.html` — dashboard (token usage, tools, models, activity heatmap, cache efficiency).
- `claude_report_conversations/<session_id>.html` — per-session viewer.

## Requirements

Python ≥ 3.10. Stdlib only. See `requirements.txt`.

## Files

- `claude_metrics.py` — scanner + report builder.
- `report_template.html` — dashboard template with `__PLACEHOLDER__` slots.
- `tests/` — `pytest tests/`.

## Test coverage

```bash
python3 -m coverage run --source=claude_metrics -m pytest tests/ && python3 -m coverage report -m
```

`run --source=claude_metrics` records line hits while pytest executes; `report -m` prints the percentage plus missing line numbers. Requires `coverage` from `requirements.txt`.
