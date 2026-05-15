#!/usr/bin/env python3
"""
Generates an interactive HTML metrics report for all Claude Code conversations.
Also writes per-session chat log HTMLs to <output_stem>_conversations/.

Usage:
    python3 claude_metrics.py [project_path] [--output report.html]

    project_path: path to filter by project (default: all projects)
    --output:     output HTML file (default: claude_report.html)
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


# ── project helpers ────────────────────────────────────────────────────────

def get_project_key(path: str) -> str:
    # Claude encodes project paths by replacing "/" with "-" and stripping leading "-"
    return os.path.abspath(path).replace("/", "-").lstrip("-")


def find_jsonl_files(project_path: str | None = None) -> list[tuple[str, str]]:
    """Returns list of (project_key, jsonl_path) tuples."""
    base = os.path.expanduser("~/.claude/projects")
    if project_path:
        key = get_project_key(project_path)
        return [(key, f) for f in glob.glob(os.path.join(base, key, "*.jsonl"))]
    results = []
    for jsonl_path in glob.glob(os.path.join(base, "**", "*.jsonl"), recursive=True):
        project_key = os.path.basename(os.path.dirname(jsonl_path))
        results.append((project_key, jsonl_path))
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
    for m in assistant_msgs:
        usage = m.get("message", {}).get("usage", {})
        input_tokens += usage.get("input_tokens", 0)
        output_tokens += usage.get("output_tokens", 0)
        cache_read_tokens += usage.get("cache_read_input_tokens", 0)
        cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)

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
        "total_tokens": input_tokens + output_tokens,
        "models": models,
        "avg_user_msg_len": round(avg_user_len),
        "first_user_msg_len": first_user_msg_len,
        "first_user_msg_text": first_user_msg_text,
        "first_asst_msg_text": first_asst_msg_text,
    }


def collect_all_metrics(project_path: str | None = None) -> list[dict]:
    files = find_jsonl_files(project_path)
    sessions = []
    for project_key, jsonl_path in files:
        try:
            metrics = parse_session(jsonl_path)
            metrics["project_key"] = project_key
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
  body.hide-tools .tool-block,body.hide-tools .tool-result-block,body.hide-tools .thinking-block{{display:none}}
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
    <div class="sp-section">
      <h4>Top Tools</h4>
      {tools_html}
    </div>
  </div>
</div>

<script>
// ── filter chips ───────────────────────────────────────────────────────────
const chipState = {{user:true, asst:true, tools:true}};

function toggleChip(type) {{
  chipState[type] = !chipState[type];
  document.getElementById('chip'+type.charAt(0).toUpperCase()+type.slice(1))
    .classList.toggle('active', chipState[type]);
  applyFilters();
}}

function applyFilters() {{
  // tools = hide/show tool/thinking details blocks via body class
  document.body.classList.toggle('hide-tools', !chipState.tools);
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
    data_json = json.dumps({"sessions": sessions_js, "projects": projects, "convDir": conv_dir_name})

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code — Conversation Metrics</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg:#0f1117;--surface:#1a1d27;--surface2:#22263a;--border:#2e3347;
    --accent:#d97706;--accent2:#6366f1;--accent3:#10b981;
    --text:#e2e8f0;--muted:#64748b;--font:'Inter',system-ui,sans-serif;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:var(--font);font-size:14px}}
  header{{padding:24px 32px 0;border-bottom:1px solid var(--border)}}
  .header-top{{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;padding-bottom:16px}}
  header h1{{font-size:20px;font-weight:600}}
  header p{{color:var(--muted);font-size:13px}}
  .global-filter{{display:flex;align-items:center;gap:10px}}
  .global-filter label{{color:var(--muted);font-size:12px;font-weight:500;white-space:nowrap}}
  .global-filter select{{
    background:var(--surface2);border:1px solid var(--border);color:var(--text);
    border-radius:8px;padding:7px 12px;font-size:13px;outline:none;min-width:220px;cursor:pointer
  }}
  .global-filter select:focus{{border-color:var(--accent2)}}
  .kpi-row{{display:flex;gap:16px;padding:20px 32px;flex-wrap:wrap}}
  .kpi{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px 20px;flex:1;min-width:150px}}
  .kpi-label{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}}
  .kpi-value{{font-size:26px;font-weight:700;margin-top:4px}}
  .kpi-value.amber{{color:var(--accent)}}.kpi-value.indigo{{color:var(--accent2)}}.kpi-value.green{{color:var(--accent3)}}
  .charts-grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px;padding:0 32px 24px}}
  .charts-grid.wide{{grid-template-columns:2fr 1fr}}
  .chart-card{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:20px}}
  .chart-card h3{{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;margin-bottom:16px}}
  .chart-wrap{{position:relative;height:220px}}
  .chart-wrap.tall{{height:300px}}
  section{{padding:0 32px 32px}}
  section h2{{font-size:15px;font-weight:600;margin-bottom:14px}}
  .table-filter{{display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap}}
  .table-filter input{{
    background:var(--surface);border:1px solid var(--border);color:var(--text);
    border-radius:6px;padding:6px 10px;font-size:13px;outline:none;flex:1;min-width:200px
  }}
  table{{width:100%;border-collapse:collapse;font-size:13px}}
  thead th{{
    text-align:left;padding:8px 10px;color:var(--muted);font-weight:500;
    border-bottom:1px solid var(--border);font-size:11px;
    text-transform:uppercase;letter-spacing:.06em;cursor:pointer;user-select:none;white-space:nowrap
  }}
  thead th:hover{{color:var(--text)}}
  tbody tr{{border-bottom:1px solid var(--border);transition:background .1s;cursor:pointer}}
  tbody tr:hover{{background:var(--surface2)}}
  tbody td{{padding:8px 10px}}
  .title-cell{{max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  .title-link{{color:var(--text);text-decoration:none}}
  .title-link:hover{{color:var(--accent2);text-decoration:underline}}
  .project-cell{{max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted)}}
  .search-bar{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px}}
  .search-bar input[type=text]{{
    flex:1;min-width:220px;background:var(--surface);border:1px solid var(--border);color:var(--text);
    border-radius:6px;padding:7px 10px;font-size:13px;outline:none
  }}
  .search-bar input[type=text]:focus{{border-color:var(--accent2)}}
  .btn-adv{{
    background:var(--surface2);border:1px solid var(--border);color:var(--muted);
    border-radius:6px;padding:6px 12px;font-size:12px;cursor:pointer;white-space:nowrap
  }}
  .btn-adv:hover,.btn-adv.active{{color:var(--text);border-color:var(--accent2)}}
  .btn-reset{{
    background:transparent;border:1px solid var(--border);color:var(--muted);
    border-radius:6px;padding:6px 10px;font-size:12px;cursor:pointer
  }}
  .btn-reset:hover{{color:var(--accent);border-color:var(--accent)}}
  .adv-panel{{
    display:none;background:var(--surface);border:1px solid var(--border);
    border-radius:8px;padding:14px 16px;margin-bottom:10px;
    display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px 16px
  }}
  .adv-panel.hidden{{display:none!important}}
  .adv-field{{display:flex;flex-direction:column;gap:4px}}
  .adv-field label{{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}}
  .adv-field input,.adv-field select{{
    background:var(--surface2);border:1px solid var(--border);color:var(--text);
    border-radius:5px;padding:5px 8px;font-size:12px;outline:none;width:100%
  }}
  .adv-field input:focus,.adv-field select:focus{{border-color:var(--accent2)}}
  .adv-range{{display:flex;gap:4px;align-items:center}}
  .adv-range input{{flex:1}}
  .adv-range span{{color:var(--muted);font-size:11px}}
  .result-count{{font-size:12px;color:var(--muted);margin-bottom:8px}}
  .result-count strong{{color:var(--text)}}
  .tabs{{display:flex;gap:2px;padding:0 32px;border-bottom:1px solid var(--border);margin-top:0}}
  .tab-btn{{
    padding:10px 18px;font-size:13px;font-weight:500;cursor:pointer;
    background:transparent;border:none;color:var(--muted);
    border-bottom:2px solid transparent;margin-bottom:-1px;transition:color .15s
  }}
  .tab-btn:hover{{color:var(--text)}}
  .tab-btn.active{{color:var(--text);border-bottom-color:var(--accent2)}}
  .tab-pane{{display:none}}.tab-pane.active{{display:block}}
  .scatter-note{{font-size:11px;color:var(--muted);margin-bottom:8px;padding:0 32px}}
  /* chart card actions */
  .chart-card{{position:relative}}
  .card-actions{{position:absolute;top:14px;right:14px;display:flex;gap:6px;opacity:0;transition:opacity .15s}}
  .chart-card:hover .card-actions{{opacity:1}}
  .btn-icon{{
    background:var(--surface2);border:1px solid var(--border);color:var(--muted);
    border-radius:5px;padding:3px 7px;font-size:12px;cursor:pointer;line-height:1
  }}
  .btn-icon:hover{{color:var(--text);border-color:var(--accent2)}}
  /* fullscreen overlay */
  .chart-overlay{{
    display:none;position:fixed;inset:0;z-index:900;background:rgba(0,0,0,.7);
    align-items:center;justify-content:center
  }}
  .chart-overlay.open{{display:flex}}
  .chart-overlay-inner{{
    background:var(--surface);border:1px solid var(--border);border-radius:12px;
    padding:24px;width:92vw;height:88vh;display:flex;flex-direction:column;gap:12px
  }}
  .chart-overlay-header{{display:flex;align-items:center;justify-content:space-between}}
  .chart-overlay-header h3{{font-size:14px;font-weight:600;color:var(--text)}}
  .chart-overlay-wrap{{flex:1;position:relative}}
  /* session picker modal */
  .picker-overlay{{
    display:none;position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,.6);
    align-items:center;justify-content:center
  }}
  .picker-overlay.open{{display:flex}}
  .picker-inner{{
    background:var(--surface);border:1px solid var(--border);border-radius:10px;
    width:540px;max-width:96vw;max-height:70vh;display:flex;flex-direction:column
  }}
  .picker-header{{
    display:flex;align-items:center;justify-content:space-between;
    padding:14px 16px;border-bottom:1px solid var(--border)
  }}
  .picker-header span{{font-size:14px;font-weight:600}}
  .picker-list{{overflow-y:auto;flex:1}}
  .picker-row{{
    display:flex;flex-direction:column;gap:2px;padding:10px 16px;
    border-bottom:1px solid var(--border);cursor:pointer;transition:background .1s
  }}
  .picker-row:hover{{background:var(--surface2)}}
  .picker-row-title{{font-size:13px;color:var(--text)}}
  .picker-row-meta{{font-size:11px;color:var(--muted)}}
  .picker-empty{{padding:24px;text-align:center;color:var(--muted);font-size:13px}}
  @media(max-width:800px){{
    .charts-grid,.charts-grid.wide{{grid-template-columns:1fr}}
    .header-top{{flex-direction:column;align-items:flex-start}}
  }}
</style>
</head>
<body>
<header>
  <div class="header-top">
    <div>
      <h1>Claude Code — Conversation Metrics</h1>
      <p>Generated {datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")} &nbsp;·&nbsp; {len(sessions)} sessions across {len(projects)} projects</p>
    </div>
    <div class="global-filter">
      <label>Filter by project</label>
      <select id="globalProject" onchange="applyGlobalFilter()">
        <option value="">All projects ({len(projects)})</option>
        {project_options}
      </select>
    </div>
  </div>
</header>

<div class="tabs">
  <button class="tab-btn active" onclick="switchTab('overview',this)">Overview</button>
  <button class="tab-btn" onclick="switchTab('deepdive',this)">Deep Dive</button>
</div>

<div id="tab-overview" class="tab-pane active">
<div class="kpi-row">
  <div class="kpi"><div class="kpi-label">Sessions</div><div class="kpi-value amber" id="kpi-sessions">—</div></div>
  <div class="kpi"><div class="kpi-label">Total Tokens</div><div class="kpi-value indigo" id="kpi-tokens">—</div></div>
  <div class="kpi"><div class="kpi-label">Tool Uses</div><div class="kpi-value green" id="kpi-tools">—</div></div>
  <div class="kpi"><div class="kpi-label">User Messages</div><div class="kpi-value" id="kpi-msgs">—</div></div>
  <div class="kpi"><div class="kpi-label">Avg Session (min)</div><div class="kpi-value" id="kpi-duration">—</div></div>
</div>

<div class="charts-grid wide">
  <div class="chart-card"><h3>Sessions &amp; Tokens over Time</h3><div class="chart-wrap"><canvas id="timelineChart"></canvas></div></div>
  <div class="chart-card"><h3>Tokens by Project</h3><div class="chart-wrap"><canvas id="projectChart"></canvas></div></div>
</div>
<div class="charts-grid">
  <div class="chart-card"><h3>Top Tools Used</h3><div class="chart-wrap tall"><canvas id="toolsChart"></canvas></div></div>
  <div class="chart-card"><h3>Token Mix (Input / Output / Cache)</h3><div class="chart-wrap"><canvas id="tokenMixChart"></canvas></div></div>
</div>
<div class="charts-grid" style="grid-template-columns:1fr">
  <div class="chart-card"><h3>Sessions &amp; Avg Tokens per Session — by Model &nbsp;<span style="font-weight:400;color:#64748b">(green = agentic: Agent tool OR &gt;35 tools OR TaskCreate)</span></h3><div class="chart-wrap"><canvas id="modelChart"></canvas></div></div>
</div>

<section>
  <h2>Sessions</h2>

  <div class="search-bar">
    <input type="text" id="searchInput" placeholder="Search title or project…" oninput="renderTable()">
    <button class="btn-adv" id="btnAdv" onclick="toggleAdv()">Advanced ▾</button>
    <button class="btn-reset" onclick="resetFilters()">Reset</button>
  </div>

  <div class="adv-panel hidden" id="advPanel">
    <div class="adv-field">
      <label>Date from</label>
      <input type="date" id="fDateFrom" onchange="renderTable()">
    </div>
    <div class="adv-field">
      <label>Date to</label>
      <input type="date" id="fDateTo" onchange="renderTable()">
    </div>
    <div class="adv-field">
      <label>Tokens</label>
      <div class="adv-range">
        <input type="number" id="fTokMin" placeholder="min" min="0" oninput="renderTable()">
        <span>–</span>
        <input type="number" id="fTokMax" placeholder="max" min="0" oninput="renderTable()">
      </div>
    </div>
    <div class="adv-field">
      <label>Duration (min)</label>
      <div class="adv-range">
        <input type="number" id="fDurMin" placeholder="min" min="0" oninput="renderTable()">
        <span>–</span>
        <input type="number" id="fDurMax" placeholder="max" min="0" oninput="renderTable()">
      </div>
    </div>
    <div class="adv-field">
      <label>Messages (total)</label>
      <div class="adv-range">
        <input type="number" id="fMsgMin" placeholder="min" min="0" oninput="renderTable()">
        <span>–</span>
        <input type="number" id="fMsgMax" placeholder="max" min="0" oninput="renderTable()">
      </div>
    </div>
    <div class="adv-field">
      <label>Tool uses</label>
      <div class="adv-range">
        <input type="number" id="fToolMin" placeholder="min" min="0" oninput="renderTable()">
        <span>–</span>
        <input type="number" id="fToolMax" placeholder="max" min="0" oninput="renderTable()">
      </div>
    </div>
    <div class="adv-field">
      <label>Model</label>
      <select id="fModel" onchange="renderTable()">
        <option value="">Any model</option>
      </select>
    </div>
    <div class="adv-field">
      <label>Cache hits</label>
      <div class="adv-range">
        <input type="number" id="fCacheMin" placeholder="min" min="0" oninput="renderTable()">
        <span>–</span>
        <input type="number" id="fCacheMax" placeholder="max" min="0" oninput="renderTable()">
      </div>
    </div>
  </div>

  <div class="result-count" id="resultCount"></div>
  <table id="sessionsTable">
    <thead>
      <tr>
        <th onclick="sortBy('start_time')">Date ↕</th>
        <th onclick="sortBy('project_name')">Project ↕</th>
        <th>Title</th>
        <th onclick="sortBy('user_messages')">User ↕</th>
        <th onclick="sortBy('assistant_messages')">Asst ↕</th>
        <th onclick="sortBy('tool_uses')">Tools ↕</th>
        <th onclick="sortBy('total_tokens')">Tokens ↕</th>
        <th onclick="sortBy('cache_read_tokens')">Cache ↕</th>
        <th onclick="sortBy('duration_mins')">Duration ↕</th>
      </tr>
    </thead>
    <tbody id="tableBody"></tbody>
  </table>
</section>
</div><!-- /tab-overview -->

<div id="tab-deepdive" class="tab-pane">

<div style="padding:20px 32px 0">
  <div class="search-bar">
    <input type="text" id="ddSearch" placeholder="Search in first message or first response…" oninput="ddSearchSessions()">
    <select id="ddSearchTarget" onchange="ddSearchSessions()" style="background:var(--surface2);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:6px 10px;font-size:13px;outline:none">
      <option value="both">Both</option>
      <option value="user">First user message</option>
      <option value="asst">First response</option>
    </select>
    <button class="btn-reset" onclick="document.getElementById('ddSearch').value='';ddSearchSessions()">Reset</button>
  </div>
  <div class="result-count" id="ddResultCount"></div>
  <div id="ddResults" style="display:none;max-height:260px;overflow-y:auto;border:1px solid var(--border);border-radius:8px;margin-bottom:16px;background:var(--surface)"></div>
</div>

<div class="charts-grid wide" style="padding-top:16px">
  <div class="chart-card">
    <h3>First Message Length → Total Tokens <span style="font-weight:400;color:#64748b">(prompt complexity vs session size)</span></h3>
    <div class="chart-wrap"><canvas id="scatterMsgTokens"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>Total Messages → Tool Uses <span style="font-weight:400;color:#64748b">(chatty vs tool-heavy)</span></h3>
    <div class="chart-wrap"><canvas id="scatterMsgTools"></canvas></div>
  </div>
</div>
<div class="charts-grid" style="padding-top:0">
  <div class="chart-card">
    <h3>Activity by Hour of Day</h3>
    <div class="chart-wrap"><canvas id="hourChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>Activity by Day of Week</h3>
    <div class="chart-wrap"><canvas id="dowChart"></canvas></div>
  </div>
</div>
<div class="charts-grid wide" style="padding-top:0;padding-bottom:32px">
  <div class="chart-card">
    <h3>Cache Efficiency over Time <span style="font-weight:400;color:#64748b">(cache reads / total input %)</span></h3>
    <div class="chart-wrap"><canvas id="cacheEffChart"></canvas></div>
  </div>
  <div class="chart-card">
    <h3>Token Distribution <span style="font-weight:400;color:#64748b">(sessions per bucket)</span></h3>
    <div class="chart-wrap"><canvas id="tokenHistChart"></canvas></div>
  </div>
</div>
</div><!-- /tab-deepdive -->

<!-- fullscreen chart overlay -->
<div class="chart-overlay" id="chartOverlay" onclick="closeExpand(event)">
  <div class="chart-overlay-inner" onclick="event.stopPropagation()">
    <div class="chart-overlay-header">
      <h3 id="overlayTitle"></h3>
      <button class="btn-icon" onclick="closeExpand()">✕ close</button>
    </div>
    <div class="chart-overlay-wrap"><canvas id="overlayCanvas"></canvas></div>
  </div>
</div>

<!-- session picker modal -->
<div class="picker-overlay" id="pickerOverlay" onclick="closePicker(event)">
  <div class="picker-inner" onclick="event.stopPropagation()">
    <div class="picker-header">
      <span id="pickerTitle">Sessions</span>
      <button class="btn-icon" onclick="closePicker()">✕</button>
    </div>
    <div class="picker-list" id="pickerList"></div>
  </div>
</div>

<script>
const DATA = {data_json};
const COLORS = ['#d97706','#6366f1','#10b981','#f59e0b','#3b82f6','#ec4899','#14b8a6','#8b5cf6','#f97316','#06b6d4','#a855f7','#ef4444','#84cc16','#0ea5e9','#fb7185'];
const GC = {{ gridColor:'#1e2335', tickColor:'#64748b' }};

let filteredSessions = DATA.sessions;
let sortKey = 'start_time', sortAsc = false;

function fmt(n) {{ return n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':String(n); }}

function updateKPIs(ss) {{
  document.getElementById('kpi-sessions').textContent = ss.length;
  document.getElementById('kpi-tokens').textContent = fmt(ss.reduce((a,s)=>a+s.total_tokens,0));
  document.getElementById('kpi-tools').textContent = fmt(ss.reduce((a,s)=>a+s.tool_uses,0));
  document.getElementById('kpi-msgs').textContent = fmt(ss.reduce((a,s)=>a+s.user_messages,0));
  const d = ss.filter(s=>s.duration_mins>0).map(s=>s.duration_mins);
  document.getElementById('kpi-duration').textContent = d.length?(d.reduce((a,b)=>a+b,0)/d.length).toFixed(1):'—';
}}

function computeTimeline(ss) {{
  const m={{}};
  ss.forEach(s=>{{ if(!s.start_time)return; const d=s.start_time.slice(0,10); m[d]=m[d]||{{s:0,t:0}}; m[d].s++; m[d].t+=s.total_tokens; }});
  const dates=Object.keys(m).sort();
  return {{dates, sessions:dates.map(d=>m[d].s), tokens:dates.map(d=>m[d].t)}};
}}
function computeProjectTokens(ss) {{
  const m={{}};
  ss.forEach(s=>{{ const n=DATA.projects[s.project_key]||s.project_key.split('-').pop(); m[n]=(m[n]||0)+s.total_tokens; }});
  const sorted=Object.entries(m).sort((a,b)=>b[1]-a[1]);
  return {{labels:sorted.map(e=>e[0]),values:sorted.map(e=>e[1])}};
}}
function computeTopTools(ss,n=15) {{
  const c={{}};
  ss.forEach(s=>Object.entries(s.tool_counts).forEach(([k,v])=>c[k]=(c[k]||0)+v));
  return Object.entries(c).sort((a,b)=>b[1]-a[1]).slice(0,n);
}}
function computeTokenMix(ss) {{
  return ['input_tokens','output_tokens','cache_read_tokens','cache_creation_tokens'].map(k=>ss.reduce((a,s)=>a+s[k],0));
}}
// Composite agentic heuristic:
//   spawned a subagent (Agent tool)
//   OR top-25% tool usage (>35 calls — p75 across all sessions)
//   OR used task management (TaskCreate)
const AGENTIC_TOOL_THRESHOLD = 35;
function isAgentic(s) {{
  const tc = s.tool_counts || {{}};
  return tc['Agent'] > 0 || s.tool_uses > AGENTIC_TOOL_THRESHOLD || tc['TaskCreate'] > 0;
}}

function computeModelStats(ss) {{
  const counts={{}}, tokenSums={{}}, agentic={{}};
  ss.forEach(s=>{{
    const m=(s.models&&s.models.length)?s.models[0]:'unknown';
    counts[m]=(counts[m]||0)+1;
    tokenSums[m]=(tokenSums[m]||0)+s.total_tokens;
    if(isAgentic(s)) agentic[m]=(agentic[m]||0)+1;
  }});
  const labels=Object.keys(counts).sort((a,b)=>counts[b]-counts[a]);
  return {{
    labels,
    sessions:labels.map(m=>counts[m]),
    agenticSessions:labels.map(m=>agentic[m]||0),
    avgTokens:labels.map(m=>Math.round(tokenSums[m]/counts[m])),
  }};
}}

const tlChart = new Chart(document.getElementById('timelineChart'),{{
  type:'bar',
  data:{{labels:[],datasets:[
    {{label:'Sessions',data:[],backgroundColor:'#d97706aa',borderColor:'#d97706',borderWidth:1,yAxisID:'y'}},
    {{label:'Tokens',data:[],type:'line',borderColor:'#6366f1',backgroundColor:'#6366f122',fill:true,tension:0.3,pointRadius:3,yAxisID:'y2'}},
  ]}},
  options:{{responsive:true,maintainAspectRatio:false,
    plugins:{{legend:{{labels:{{color:'#94a3b8',font:{{size:11}}}}}}}},
    scales:{{
      x:{{ticks:{{color:GC.tickColor,maxRotation:45,font:{{size:10}}}},grid:{{color:GC.gridColor}}}},
      y:{{ticks:{{color:GC.tickColor,font:{{size:10}}}},grid:{{color:GC.gridColor}},title:{{display:true,text:'Sessions',color:GC.tickColor,font:{{size:10}}}}}},
      y2:{{position:'right',ticks:{{color:'#6366f1',font:{{size:10}}}},grid:{{drawOnChartArea:false}},title:{{display:true,text:'Tokens',color:'#6366f1',font:{{size:10}}}}}},
    }}
  }}
}});

const pjChart = new Chart(document.getElementById('projectChart'),{{
  type:'doughnut',data:{{labels:[],datasets:[{{data:[],backgroundColor:COLORS,borderWidth:0}}]}},
  options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{position:'right',labels:{{color:'#94a3b8',font:{{size:10}},boxWidth:12,padding:8}}}}}}}}
}});

const toolChart = new Chart(document.getElementById('toolsChart'),{{
  type:'bar',data:{{labels:[],datasets:[{{data:[],backgroundColor:COLORS,borderWidth:0}}]}},
  options:{{indexAxis:'y',responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}}}},
    scales:{{x:{{ticks:{{color:GC.tickColor,font:{{size:10}}}},grid:{{color:GC.gridColor}}}},y:{{ticks:{{color:'#94a3b8',font:{{size:11}}}},grid:{{display:false}}}}}}
  }}
}});

const mixChart = new Chart(document.getElementById('tokenMixChart'),{{
  type:'pie',
  data:{{labels:['Input','Output','Cache Read','Cache Creation'],datasets:[{{data:[0,0,0,0],backgroundColor:['#6366f1','#10b981','#f59e0b','#d97706'],borderWidth:0}}]}},
  options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{position:'bottom',labels:{{color:'#94a3b8',font:{{size:11}},boxWidth:12,padding:10}}}}}}}}
}});

const modelChart = new Chart(document.getElementById('modelChart'),{{
  type:'bar',
  data:{{labels:[],datasets:[
    {{label:'Sessions',data:[],backgroundColor:'#6366f1cc',borderColor:'#6366f1',borderWidth:1,yAxisID:'y'}},
    {{label:'Agentic sessions',data:[],backgroundColor:'#10b981cc',borderColor:'#10b981',borderWidth:1,yAxisID:'y'}},
    {{label:'Avg tokens/session',data:[],backgroundColor:'#d97706cc',borderColor:'#d97706',borderWidth:1,yAxisID:'y2'}},
  ]}},
  options:{{
    responsive:true,maintainAspectRatio:false,
    plugins:{{legend:{{labels:{{color:'#94a3b8',font:{{size:11}}}}}}}},
    scales:{{
      x:{{ticks:{{color:'#94a3b8',font:{{size:11}}}},grid:{{color:GC.gridColor}}}},
      y:{{ticks:{{color:GC.tickColor,font:{{size:10}}}},grid:{{color:GC.gridColor}},title:{{display:true,text:'Sessions',color:GC.tickColor,font:{{size:10}}}}}},
      y2:{{position:'right',ticks:{{color:'#d97706',font:{{size:10}}}},grid:{{drawOnChartArea:false}},title:{{display:true,text:'Avg tokens',color:'#d97706',font:{{size:10}}}}}},
    }}
  }}
}});

function updateCharts(ss) {{
  const tl=computeTimeline(ss);
  tlChart.data.labels=tl.dates; tlChart.data.datasets[0].data=tl.sessions; tlChart.data.datasets[1].data=tl.tokens; tlChart.update();
  const pt=computeProjectTokens(ss);
  pjChart.data.labels=pt.labels; pjChart.data.datasets[0].data=pt.values; pjChart.update();
  const tools=computeTopTools(ss);
  toolChart.data.labels=tools.map(t=>t[0]); toolChart.data.datasets[0].data=tools.map(t=>t[1]); toolChart.update();
  mixChart.data.datasets[0].data=computeTokenMix(ss); mixChart.update();
  const ms=computeModelStats(ss);
  modelChart.data.labels=ms.labels;
  modelChart.data.datasets[0].data=ms.sessions;
  modelChart.data.datasets[1].data=ms.agenticSessions;
  modelChart.data.datasets[2].data=ms.avgTokens;
  modelChart.update();
}}

// ── advanced panel toggle ──────────────────────────────────────────────────
function toggleAdv() {{
  const panel=document.getElementById('advPanel');
  const btn=document.getElementById('btnAdv');
  const hidden=panel.classList.toggle('hidden');
  btn.classList.toggle('active',!hidden);
  btn.textContent=hidden?'Advanced ▾':'Advanced ▴';
}}

function resetFilters() {{
  document.getElementById('searchInput').value='';
  ['fDateFrom','fDateTo','fTokMin','fTokMax','fDurMin','fDurMax',
   'fMsgMin','fMsgMax','fToolMin','fToolMax','fCacheMin','fCacheMax'].forEach(id=>{{
    document.getElementById(id).value='';
  }});
  document.getElementById('fModel').value='';
  renderTable();
}}

function gv(id) {{ return document.getElementById(id).value; }}
function gn(id) {{ const v=parseFloat(gv(id)); return isNaN(v)?null:v; }}

function applyAdvancedFilters(sessions) {{
  const q=gv('searchInput').toLowerCase();
  const dateFrom=gv('fDateFrom'), dateTo=gv('fDateTo');
  const tokMin=gn('fTokMin'), tokMax=gn('fTokMax');
  const durMin=gn('fDurMin'), durMax=gn('fDurMax');
  const msgMin=gn('fMsgMin'), msgMax=gn('fMsgMax');
  const toolMin=gn('fToolMin'), toolMax=gn('fToolMax');
  const cacheMin=gn('fCacheMin'), cacheMax=gn('fCacheMax');
  const model=gv('fModel');

  return sessions.filter(s=>{{
    if(q && !s.title.toLowerCase().includes(q) && !s.project_name.toLowerCase().includes(q)) return false;
    const date=s.start_time?s.start_time.slice(0,10):'';
    if(dateFrom && date < dateFrom) return false;
    if(dateTo   && date > dateTo)   return false;
    if(tokMin  !== null && s.total_tokens   < tokMin)  return false;
    if(tokMax  !== null && s.total_tokens   > tokMax)  return false;
    if(durMin  !== null && s.duration_mins  < durMin)  return false;
    if(durMax  !== null && s.duration_mins  > durMax)  return false;
    if(msgMin  !== null && s.total_messages < msgMin)  return false;
    if(msgMax  !== null && s.total_messages > msgMax)  return false;
    if(toolMin !== null && s.tool_uses      < toolMin) return false;
    if(toolMax !== null && s.tool_uses      > toolMax) return false;
    if(cacheMin !== null && s.cache_read_tokens < cacheMin) return false;
    if(cacheMax !== null && s.cache_read_tokens > cacheMax) return false;
    if(model && !(s.models||[]).includes(model)) return false;
    return true;
  }});
}}

function renderTable() {{
  const rows=applyAdvancedFilters(filteredSessions)
    .sort((a,b)=>{{
      const av=a[sortKey],bv=b[sortKey];
      const cmp=typeof av==='number'?av-bv:String(av||'').localeCompare(String(bv||''));
      return sortAsc?cmp:-cmp;
    }});
  const total=filteredSessions.length;
  document.getElementById('resultCount').innerHTML=
    rows.length===total
      ? `<strong>${{total}}</strong> sessions`
      : `<strong>${{rows.length}}</strong> of <strong>${{total}}</strong> sessions`;
  document.getElementById('tableBody').innerHTML=rows.map(s=>{{
    const date=s.start_time?s.start_time.slice(0,10):'?';
    const convUrl=DATA.convDir+'/'+s.session_id+'.html';
    return `<tr onclick="window.open('${{convUrl}}','_blank')">
      <td>${{date}}</td>
      <td class="project-cell" title="${{s.cwd}}">${{s.project_name}}</td>
      <td class="title-cell"><a class="title-link" href="${{convUrl}}" target="_blank" onclick="event.stopPropagation()">${{s.title}}</a></td>
      <td>${{s.user_messages}}</td>
      <td>${{s.assistant_messages}}</td>
      <td>${{s.tool_uses}}</td>
      <td>${{s.total_tokens.toLocaleString()}}</td>
      <td>${{s.cache_read_tokens.toLocaleString()}}</td>
      <td>${{s.duration_mins}}m</td>
    </tr>`;
  }}).join('');
}}

function sortBy(key) {{
  if(sortKey===key) sortAsc=!sortAsc; else {{sortKey=key;sortAsc=false;}}
  renderTable();
}}

function populateModelDropdown() {{
  const models=[...new Set(DATA.sessions.flatMap(s=>s.models||[]))].sort();
  const sel=document.getElementById('fModel');
  models.forEach(m=>{{ const o=document.createElement('option'); o.value=m; o.textContent=m; sel.appendChild(o); }});
}}

function applyGlobalFilter() {{
  const pk=document.getElementById('globalProject').value;
  filteredSessions=pk?DATA.sessions.filter(s=>s.project_key===pk):DATA.sessions;
  updateKPIs(filteredSessions);
  updateCharts(filteredSessions);
  renderTable();
  if(document.getElementById('tab-deepdive').classList.contains('active'))
    updateDeepDive(filteredSessions);
}}

// ── tab switching ──────────────────────────────────────────────────────────
function switchTab(id, btn) {{
  document.querySelectorAll('.tab-pane').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('tab-'+id).classList.add('active');
  btn.classList.add('active');
  if(id==='deepdive') updateDeepDive(filteredSessions);
}}

// ── deep dive compute ──────────────────────────────────────────────────────
function computeHourActivity(ss) {{
  const counts=Array(24).fill(0);
  ss.forEach(s=>{{ if(s.hour>=0) counts[s.hour]++; }});
  return counts;
}}

function computeDowActivity(ss) {{
  const counts=Array(7).fill(0);
  ss.forEach(s=>{{ if(s.dow>=0) counts[s.dow]++; }});
  return counts;
}}

function computeCacheEff(ss) {{
  const byDate={{}};
  ss.forEach(s=>{{
    if(!s.start_time) return;
    const d=s.start_time.slice(0,10);
    byDate[d]=byDate[d]||{{cr:0,inp:0}};
    byDate[d].cr+=s.cache_read_tokens;
    byDate[d].inp+=s.input_tokens+s.cache_read_tokens+s.cache_creation_tokens;
  }});
  const dates=Object.keys(byDate).sort();
  return {{dates, eff:dates.map(d=>byDate[d].inp>0?Math.round(byDate[d].cr/byDate[d].inp*100):0)}};
}}

function computeTokenHist(ss) {{
  const buckets=[
    [0,      5000,  '0–5K'],
    [5000,   20000, '5–20K'],
    [20000,  50000, '20–50K'],
    [50000,  100000,'50–100K'],
    [100000, 200000,'100–200K'],
    [200000, 500000,'200–500K'],
    [500000, Infinity,'500K+'],
  ];
  const counts=buckets.map(()=>0);
  ss.forEach(s=>{{
    const i=buckets.findIndex(([lo,hi])=>s.total_tokens>=lo&&s.total_tokens<hi);
    if(i>=0) counts[i]++;
  }});
  return {{labels:buckets.map(b=>b[2]), counts}};
}}

function scatterData(ss, xKey, yKey) {{
  return ss
    .filter(s=>s[xKey]>0&&s[yKey]>0)
    .map(s=>({{x:s[xKey], y:s[yKey], label:s.title||s.session_id.slice(0,8)}}));
}}

const scatterOpts = (xlabel, ylabel) => ({{
  responsive:true, maintainAspectRatio:false,
  plugins:{{
    legend:{{display:false}},
    tooltip:{{callbacks:{{label:ctx=>`${{ctx.raw.label}} (${{ctx.raw.x.toLocaleString()}}, ${{ctx.raw.y.toLocaleString()}})`}}}}
  }},
  scales:{{
    x:{{ticks:{{color:'#64748b',font:{{size:10}}}},grid:{{color:'#1e2335'}},title:{{display:true,text:xlabel,color:'#64748b',font:{{size:10}}}}}},
    y:{{ticks:{{color:'#64748b',font:{{size:10}}}},grid:{{color:'#1e2335'}},title:{{display:true,text:ylabel,color:'#64748b',font:{{size:10}}}}}},
  }}
}});

// ── deep dive chart instances ──────────────────────────────────────────────
const scMsgTok = new Chart(document.getElementById('scatterMsgTokens'),{{
  type:'scatter',
  data:{{datasets:[
    {{label:'Regular',data:[],backgroundColor:'#6366f188',pointRadius:4,pointHoverRadius:6}},
    {{label:'Agentic',data:[],backgroundColor:'#10b98188',pointRadius:5,pointHoverRadius:7}},
  ]}},
  options:scatterOpts('First message length (chars)','Total tokens'),
}});

const scMsgTools = new Chart(document.getElementById('scatterMsgTools'),{{
  type:'scatter',
  data:{{datasets:[
    {{label:'Regular',data:[],backgroundColor:'#d9770688',pointRadius:4,pointHoverRadius:6}},
    {{label:'Agentic',data:[],backgroundColor:'#10b98188',pointRadius:5,pointHoverRadius:7}},
  ]}},
  options:scatterOpts('Total messages','Tool uses'),
}});

const hourChart = new Chart(document.getElementById('hourChart'),{{
  type:'bar',
  data:{{
    labels:Array.from({{length:24}},(_,i)=>i+'h'),
    datasets:[{{data:Array(24).fill(0),backgroundColor:'#6366f1cc',borderWidth:0}}]
  }},
  options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}}}},
    scales:{{x:{{ticks:{{color:'#64748b',font:{{size:10}}}},grid:{{color:'#1e2335'}}}},y:{{ticks:{{color:'#64748b',font:{{size:10}}}},grid:{{color:'#1e2335'}}}}}}
  }}
}});

const dowChart = new Chart(document.getElementById('dowChart'),{{
  type:'bar',
  data:{{
    labels:['Mon','Tue','Wed','Thu','Fri','Sat','Sun'],
    datasets:[{{data:Array(7).fill(0),backgroundColor:['#6366f1cc','#6366f1cc','#6366f1cc','#6366f1cc','#6366f1cc','#d97706cc','#d97706cc'],borderWidth:0}}]
  }},
  options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}}}},
    scales:{{x:{{ticks:{{color:'#64748b',font:{{size:11}}}},grid:{{color:'#1e2335'}}}},y:{{ticks:{{color:'#64748b',font:{{size:10}}}},grid:{{color:'#1e2335'}}}}}}
  }}
}});

const cacheEffChart = new Chart(document.getElementById('cacheEffChart'),{{
  type:'line',
  data:{{labels:[],datasets:[{{label:'Cache eff %',data:[],borderColor:'#10b981',backgroundColor:'#10b98122',fill:true,tension:0.3,pointRadius:3}}]}},
  options:{{responsive:true,maintainAspectRatio:false,
    plugins:{{legend:{{display:false}}}},
    scales:{{
      x:{{ticks:{{color:'#64748b',maxRotation:45,font:{{size:10}}}},grid:{{color:'#1e2335'}}}},
      y:{{min:0,max:100,ticks:{{color:'#64748b',font:{{size:10}},callback:v=>v+'%'}},grid:{{color:'#1e2335'}}}},
    }}
  }}
}});

const tokenHistChart = new Chart(document.getElementById('tokenHistChart'),{{
  type:'bar',
  data:{{labels:[],datasets:[{{data:[],backgroundColor:COLORS,borderWidth:0}}]}},
  options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}}}},
    scales:{{x:{{ticks:{{color:'#94a3b8',font:{{size:11}}}},grid:{{display:false}}}},y:{{ticks:{{color:'#64748b',font:{{size:10}}}},grid:{{color:'#1e2335'}}}}}}
  }}
}});

function updateDeepDive(ss) {{
  // scatter: split agentic vs regular
  const reg=ss.filter(s=>!isAgentic(s));
  const agt=ss.filter(s=>isAgentic(s));
  scMsgTok.data.datasets[0].data=scatterData(reg,'first_user_msg_len','total_tokens');
  scMsgTok.data.datasets[1].data=scatterData(agt,'first_user_msg_len','total_tokens');
  scMsgTok.update();
  scMsgTools.data.datasets[0].data=scatterData(reg,'total_messages','tool_uses');
  scMsgTools.data.datasets[1].data=scatterData(agt,'total_messages','tool_uses');
  scMsgTools.update();

  hourChart.data.datasets[0].data=computeHourActivity(ss); hourChart.update();
  dowChart.data.datasets[0].data=computeDowActivity(ss); dowChart.update();

  const ce=computeCacheEff(ss);
  cacheEffChart.data.labels=ce.dates; cacheEffChart.data.datasets[0].data=ce.eff; cacheEffChart.update();

  const th=computeTokenHist(ss);
  tokenHistChart.data.labels=th.labels; tokenHistChart.data.datasets[0].data=th.counts; tokenHistChart.update();
}}

// ── expand to fullscreen ───────────────────────────────────────────────────
let overlayChart = null;

function expandChart(canvasId, title) {{
  const src = Chart.getChart(canvasId);
  if(!src) return;
  document.getElementById('overlayTitle').textContent = title;
  document.getElementById('chartOverlay').classList.add('open');
  if(overlayChart) {{ overlayChart.destroy(); overlayChart = null; }}
  const cfg = {{
    type: src.config.type,
    data: JSON.parse(JSON.stringify(src.data)),
    options: JSON.parse(JSON.stringify(src.options))
  }};
  if(src.config.options?.onClick) cfg.options.onClick = src.config.options.onClick;
  overlayChart = new Chart(document.getElementById('overlayCanvas'), cfg);
}}

function closeExpand(e) {{
  if(e && e.target !== document.getElementById('chartOverlay')) return;
  document.getElementById('chartOverlay').classList.remove('open');
  if(overlayChart) {{ overlayChart.destroy(); overlayChart = null; }}
}}

// ── session picker ─────────────────────────────────────────────────────────
function openSessionPicker(sessions, title) {{
  if(!sessions.length) return;
  if(sessions.length === 1) {{ openSession(sessions[0].session_id); return; }}
  document.getElementById('pickerTitle').textContent = title + ' — ' + sessions.length + ' sessions';
  const list = document.getElementById('pickerList');
  list.innerHTML = sessions.length
    ? sessions.map(s=>{{
        const date = s.start_time ? s.start_time.slice(0,10) : '?';
        const tok = s.total_tokens.toLocaleString();
        return `<div class="picker-row" onclick="openSession('${{s.session_id}}')">
          <div class="picker-row-title">${{s.title||s.session_id.slice(0,8)}}</div>
          <div class="picker-row-meta">${{date}} &nbsp;·&nbsp; ${{s.project_name}} &nbsp;·&nbsp; ${{tok}} tokens &nbsp;·&nbsp; ${{s.tool_uses}} tools</div>
        </div>`;
      }}).join('')
    : '<div class="picker-empty">No sessions</div>';
  document.getElementById('pickerOverlay').classList.add('open');
}}

function closePicker(e) {{
  if(e && e.target !== document.getElementById('pickerOverlay')) return;
  document.getElementById('pickerOverlay').classList.remove('open');
}}

function openSession(sid, hash) {{
  window.open(DATA.convDir+'/'+sid+'.html'+(hash?'#'+hash:''),'_blank');
}}

// ── click → sessions routing ───────────────────────────────────────────────
function sessionsForClick(chartId, label, datasetIdx, elemIdx) {{
  const ss = filteredSessions;
  switch(chartId) {{
    case 'timelineChart':
      return ss.filter(s=>s.start_time&&s.start_time.slice(0,10)===label);
    case 'projectChart':
      return ss.filter(s=>(DATA.projects[s.project_key]||'')===label);
    case 'toolsChart':
      return ss.filter(s=>(s.tool_counts[label]||0)>0)
               .sort((a,b)=>(b.tool_counts[label]||0)-(a.tool_counts[label]||0));
    case 'tokenMixChart': {{
      const keys=['input_tokens','output_tokens','cache_read_tokens','cache_creation_tokens'];
      const k=keys[elemIdx];
      return [...ss].sort((a,b)=>b[k]-a[k]).slice(0,20);
    }}
    case 'modelChart': {{
      const mss=ss.filter(s=>(s.models&&s.models.length?s.models[0]:'unknown')===label);
      return datasetIdx===1?mss.filter(s=>isAgentic(s)):mss;
    }}
    case 'scatterMsgTokens':
    case 'scatterMsgTools':
      return [];  // scatter handled directly via point sid
    case 'hourChart':
      return ss.filter(s=>s.hour===elemIdx);
    case 'dowChart':
      return ss.filter(s=>s.dow===elemIdx);
    case 'cacheEffChart':
      return ss.filter(s=>s.start_time&&s.start_time.slice(0,10)===label);
    case 'tokenHistChart': {{
      const buckets=[[0,5000],[5000,20000],[20000,50000],[50000,100000],[100000,200000],[200000,500000],[500000,Infinity]];
      const [lo,hi]=buckets[elemIdx]||[0,Infinity];
      return ss.filter(s=>s.total_tokens>=lo&&s.total_tokens<hi);
    }}
    default: return [];
  }}
}}

function makeClickHandler(chartId) {{
  return function(evt, elements) {{
    if(!elements.length) return;
    const el=elements[0];
    const chart=Chart.getChart(chartId);
    const label=chart.data.labels?.[el.index]||'';
    // scatter: open directly via sid on the data point
    if(chartId==='scatterMsgTokens'||chartId==='scatterMsgTools') {{
      const pt=chart.data.datasets[el.datasetIndex].data[el.index];
      if(pt?.sid) openSession(pt.sid, chartId==='scatterMsgTokens' ? 'first-msg' : null);
      return;
    }}
    const matched=sessionsForClick(chartId,label,el.datasetIndex,el.index);
    openSessionPicker(matched, label||'Sessions');
  }};
}}

// ── inject expand buttons ──────────────────────────────────────────────────
function initChartActions() {{
  document.querySelectorAll('.chart-card').forEach(card=>{{
    const h3=card.querySelector('h3');
    const canvas=card.querySelector('canvas');
    if(!canvas) return;
    const title=h3?h3.textContent.trim().slice(0,60):'Chart';
    const actions=document.createElement('div');
    actions.className='card-actions';
    actions.innerHTML=`<button class="btn-icon" title="Expand" onclick="expandChart('${{canvas.id}}','${{title.replace(/'/g,"\\'")}}')">⤢</button>`;
    card.appendChild(actions);
  }});
}}

// ── wire onClick into all charts ───────────────────────────────────────────
function attachClickHandlers() {{
  const ids=['timelineChart','projectChart','toolsChart','tokenMixChart','modelChart',
             'scatterMsgTokens','scatterMsgTools','hourChart','dowChart','cacheEffChart','tokenHistChart'];
  ids.forEach(id=>{{
    const ch=Chart.getChart(id);
    if(!ch) return;
    ch.options.onClick=makeClickHandler(id);
    ch.options.plugins=ch.options.plugins||{{}};
    ch.options.plugins.tooltip=ch.options.plugins.tooltip||{{}};
    // make cursor pointer on hover
    ch.canvas.style.cursor='pointer';
    ch.update('none');
  }});
}}

// ── scatter data with sid ──────────────────────────────────────────────────
// override scatterData to include session_id for direct navigation
const _scatterData = scatterData;
function scatterData(ss, xKey, yKey) {{
  return ss
    .filter(s=>s[xKey]>0&&s[yKey]>0)
    .map(s=>({{x:s[xKey],y:s[yKey],label:s.title||s.session_id.slice(0,8),sid:s.session_id}}));
}}

// ── deep dive text search ──────────────────────────────────────────────────
function ddSearchSessions() {{
  const q = document.getElementById('ddSearch').value.toLowerCase().trim();
  const target = document.getElementById('ddSearchTarget').value;
  const resultsEl = document.getElementById('ddResults');
  const countEl = document.getElementById('ddResultCount');

  if(!q) {{
    resultsEl.style.display='none';
    countEl.textContent='';
    return;
  }}

  const matches = filteredSessions.filter(s=>{{
    const inUser = target!=='asst' && (s.first_user_msg_text||'').toLowerCase().includes(q);
    const inAsst = target!=='user' && (s.first_asst_msg_text||'').toLowerCase().includes(q);
    return inUser||inAsst;
  }});

  countEl.innerHTML = matches.length
    ? `<strong>${{matches.length}}</strong> session${{matches.length!==1?'s':''}} matching "<em>${{q}}</em>"`
    : `No sessions matching "<em>${{q}}</em>"`;

  resultsEl.style.display = matches.length ? 'block' : 'none';
  resultsEl.innerHTML = matches.map(s=>{{
    const date = s.start_time?s.start_time.slice(0,10):'?';
    const tok = s.total_tokens.toLocaleString();
    const snippet = (target==='asst'
      ? s.first_asst_msg_text
      : s.first_user_msg_text)||'';
    const idx = snippet.toLowerCase().indexOf(q);
    const ctx = idx>=0
      ? '…'+snippet.slice(Math.max(0,idx-40), idx+q.length+80).replace(
          new RegExp(q.replace(/[.*+?^${{}}()|[\\]\\\\]/g,'\\\\$&'),'gi'),
          m=>`<mark style="background:#d9770622;color:var(--accent)">${{m}}</mark>`)+'…'
      : '';
    return `<div class="picker-row" onclick="openSession('${{s.session_id}}')">
      <div class="picker-row-title">${{s.title||s.session_id.slice(0,8)}}</div>
      <div class="picker-row-meta">${{date}} &nbsp;·&nbsp; ${{s.project_name}} &nbsp;·&nbsp; ${{tok}} tokens</div>
      ${{ctx?`<div style="font-size:11px;color:var(--muted);margin-top:4px;line-height:1.5">${{ctx}}</div>`:''}}
    </div>`;
  }}).join('');
}}

populateModelDropdown();
applyGlobalFilter();
initChartActions();
attachClickHandlers();
</script>
</body>
</html>"""


# ── main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Claude Code conversation metrics report")
    parser.add_argument("project", nargs="?", help="Pre-filter by project path (default: all)")
    parser.add_argument("--output", default="claude_report.html", help="Output HTML file")
    args = parser.parse_args()

    output_path = os.path.abspath(args.output)
    output_dir = os.path.dirname(output_path)
    output_stem = os.path.splitext(os.path.basename(output_path))[0]
    conv_dir = os.path.join(output_dir, f"{output_stem}_conversations")
    conv_dir_name = f"{output_stem}_conversations"

    print("Scanning ~/.claude/projects/...", file=sys.stderr)
    sessions = collect_all_metrics(args.project)

    if not sessions:
        print("No conversations found.", file=sys.stderr)
        sys.exit(1)

    projects = len({s["project_key"] for s in sessions})
    print(f"Found {len(sessions)} sessions across {projects} projects", file=sys.stderr)

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
