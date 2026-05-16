# claude-introspection-metrics

Generates an interactive HTML dashboard from local Claude Code conversation logs
(`~/.claude/projects/**/*.jsonl`).

## Usage

```bash
python3 claude_metrics.py                  # all projects
python3 claude_metrics.py <project-path>   # filter by project
python3 claude_metrics.py --output foo.html
```

Outputs:
- `claude_report.html` — main dashboard (Chart.js charts of tokens, tools, models, activity, cache efficiency).
- `claude_report_conversations/<session_id>.html` — per-session conversation viewer; click any chart element to open the matching session(s).

## Requirements

Python ≥ 3.10. Standard library only — see `requirements.txt`.

## Files

- `claude_metrics.py` — scanner + report builder.
- `report_template.html` — HTML/CSS/JS template with `__PLACEHOLDER__` slots filled at render time.
- `tests/` — `pytest tests/`.
