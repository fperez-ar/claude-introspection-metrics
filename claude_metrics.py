#!/usr/bin/env python3
"""
Generates an interactive HTML metrics report for Claude conversations.
Reads JSONL transcripts from Claude Code and/or Claude Desktop local-agent sessions.
Also writes per-session chat log HTMLs to <output_stem>_conversations/.

Usage:
    python3 claude_metrics.py [project_path] [--output report.html] [--source both|code|desktop]
"""

import json
import glob
import os
import sys
import argparse
import html as html_mod
from datetime import datetime, timezone, timedelta
from collections import defaultdict

JST = timezone(timedelta(hours=9))

SOURCES = {
    "code": "~/.claude/projects",
    "desktop": "~/Library/Application Support/Claude/local-agent-mode-sessions",
}

# Defaults per https://platform.claude.com/docs/en/about-claude/pricing
# All rates are USD per 1M tokens. First matching pattern (substring on model id)
# wins; last entry (empty pattern) is the catch-all fallback.
PRICING_DEFAULTS = [
    {"id": "opus-4-7",   "pattern": "opus-4-7",   "label": "Claude Opus 4.7",   "input":  5.0, "cache_5m": 6.25,  "cache_1h": 10.0, "cache_hit": 0.50, "output": 25.0},
    {"id": "opus-4-6",   "pattern": "opus-4-6",   "label": "Claude Opus 4.6",   "input":  5.0, "cache_5m": 6.25,  "cache_1h": 10.0, "cache_hit": 0.50, "output": 25.0},
    {"id": "opus-4-5",   "pattern": "opus-4-5",   "label": "Claude Opus 4.5",   "input":  5.0, "cache_5m": 6.25,  "cache_1h": 10.0, "cache_hit": 0.50, "output": 25.0},
    {"id": "opus-4-1",   "pattern": "opus-4-1",   "label": "Claude Opus 4.1",   "input": 15.0, "cache_5m": 18.75, "cache_1h": 30.0, "cache_hit": 1.50, "output": 75.0},
    {"id": "opus-4",     "pattern": "opus-4",     "label": "Claude Opus 4",     "input": 15.0, "cache_5m": 18.75, "cache_1h": 30.0, "cache_hit": 1.50, "output": 75.0},
    {"id": "sonnet-4-6", "pattern": "sonnet-4-6", "label": "Claude Sonnet 4.6", "input":  3.0, "cache_5m":  3.75, "cache_1h":  6.0, "cache_hit": 0.30, "output": 15.0},
    {"id": "sonnet-4-5", "pattern": "sonnet-4-5", "label": "Claude Sonnet 4.5", "input":  3.0, "cache_5m":  3.75, "cache_1h":  6.0, "cache_hit": 0.30, "output": 15.0},
    {"id": "sonnet-4",   "pattern": "sonnet-4",   "label": "Claude Sonnet 4",   "input":  3.0, "cache_5m":  3.75, "cache_1h":  6.0, "cache_hit": 0.30, "output": 15.0},
    {"id": "haiku-4-5",  "pattern": "haiku-4-5",  "label": "Claude Haiku 4.5",  "input":  1.0, "cache_5m":  1.25, "cache_1h":  2.0, "cache_hit": 0.10, "output":  5.0},
    {"id": "haiku-3-5",  "pattern": "haiku-3-5",  "label": "Claude Haiku 3.5",  "input":  0.80,"cache_5m":  1.0,  "cache_1h":  1.60,"cache_hit": 0.08, "output":  4.0},
    {"id": "default",    "pattern": "",           "label": "Default fallback",  "input":  3.0, "cache_5m":  3.75, "cache_1h":  6.0, "cache_hit": 0.30, "output": 15.0},
]


# ── project helpers ────────────────────────────────────────────────────────

def get_project_key(path: str) -> str:
    # Claude encodes project paths by replacing "/" with "-" and stripping leading "-"
    return os.path.abspath(path).replace("/", "-").lstrip("-")


def find_jsonl_files(
    project_path: str | None = None,
    sources: tuple[str, ...] = ("code", "desktop"),
) -> list[tuple[str, str, str]]:
    """Returns list of (source, project_key, jsonl_path) tuples."""
    results = []
    key_filter = get_project_key(project_path) if project_path else None

    for src in sources:
        base = os.path.expanduser(SOURCES[src])
        if not os.path.isdir(base):
            continue

        if src == "code":
            if key_filter:
                paths = glob.glob(os.path.join(base, key_filter, "*.jsonl"))
            else:
                paths = glob.glob(os.path.join(base, "**", "*.jsonl"), recursive=True)
        else:  # desktop: nested under local_<id>/.claude/projects/<key>/...
            paths = glob.glob(
                os.path.join(base, "**", ".claude", "projects", "**", "*.jsonl"),
                recursive=True,
            )
            if key_filter:
                paths = [p for p in paths if f"{os.sep}projects{os.sep}{key_filter}{os.sep}" in p]

        for p in paths:
            if os.path.basename(p) == "audit.jsonl":
                continue
            project_key = os.path.basename(os.path.dirname(p))
            results.append((src, project_key, p))
    return results


# ── session parsing ────────────────────────────────────────────────────────

def load_messages(jsonl_path: str) -> list[dict]:
    with open(jsonl_path) as f:
        return [json.loads(l) for l in f if l.strip()]


def parse_session(jsonl_path: str) -> dict:
    """Returns metrics dict for one session."""
    messages = load_messages(jsonl_path)
    session_id = os.path.splitext(os.path.basename(jsonl_path))[0]

    cwd = next((m.get("cwd") for m in messages if m.get("cwd")), None)
    user_msgs = [m for m in messages if m.get("type") == "user" and not m.get("isMeta")]
    assistant_msgs = [m for m in messages if m.get("type") == "assistant"]

    tool_uses = []
    for m in assistant_msgs:
        content = m.get("message", {}).get("content", [])
        if isinstance(content, list):
            tool_uses.extend(c for c in content if isinstance(c, dict) and c.get("type") == "tool_use")

    input_tokens = output_tokens = cache_read_tokens = cache_creation_tokens = 0
    cache_5m_tokens = cache_1h_tokens = 0
    model_tokens: dict[str, dict[str, int]] = defaultdict(
        lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_5m": 0, "cache_1h": 0}
    )
    for m in assistant_msgs:
        msg = m.get("message", {})
        usage = msg.get("usage", {})
        model = msg.get("model") or "unknown"
        u_in = usage.get("input_tokens", 0)
        u_out = usage.get("output_tokens", 0)
        u_cr = usage.get("cache_read_input_tokens", 0)
        u_cc = usage.get("cache_creation_input_tokens", 0)
        cc_detail = usage.get("cache_creation") or {}
        u_5m = cc_detail.get("ephemeral_5m_input_tokens", 0)
        u_1h = cc_detail.get("ephemeral_1h_input_tokens", 0)
        # if breakdown missing, attribute cache creation to 5m (API default TTL)
        if not (u_5m or u_1h) and u_cc:
            u_5m = u_cc
        input_tokens += u_in
        output_tokens += u_out
        cache_read_tokens += u_cr
        cache_creation_tokens += u_cc
        cache_5m_tokens += u_5m
        cache_1h_tokens += u_1h
        mt = model_tokens[model]
        mt["input"] += u_in
        mt["output"] += u_out
        mt["cache_read"] += u_cr
        mt["cache_5m"] += u_5m
        mt["cache_1h"] += u_1h

    timestamps = [
        datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00")).astimezone(JST)
        for m in messages if "timestamp" in m
    ]
    start_time = min(timestamps) if timestamps else None
    end_time = max(timestamps) if timestamps else None
    duration_mins = (end_time - start_time).total_seconds() / 60 if start_time and end_time else 0

    models = list({
        m.get("message", {}).get("model")
        for m in assistant_msgs
        if m.get("message", {}).get("model") and m.get("message", {}).get("model") != "<synthetic>"
    })

    title = next(
        (m.get("aiTitle") for m in messages if m.get("type") == "ai-title"),
        session_id[:8]
    )

    tool_counts: dict[str, int] = defaultdict(int)
    for t in tool_uses:
        tool_counts[t.get("name", "unknown")] += 1

    user_texts = []
    for m in user_msgs:
        content = m.get("message", {}).get("content", "")
        if isinstance(content, str):
            user_texts.append(len(content))
        elif isinstance(content, list):
            user_texts.append(sum(len(c.get("text", "")) for c in content if isinstance(c, dict)))
    avg_user_len = sum(user_texts) / len(user_texts) if user_texts else 0

    # first user message — length + text snippet for deep dive search
    first_user_msg = next((m for m in user_msgs if not m.get("isMeta")), None)
    first_user_msg_len = 0
    first_user_msg_text = ""
    if first_user_msg:
        c = first_user_msg.get("message", {}).get("content", "")
        if isinstance(c, str):
            first_user_msg_len = len(c)
            first_user_msg_text = c[:500]
        elif isinstance(c, list):
            text = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
            first_user_msg_len = len(text)
            first_user_msg_text = text[:500]

    # first assistant response text snippet
    first_asst_msg = next((m for m in assistant_msgs), None)
    first_asst_msg_text = ""
    if first_asst_msg:
        content = first_asst_msg.get("message", {}).get("content", [])
        if isinstance(content, list):
            first_asst_msg_text = " ".join(
                c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            )[:500]

    return {
        "session_id": session_id,
        "jsonl_path": jsonl_path,
        "title": title,
        "cwd": cwd or "",
        "project_name": os.path.basename(cwd) if cwd else "",
        "start_time": start_time.isoformat() if start_time else None,
        "end_time": end_time.isoformat() if end_time else None,
        "duration_mins": round(duration_mins, 1),
        "hour": start_time.hour if start_time else -1,
        "dow": start_time.weekday() if start_time else -1,  # 0=Mon … 6=Sun
        "user_messages": len(user_msgs),
        "assistant_messages": len(assistant_msgs),
        "total_messages": len(user_msgs) + len(assistant_msgs),
        "tool_uses": len(tool_uses),
        "tool_counts": dict(tool_counts),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_5m_tokens": cache_5m_tokens,
        "cache_1h_tokens": cache_1h_tokens,
        "total_tokens": input_tokens + output_tokens,
        "model_tokens": dict(model_tokens),
        "models": models,
        "avg_user_msg_len": round(avg_user_len),
        "first_user_msg_len": first_user_msg_len,
        "first_user_msg_text": first_user_msg_text,
        "first_asst_msg_text": first_asst_msg_text,
    }


def collect_all_metrics(
    project_path: str | None = None,
    sources: tuple[str, ...] = ("code", "desktop"),
) -> list[dict]:
    files = find_jsonl_files(project_path, sources)
    sessions = []
    for source, project_key, jsonl_path in files:
        try:
            metrics = parse_session(jsonl_path)
            metrics["project_key"] = project_key
            metrics["source"] = source
            sessions.append(metrics)
        except Exception as e:
            print(f"Warning: failed to parse {jsonl_path}: {e}", file=sys.stderr)
    sessions.sort(key=lambda s: s["start_time"] or "")
    return sessions


# ── conversation extraction ────────────────────────────────────────────────

def _content_to_parts(content) -> list[dict]:
    """Normalises message content to list of typed parts."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return []


def extract_turns(messages: list[dict]) -> list[dict]:
    """
    Extracts conversation turns in chronological order.
    Each turn: {role, timestamp, parts: [{type, ...}]}
    Skips meta/system/snapshot messages.
    """
    turns = []
    for m in messages:
        mtype = m.get("type")
        ts = m.get("timestamp", "")

        if mtype == "user" and not m.get("isMeta"):
            content = m.get("message", {}).get("content", "")
            parts = _content_to_parts(content)
            # skip turns that are only tool_result (they pair with assistant tool_use display)
            text_parts = [p for p in parts if p.get("type") == "text"]
            tool_results = [p for p in parts if p.get("type") == "tool_result"]
            if text_parts or (not tool_results):
                turns.append({"role": "user", "timestamp": ts, "parts": parts})
            elif tool_results:
                # attach tool results to previous assistant turn if possible
                if turns and turns[-1]["role"] == "assistant":
                    turns[-1]["tool_results"] = tool_results
                else:
                    turns.append({"role": "tool_results", "timestamp": ts, "parts": tool_results})

        elif mtype == "assistant":
            content = m.get("message", {}).get("content", [])
            parts = _content_to_parts(content)
            model = m.get("message", {}).get("model", "")
            usage = m.get("message", {}).get("usage", {})
            turns.append({
                "role": "assistant",
                "timestamp": ts,
                "parts": parts,
                "model": model,
                "usage": usage,
                "tool_results": [],
            })

    return turns


# ── conversation HTML rendering ────────────────────────────────────────────

def _render_part(part: dict) -> str:
    ptype = part.get("type", "")

    if ptype == "text":
        text = html_mod.escape(part.get("text", ""))
        # preserve newlines, basic code blocks
        text = text.replace("\n", "<br>")
        return f'<div class="text-part">{text}</div>'

    if ptype == "thinking":
        thinking = html_mod.escape(part.get("thinking", "")).replace("\n", "<br>")
        return f'''<details class="thinking-block">
  <summary>Thinking…</summary>
  <div class="thinking-body">{thinking}</div>
</details>'''

    if ptype == "tool_use":
        name = html_mod.escape(part.get("name", "tool"))
        inp = html_mod.escape(json.dumps(part.get("input", {}), indent=2))
        return f'''<details class="tool-block">
  <summary><span class="tool-name">{name}</span></summary>
  <pre class="tool-input">{inp}</pre>
</details>'''

    if ptype == "tool_result":
        content = part.get("content", [])
        if isinstance(content, str):
            body = html_mod.escape(content).replace("\n", "<br>")
        elif isinstance(content, list):
            body = "<br>".join(
                html_mod.escape(c.get("text", "")).replace("\n", "<br>")
                for c in content if isinstance(c, dict)
            )
        else:
            body = ""
        return f'''<details class="tool-result-block">
  <summary>Tool result</summary>
  <div class="tool-result-body">{body}</div>
</details>'''

    # fallback
    return f'<div class="unknown-part"><code>{html_mod.escape(json.dumps(part))}</code></div>'


def render_conversation_html(session: dict, turns: list[dict]) -> str:
    date = session["start_time"][:10] if session["start_time"] else ""
    title = html_mod.escape(session["title"])
    project = html_mod.escape(session["cwd"] or session["project_name"])

    bubbles = []
    for turn in turns:
        role = turn["role"]
        if role not in ("user", "assistant"):
            continue

        ts_raw = turn.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).astimezone(JST).strftime("%H:%M")
        except (ValueError, AttributeError):
            ts = ts_raw[11:16] if len(ts_raw) >= 16 else ""

        parts_html = "".join(_render_part(p) for p in turn["parts"])

        # tool results attached to this assistant turn
        if role == "assistant":
            for tr in turn.get("tool_results", []):
                parts_html += _render_part(tr)
            model = html_mod.escape(turn.get("model", ""))
            usage = turn.get("usage", {})
            tok_in = usage.get("input_tokens", 0)
            tok_out = usage.get("output_tokens", 0)
            tok_cache = usage.get("cache_read_input_tokens", 0)
            meta = f'<div class="msg-meta">{model} &nbsp;·&nbsp; in:{tok_in:,} out:{tok_out:,} cache:{tok_cache:,} &nbsp;·&nbsp; {ts}</div>'
        else:
            meta = f'<div class="msg-meta">{ts}</div>'

            # add plaintext search index to bubble
        plain = " ".join(
            p.get("text","") or p.get("thinking","") or p.get("name","")
            for p in turn["parts"] if isinstance(p, dict)
        ).replace('"', '&quot;').replace('\n', ' ')[:1000]
        has_tools = "1" if any(p.get("type") in ("tool_use","tool_result") for p in turn["parts"]) else "0"
        msg_id = f' id="msg-{len(bubbles)}"'
        bubbles.append(
            f'<div class="msg msg-{role}"{msg_id} data-role="{role}" data-has-tools="{has_tools}" data-text="{plain}">'
            f'<div class="msg-body">{parts_html}</div>{meta}</div>'
        )

    bubbles_html = "\n".join(bubbles)

    # side panel metrics
    cache_eff = 0
    denom = session["input_tokens"] + session["cache_read_tokens"] + session["cache_creation_tokens"]
    if denom > 0:
        cache_eff = round(session["cache_read_tokens"] / denom * 100, 1)
    top_tools = sorted(session["tool_counts"].items(), key=lambda x: -x[1])[:12]
    tools_html = "".join(
        f'<div class="sp-row"><span class="sp-label">{html_mod.escape(t)}</span><span class="sp-val">{c}</span></div>'
        for t, c in top_tools
    ) or '<div class="sp-empty">No tool calls</div>'
    models_str = ", ".join(session.get("models", [])) or "—"
    dur = session["duration_mins"]

    session_meta_json = json.dumps({
        "session_id": session["session_id"],
        "model_tokens": session.get("model_tokens", {}),
        "models": session.get("models", []),
    })
    pricing_json = json.dumps(PRICING_DEFAULTS)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  :root {{
    --bg:#0f1117;--surface:#1a1d27;--surface2:#22263a;--border:#2e3347;
    --user-bg:#1e2d45;--user-border:#2d4a6e;
    --asst-bg:#1a2230;--asst-border:#2e3347;
    --accent:#d97706;--accent2:#6366f1;--text:#e2e8f0;--muted:#64748b;
    --green:#10b981;--font:'Inter',system-ui,sans-serif;
    --sp-width:260px;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:var(--font);font-size:14px;line-height:1.6;display:flex;flex-direction:column;height:100vh}}
  /* header */
  header{{padding:14px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px;flex-shrink:0}}
  .back{{color:var(--muted);text-decoration:none;font-size:13px;white-space:nowrap}}
  .back:hover{{color:var(--text)}}
  .hinfo{{flex:1;min-width:0}}
  .hinfo h1{{font-size:15px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  .hinfo p{{color:var(--muted);font-size:11px;margin-top:2px}}
  /* toolbar */
  .toolbar{{
    display:flex;align-items:center;gap:8px;flex-wrap:wrap;
    padding:10px 20px;border-bottom:1px solid var(--border);flex-shrink:0;
    background:var(--bg)
  }}
  .search-wrap{{display:flex;align-items:center;gap:6px;flex:1;min-width:200px}}
  .search-wrap input{{
    flex:1;background:var(--surface);border:1px solid var(--border);color:var(--text);
    border-radius:6px;padding:5px 10px;font-size:13px;outline:none
  }}
  .search-wrap input:focus{{border-color:var(--accent2)}}
  .match-count{{font-size:11px;color:var(--muted);white-space:nowrap}}
  .btn-nav{{
    background:var(--surface2);border:1px solid var(--border);color:var(--muted);
    border-radius:5px;padding:3px 8px;cursor:pointer;font-size:12px
  }}
  .btn-nav:hover{{color:var(--text)}}
  .filter-chips{{display:flex;gap:4px}}
  .chip{{
    padding:4px 10px;border-radius:20px;font-size:12px;font-weight:500;cursor:pointer;
    border:1px solid var(--border);background:var(--surface2);color:var(--muted);transition:all .15s
  }}
  .chip.active{{background:var(--accent2);border-color:var(--accent2);color:#fff}}
  .chip.active.user{{background:var(--user-border);border-color:var(--user-border)}}
  .chip.active.asst{{background:#334155;border-color:#475569;color:var(--text)}}
  .chip.active.tools{{background:#1a3a2a;border-color:#10b981;color:#10b981}}
  .chip.active.thinking{{background:#3a1a3a;border-color:#a855f7;color:#a855f7}}
  .divider{{width:1px;height:20px;background:var(--border);flex-shrink:0}}
  .btn-panel{{
    background:var(--surface2);border:1px solid var(--border);color:var(--muted);
    border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px;white-space:nowrap
  }}
  .btn-panel:hover,.btn-panel.active{{color:var(--text);border-color:var(--accent2)}}
  /* main layout */
  .main{{display:flex;flex:1;overflow:hidden}}
  .chat-col{{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:14px;min-width:0}}
  /* messages */
  .msg{{border-radius:10px;padding:14px 16px;border:1px solid;transition:outline .1s}}
  .msg-user{{background:var(--user-bg);border-color:var(--user-border)}}
  .msg-assistant{{background:var(--asst-bg);border-color:var(--asst-border)}}
  .msg.hidden{{display:none}}
  .msg.search-match{{outline:2px solid var(--accent2)}}
  .msg.search-current{{outline:2px solid var(--accent)}}
  .msg-body{{margin-bottom:6px}}
  .text-part{{white-space:pre-wrap;word-break:break-word}}
  .msg-meta{{font-size:11px;color:var(--muted);margin-top:6px}}
  mark{{background:#d9770633;color:var(--accent);border-radius:2px;padding:0 1px}}
  details{{margin:6px 0;border-radius:6px;overflow:hidden}}
  details summary{{
    cursor:pointer;padding:6px 10px;font-size:12px;font-weight:500;
    background:var(--surface2);color:var(--muted);list-style:none;border-radius:6px;user-select:none
  }}
  details summary:hover{{color:var(--text)}}
  details[open] summary{{border-radius:6px 6px 0 0}}
  .thinking-body,.tool-result-body{{
    padding:10px 12px;font-size:12px;white-space:pre-wrap;word-break:break-word;
    background:var(--surface);color:var(--muted);border-radius:0 0 6px 6px
  }}
  .tool-input{{
    padding:10px 12px;font-size:11px;white-space:pre-wrap;word-break:break-word;
    background:#0d1117;color:#7dd3fc;border-radius:0 0 6px 6px;font-family:monospace
  }}
  .tool-name{{color:var(--accent);font-family:monospace}}
  .unknown-part{{font-size:11px;color:var(--muted);font-family:monospace}}
  /* hide tools class on body */
  body.hide-tools .tool-block,body.hide-tools .tool-result-block{{display:none}}
  body.hide-thinking .thinking-block{{display:none}}
  /* side panel */
  .side-panel{{
    width:var(--sp-width);flex-shrink:0;border-left:1px solid var(--border);
    overflow-y:auto;background:var(--surface);transition:width .2s,opacity .2s;
  }}
  .side-panel.hidden{{width:0;opacity:0;overflow:hidden;border:none}}
  .sp-section{{padding:14px 16px;border-bottom:1px solid var(--border)}}
  .sp-section h4{{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-bottom:10px}}
  .sp-row{{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;gap:8px}}
  .sp-label{{font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  .sp-val{{font-size:12px;font-weight:600;color:var(--text);white-space:nowrap}}
  .sp-val.amber{{color:var(--accent)}}.sp-val.indigo{{color:var(--accent2)}}.sp-val.green{{color:var(--green)}}
  .sp-bar-wrap{{margin-top:4px;margin-bottom:8px}}
  .sp-bar-label{{display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-bottom:3px}}
  .sp-bar{{height:5px;border-radius:3px;background:var(--surface2)}}
  .sp-bar-fill{{height:100%;border-radius:3px}}
  .sp-empty{{font-size:12px;color:var(--muted);font-style:italic}}
  /* tag widget */
  .tag-input-wrap{{margin-bottom:8px}}
  .tag-input-wrap input,.tag-input-wrap textarea{{
    width:100%;background:var(--surface2);border:1px solid var(--border);color:var(--text);
    border-radius:5px;padding:5px 8px;font-size:12px;outline:none;font-family:inherit
  }}
  .tag-input-wrap input:focus,.tag-input-wrap textarea:focus{{border-color:var(--accent2)}}
  .tag-chips{{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}}
  .tag-chip{{
    display:inline-flex;align-items:center;gap:4px;font-size:11px;padding:2px 6px 2px 8px;
    border-radius:10px;background:var(--surface2);border:1px solid var(--border);color:var(--text)
  }}
  .tag-chip .x{{cursor:pointer;color:var(--muted);font-weight:600}}
  .tag-chip .x:hover{{color:var(--accent)}}
  #tagStatus{{font-size:11px;color:var(--muted);margin-top:6px;min-height:14px}}
  #tagStatus.ok{{color:var(--green)}}
</style>
</head>
<body>
<header>
  <a class="back" href="../index.html" onclick="window.close();return false;">← back</a>
  <div class="hinfo">
    <h1>{title}</h1>
    <p>{project} &nbsp;·&nbsp; {date} &nbsp;·&nbsp; {session['user_messages']}u / {session['assistant_messages']}a messages &nbsp;·&nbsp; {session['total_tokens']:,} tokens</p>
  </div>
</header>

<div class="toolbar">
  <div class="search-wrap">
    <input type="text" id="searchBox" placeholder="Search messages…" oninput="doSearch()">
    <span class="match-count" id="matchCount"></span>
    <button class="btn-nav" id="btnPrev" onclick="navMatch(-1)" title="Previous">▲</button>
    <button class="btn-nav" id="btnNext" onclick="navMatch(1)"  title="Next">▼</button>
  </div>
  <div class="divider"></div>
  <div class="filter-chips">
    <button class="chip user active"  id="chipUser" onclick="toggleChip('user')">User</button>
    <button class="chip asst active"  id="chipAsst" onclick="toggleChip('asst')">Assistant</button>
    <button class="chip tools active" id="chipTools" onclick="toggleChip('tools')">Tools</button>
    <button class="chip thinking active" id="chipThinking" onclick="toggleChip('thinking')">Thinking</button>
  </div>
  <div class="divider"></div>
  <button class="btn-panel" id="btnPanel" onclick="togglePanel()">⊟ Metrics</button>
</div>

<div class="main">
  <div class="chat-col" id="chatCol">
{bubbles_html}
  </div>

  <div class="side-panel" id="sidePanel">
    <div class="sp-section">
      <h4>Session</h4>
      <div class="sp-row"><span class="sp-label">Date</span><span class="sp-val">{date}</span></div>
      <div class="sp-row"><span class="sp-label">Duration</span><span class="sp-val">{dur}m</span></div>
      <div class="sp-row"><span class="sp-label">Model</span><span class="sp-val" style="font-size:11px">{html_mod.escape(models_str)}</span></div>
    </div>
    <div class="sp-section">
      <h4>Messages</h4>
      <div class="sp-row"><span class="sp-label">Total</span><span class="sp-val">{session['total_messages']}</span></div>
      <div class="sp-row"><span class="sp-label">User</span><span class="sp-val amber">{session['user_messages']}</span></div>
      <div class="sp-row"><span class="sp-label">Assistant</span><span class="sp-val indigo">{session['assistant_messages']}</span></div>
      <div class="sp-row"><span class="sp-label">Tool calls</span><span class="sp-val green">{session['tool_uses']}</span></div>
    </div>
    <div class="sp-section">
      <h4>Tokens</h4>
      <div class="sp-row"><span class="sp-label">Total</span><span class="sp-val">{session['total_tokens']:,}</span></div>
      <div class="sp-bar-wrap">
        <div class="sp-bar-label"><span>Input</span><span>{session['input_tokens']:,}</span></div>
        <div class="sp-bar"><div class="sp-bar-fill" style="width:{round(session['input_tokens']/max(session['total_tokens'],1)*100)}%;background:#6366f1"></div></div>
      </div>
      <div class="sp-bar-wrap">
        <div class="sp-bar-label"><span>Output</span><span>{session['output_tokens']:,}</span></div>
        <div class="sp-bar"><div class="sp-bar-fill" style="width:{round(session['output_tokens']/max(session['total_tokens'],1)*100)}%;background:#10b981"></div></div>
      </div>
      <div class="sp-bar-wrap">
        <div class="sp-bar-label"><span>Cache read</span><span>{session['cache_read_tokens']:,}</span></div>
        <div class="sp-bar"><div class="sp-bar-fill" style="width:{round(session['cache_read_tokens']/max(denom,1)*100)}%;background:#d97706"></div></div>
      </div>
      <div class="sp-row" style="margin-top:6px"><span class="sp-label">Cache efficiency</span><span class="sp-val green">{cache_eff}%</span></div>
    </div>
    <div class="sp-section" id="costSection">
      <h4>Estimated Cost</h4>
      <div class="sp-row"><span class="sp-label">Total</span><span class="sp-val amber" id="costTotal">—</span></div>
      <div id="costBreakdown" style="margin-top:8px"></div>
      <div style="font-size:10px;color:var(--muted);margin-top:8px">
        Rates editable in main report → Regenerate tab.
      </div>
    </div>
    <div class="sp-section" id="tagsSection">
      <h4>Tags</h4>
      <div class="tag-input-wrap">
        <input id="tagInput" type="text" placeholder="Add tag, press Enter…">
        <div class="tag-chips" id="tagChips"></div>
      </div>
      <div class="tag-input-wrap">
        <textarea id="notesInput" rows="2" placeholder="Notes…"></textarea>
      </div>
      <div id="tagStatus"></div>
    </div>
    <div class="sp-section">
      <h4>Top Tools</h4>
      {tools_html}
    </div>
  </div>
</div>

<script>
// ── filter chips ───────────────────────────────────────────────────────────
const chipState = {{user:true, asst:true, tools:true, thinking:true}};

function toggleChip(type) {{
  chipState[type] = !chipState[type];
  document.getElementById('chip'+type.charAt(0).toUpperCase()+type.slice(1))
    .classList.toggle('active', chipState[type]);
  applyFilters();
}}

function applyFilters() {{
  // tools = hide/show tool/thinking details blocks via body class
  document.body.classList.toggle('hide-tools', !chipState.tools);
  document.body.classList.toggle('hide-thinking', !chipState.thinking);
  // message visibility
  document.querySelectorAll('.msg').forEach(m => {{
    const role = m.dataset.role;
    const visible =
      (role==='user'      && chipState.user) ||
      (role==='assistant' && chipState.asst);
    m.classList.toggle('hidden', !visible);
  }});
  doSearch();
}}

// ── search ─────────────────────────────────────────────────────────────────
let matches = [], matchIdx = 0;

function doSearch() {{
  // clear previous highlights
  document.querySelectorAll('.msg').forEach(m => {{
    m.classList.remove('search-match','search-current');
    // restore original html from data-orig if set
    const orig = m.querySelector('[data-orig]');
    if(orig) {{ orig.innerHTML = orig.getAttribute('data-orig'); orig.removeAttribute('data-orig'); }}
  }});
  matches = []; matchIdx = 0;
  const q = document.getElementById('searchBox').value.trim();
  if(!q) {{ document.getElementById('matchCount').textContent=''; return; }}
  const re = new RegExp(q.replace(/[.*+?^${{}}()|[\\]\\\\]/g,'\\\\$&'),'gi');
  document.querySelectorAll('.msg:not(.hidden)').forEach(m => {{
    const text = m.dataset.text || '';
    if(text.toLowerCase().includes(q.toLowerCase())) {{
      m.classList.add('search-match');
      matches.push(m);
      // highlight in text-part elements
      m.querySelectorAll('.text-part').forEach(el => {{
        if(!el.hasAttribute('data-orig')) el.setAttribute('data-orig', el.innerHTML);
        el.innerHTML = el.innerHTML.replace(re, match => `<mark>${{match}}</mark>`);
      }});
    }}
  }});
  document.getElementById('matchCount').textContent =
    matches.length ? `1 / ${{matches.length}}` : '0';
  if(matches.length) {{ matchIdx=0; scrollToMatch(0); }}
}}

function navMatch(dir) {{
  if(!matches.length) return;
  matchIdx = (matchIdx+dir+matches.length)%matches.length;
  scrollToMatch(matchIdx);
  document.getElementById('matchCount').textContent = `${{matchIdx+1}} / ${{matches.length}}`;
}}

function scrollToMatch(idx) {{
  matches.forEach((m,i)=>m.classList.toggle('search-current',i===idx));
  matches[idx]?.scrollIntoView({{behavior:'smooth',block:'center'}});
}}

// ── url hash highlight ─────────────────────────────────────────────────────
(function() {{
  const h = location.hash;
  if(!h) return;
  const m = h === '#first-msg'
    ? document.querySelector('.msg-user')
    : document.getElementById(h.slice(1));
  if(!m) return;
  m.classList.add('search-current');
  m.scrollIntoView({{behavior:'smooth',block:'center'}});
}})();

// ── side panel toggle ──────────────────────────────────────────────────────
function togglePanel() {{
  const panel = document.getElementById('sidePanel');
  const btn   = document.getElementById('btnPanel');
  const hidden = panel.classList.toggle('hidden');
  btn.classList.toggle('active', !hidden);
  btn.textContent = hidden ? '⊞ Metrics' : '⊟ Metrics';
}}

// ── cost estimate ──────────────────────────────────────────────────────────
const SESSION_META = {session_meta_json};
const PRICING_DEFAULTS = {pricing_json};
const PRICING_LS_KEY = 'claudeReport.pricing';

function loadPricing() {{
  try {{
    const stored = JSON.parse(localStorage.getItem(PRICING_LS_KEY));
    if (Array.isArray(stored) && stored.length) return stored;
  }} catch (e) {{}}
  return PRICING_DEFAULTS;
}}
function pickRate(model, pricing) {{
  for (const r of pricing) {{
    if (r.pattern && model && model.includes(r.pattern)) return r;
  }}
  return pricing[pricing.length - 1];
}}
function modelCost(usage, rate) {{
  return (
    (usage.input      || 0) * (rate.input      || 0) +
    (usage.output     || 0) * (rate.output     || 0) +
    (usage.cache_read || 0) * (rate.cache_hit  || 0) +
    (usage.cache_5m   || 0) * (rate.cache_5m   || 0) +
    (usage.cache_1h   || 0) * (rate.cache_1h   || 0)
  ) / 1e6;
}}
function fmtUSD(v) {{
  if (v >= 1)   return '$' + v.toFixed(2);
  if (v >= 0.01) return '$' + v.toFixed(3);
  if (v > 0)    return '$' + v.toFixed(4);
  return '$0.00';
}}
function renderCost() {{
  const pricing = loadPricing();
  const mt = SESSION_META.model_tokens || {{}};
  let total = 0;
  const rows = [];
  Object.entries(mt).forEach(([model, u]) => {{
    const r = pickRate(model, pricing);
    const c = modelCost(u, r);
    total += c;
    rows.push({{model, label: r.label, cost: c}});
  }});
  document.getElementById('costTotal').textContent = fmtUSD(total);
  const bd = document.getElementById('costBreakdown');
  bd.innerHTML = rows.length
    ? rows.map(r => `<div class="sp-row"><span class="sp-label" title="${{r.model}}">${{r.label}}</span><span class="sp-val">${{fmtUSD(r.cost)}}</span></div>`).join('')
    : '<div class="sp-empty">No usage data</div>';
}}
renderCost();
window.addEventListener('storage', e => {{ if (e.key === PRICING_LS_KEY) renderCost(); }});

// ── tag widget ─────────────────────────────────────────────────────────────

const TAGS_LS_KEY = 'claudeReport.tags';

function loadAllTags() {{
  try {{ return JSON.parse(localStorage.getItem(TAGS_LS_KEY)) || {{}}; }}
  catch (e) {{ return {{}}; }}
}}
function saveAllTags(m) {{ localStorage.setItem(TAGS_LS_KEY, JSON.stringify(m)); }}

(function initTags() {{
  const all = loadAllTags();
  const entry = all[SESSION_META.session_id] || {{tags: [], notes: ''}};
  let tags = (entry.tags || []).slice();
  let notes = entry.notes || '';

  const chipsEl = document.getElementById('tagChips');
  const inputEl = document.getElementById('tagInput');
  const notesEl = document.getElementById('notesInput');
  const statusEl = document.getElementById('tagStatus');

  notesEl.value = notes;

  function escapeHtml(s) {{
    return String(s).replace(/[&<>"']/g, c => (
      {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]);
  }}
  function paintChips() {{
    chipsEl.innerHTML = '';
    tags.forEach((t, i) => {{
      const chip = document.createElement('span');
      chip.className = 'tag-chip';
      chip.innerHTML = `${{escapeHtml(t)}} <span class="x" data-i="${{i}}">×</span>`;
      chipsEl.appendChild(chip);
    }});
  }}
  function persist() {{
    const m = loadAllTags();
    if (tags.length === 0 && !notes) delete m[SESSION_META.session_id];
    else m[SESSION_META.session_id] = {{tags: tags, notes: notes, updated_at: new Date().toISOString()}};
    saveAllTags(m);
    statusEl.className = 'ok';
    statusEl.textContent = 'Saved';
    clearTimeout(persist._t);
    persist._t = setTimeout(() => {{ statusEl.textContent = ''; statusEl.className = ''; }}, 1200);
  }}

  paintChips();

  inputEl.addEventListener('keydown', e => {{
    if (e.key === 'Enter') {{
      e.preventDefault();
      const v = inputEl.value.trim();
      if (v && !tags.includes(v)) {{ tags.push(v); paintChips(); persist(); }}
      inputEl.value = '';
    }}
  }});
  chipsEl.addEventListener('click', e => {{
    const x = e.target.closest('.x');
    if (!x) return;
    tags.splice(parseInt(x.dataset.i, 10), 1);
    paintChips();
    persist();
  }});
  let notesTimer = null;
  notesEl.addEventListener('input', () => {{
    notes = notesEl.value;
    clearTimeout(notesTimer);
    notesTimer = setTimeout(persist, 400);
  }});
}})();
</script>
</body>
</html>"""


# ── report HTML ────────────────────────────────────────────────────────────

def build_report_html(sessions: list[dict], conv_dir_name: str) -> str:
    projects: dict[str, str] = {}
    for s in sessions:
        if s["project_key"] not in projects:
            name = s["project_name"] or s["project_key"].split("-")[-1]
            projects[s["project_key"]] = name

    project_options = "\n".join(
        f'<option value="{k}">{name}</option>'
        for k, name in sorted(projects.items(), key=lambda x: x[1])
    )

    # strip jsonl_path from data sent to browser (not needed)
    sessions_js = [{k: v for k, v in s.items() if k != "jsonl_path"} for s in sessions]
    data_json = json.dumps({
        "sessions": sessions_js,
        "projects": projects,
        "convDir": conv_dir_name,
        "pricingDefaults": PRICING_DEFAULTS,
    })

    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_template.html")
    with open(template_path) as tf:
        template = tf.read()
    generated = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    return (
        template
        .replace("__GENERATED__", generated)
        .replace("__SESSION_COUNT__", str(len(sessions)))
        .replace("__PROJECT_COUNT__", str(len(projects)))
        .replace("__PROJECT_OPTIONS__", project_options)
        .replace("__DATA_JSON__", data_json)
    )


# ── main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Claude Code conversation metrics report")
    parser.add_argument("project", nargs="?", help="Pre-filter by project path (default: all)")
    parser.add_argument("--output", default="claude_report.html", help="Output HTML file")
    parser.add_argument("--no-fetch", action="store_true",
                        help="Skip writing per-session conversation HTMLs; reuse existing folder")
    parser.add_argument("--source", choices=["both", "code", "desktop"], default="both",
                        help="Which transcript source(s) to scan (default: both)")
    args = parser.parse_args()

    sources = ("code", "desktop") if args.source == "both" else (args.source,)

    output_path = os.path.abspath(args.output)
    output_dir = os.path.dirname(output_path)
    output_stem = os.path.splitext(os.path.basename(output_path))[0]
    conv_dir = os.path.join(output_dir, f"{output_stem}_conversations")
    conv_dir_name = f"{output_stem}_conversations"

    scan_paths = ", ".join(SOURCES[s] for s in sources)
    print(f"Scanning: {scan_paths}", file=sys.stderr)
    sessions = collect_all_metrics(args.project, sources)

    if not sessions:
        print("No conversations found.", file=sys.stderr)
        sys.exit(1)

    projects = len({s["project_key"] for s in sessions})
    print(f"Found {len(sessions)} sessions across {projects} projects", file=sys.stderr)

    if args.no_fetch:
        if not os.path.isdir(conv_dir) or not any(f.endswith(".html") for f in os.listdir(conv_dir)):
            print(f"Error: --no-fetch requires existing conversation folder at {conv_dir}/", file=sys.stderr)
            sys.exit(1)
        print(f"Reusing existing conversation logs in {conv_dir}/", file=sys.stderr)
    else:
        # write per-session conversation HTMLs
        os.makedirs(conv_dir, exist_ok=True)
        print(f"Writing conversation logs to {conv_dir}/", file=sys.stderr)
        for i, session in enumerate(sessions, 1):
            try:
                messages = load_messages(session["jsonl_path"])
                turns = extract_turns(messages)
                conv_html = render_conversation_html(session, turns)
                conv_path = os.path.join(conv_dir, f"{session['session_id']}.html")
                with open(conv_path, "w") as f:
                    f.write(conv_html)
            except Exception as e:
                print(f"  Warning: {session['session_id']}: {e}", file=sys.stderr)
            if i % 50 == 0:
                print(f"  {i}/{len(sessions)} done", file=sys.stderr)

    # write main report
    report_html = build_report_html(sessions, conv_dir_name)
    with open(output_path, "w") as f:
        f.write(report_html)

    print(f"Report:        {output_path}", file=sys.stderr)
    print(f"Conversations: {conv_dir}/", file=sys.stderr)


if __name__ == "__main__":
    main()
