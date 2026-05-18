#!/usr/bin/env python3
"""
Generates an interactive HTML metrics report for Cursor IDE conversations.
Reads chat/composer data from Cursor's SQLite state databases.
Also writes per-session chat log HTMLs to <output_stem>_conversations/.

Usage:
    python3 cursor_metrics.py [project_path] [--output cursor_report.html]
"""

import json
import os
import sys
import sqlite3
import argparse
import html as html_mod
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from urllib.parse import unquote, urlparse

JST = timezone(timedelta(hours=9))

CURSOR_ROOT = os.path.expanduser("~/Library/Application Support/Cursor/User")
GLOBAL_DB = os.path.join(CURSOR_ROOT, "globalStorage", "state.vscdb")
WORKSPACE_ROOT = os.path.join(CURSOR_ROOT, "workspaceStorage")

# Cursor's local DB does NOT persist per-turn input/output tokens
# (tokenCount on every bubble is {0,0}). We estimate tokens from message
# text length (~4 chars/token English heuristic) so cost charts have a value
# of the right order of magnitude. Marked as "estimated" in the UI.
#
# Rates loaded from pricing.json. Cursor routes "default"/"Auto" through its
# own router so the backing model is unknown — Cursor publishes a flat
# pool price for Auto + Composer 2 ($1.25 in / $6 out / $0.25 cache read).
PRICING_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pricing.json")


def load_pricing(vendors: tuple[str, ...] | None = None) -> list[dict]:
    with open(PRICING_PATH) as f:
        entries = json.load(f)["entries"]
    if vendors:
        vs = set(vendors)
        entries = [e for e in entries if e.get("vendor") in vs]
    return entries


PRICING_DEFAULTS = load_pricing()


# ── token estimation ───────────────────────────────────────────────────────
# Cursor doesn't log per-turn tokens locally, so we estimate from text length.
# ~4 chars/token is the OpenAI/Anthropic rule of thumb for English text;
# code/JSON skews slightly lower (3.5) — we use 4 as a round number.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


def _tool_io_text(tf: dict) -> tuple[str, str]:
    """Returns (args_text, result_text) for a toolFormerData blob."""
    args = tf.get("rawArgs") or ""
    if not args and tf.get("params"):
        try:
            args = json.dumps(tf["params"])
        except Exception:
            args = str(tf["params"])
    result = tf.get("result") or ""
    if isinstance(result, (dict, list)):
        try:
            result = json.dumps(result)
        except Exception:
            result = str(result)
    return args, result


# ── workspace mapping ──────────────────────────────────────────────────────

def _open_db(path: str) -> sqlite3.Connection | None:
    if not os.path.isfile(path):
        return None
    # open read-only via URI to coexist with running Cursor
    uri = f"file:{path}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def _workspace_folder(ws_dir: str) -> str | None:
    """Returns the folder URI for a workspaceStorage dir, or None."""
    meta = os.path.join(ws_dir, "workspace.json")
    if os.path.isfile(meta):
        try:
            with open(meta) as f:
                d = json.load(f)
            return d.get("folder") or (d.get("workspace") or {}).get("configPath")
        except Exception:
            pass
    return None


def _uri_to_path(uri: str | None) -> str:
    if not uri:
        return ""
    if uri.startswith("file://"):
        return unquote(urlparse(uri).path)
    return uri


def build_workspace_index() -> dict[str, str]:
    """Returns {composer_id: workspace_path}."""
    index: dict[str, str] = {}
    if not os.path.isdir(WORKSPACE_ROOT):
        return index
    for entry in os.listdir(WORKSPACE_ROOT):
        ws_dir = os.path.join(WORKSPACE_ROOT, entry)
        if not os.path.isdir(ws_dir):
            continue
        db_path = os.path.join(ws_dir, "state.vscdb")
        folder = _uri_to_path(_workspace_folder(ws_dir))
        conn = _open_db(db_path)
        if conn is None:
            continue
        try:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key = 'composer.composerData'"
            ).fetchone()
        except sqlite3.Error:
            row = None
        conn.close()
        if not row:
            continue
        try:
            data = json.loads(row[0])
        except Exception:
            continue
        for cid in (
            (data.get("allComposers") or [])
            + (data.get("selectedComposerIds") or [])
            + (data.get("lastFocusedComposerIds") or [])
        ):
            # allComposers entries are objects with composerId, others are bare ids
            if isinstance(cid, dict):
                cid = cid.get("composerId")
            if cid:
                index.setdefault(cid, folder or f"<workspace:{entry}>")
    return index


# ── composer / bubble parsing ──────────────────────────────────────────────

def _epoch_to_iso(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(JST).isoformat()


def _bubble_text(bubble: dict) -> str:
    t = bubble.get("text") or ""
    if t:
        return t
    rt = bubble.get("richText")
    if isinstance(rt, str):
        try:
            doc = json.loads(rt)
        except Exception:
            return rt
        parts = []
        def walk(n):
            if isinstance(n, dict):
                if n.get("type") == "text" and n.get("text"):
                    parts.append(n["text"])
                for c in n.get("content", []) or []:
                    walk(c)
            elif isinstance(n, list):
                for c in n:
                    walk(c)
        walk(doc)
        return " ".join(parts)
    return ""


def _bubble_kind(bubble: dict) -> str:
    """Returns: 'user' | 'assistant' | 'thinking' | 'tool'"""
    if bubble.get("type") == 1:
        return "user"
    cap = bubble.get("capabilityType")
    if cap == 30 or bubble.get("thinking"):
        return "thinking"
    if cap == 15 or bubble.get("toolFormerData"):
        return "tool"
    return "assistant"


def collect_composers(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
    ).fetchall()
    composers = []
    for key, val in rows:
        cid = key.split(":", 1)[1]
        if cid in ("empty-state-draft",):
            continue
        try:
            d = json.loads(val)
        except Exception:
            continue
        d["_composerId"] = cid
        composers.append(d)
    return composers


def load_bubbles(conn: sqlite3.Connection, composer_id: str, header_ids: list[str]) -> list[dict]:
    if not header_ids:
        return []
    placeholders = ",".join("?" for _ in header_ids)
    keys = [f"bubbleId:{composer_id}:{bid}" for bid in header_ids]
    rows = conn.execute(
        f"SELECT key, value FROM cursorDiskKV WHERE key IN ({placeholders})",
        keys,
    ).fetchall()
    by_id: dict[str, dict] = {}
    for key, val in rows:
        bid = key.rsplit(":", 1)[1]
        try:
            by_id[bid] = json.loads(val)
        except Exception:
            pass
    # preserve header order
    return [by_id[bid] for bid in header_ids if bid in by_id]


def parse_session(composer: dict, bubbles: list[dict], workspace: str) -> dict:
    cid = composer["_composerId"]
    title = composer.get("name") or cid[:8]

    user_count = sum(1 for b in bubbles if _bubble_kind(b) == "user")
    asst_count = sum(1 for b in bubbles if _bubble_kind(b) == "assistant")
    thinking_count = sum(1 for b in bubbles if _bubble_kind(b) == "thinking")
    tool_bubbles = [b for b in bubbles if _bubble_kind(b) == "tool"]

    tool_counts: dict[str, int] = defaultdict(int)
    for b in tool_bubbles:
        name = (b.get("toolFormerData") or {}).get("name") or "unknown"
        tool_counts[name] += 1

    timestamps = []
    for b in bubbles:
        ts = b.get("createdAt")
        if ts:
            try:
                timestamps.append(
                    datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(JST)
                )
            except Exception:
                pass
    if not timestamps:
        for fld in ("createdAt", "lastUpdatedAt"):
            v = composer.get(fld)
            if isinstance(v, (int, float)):
                timestamps.append(
                    datetime.fromtimestamp(v / 1000, tz=timezone.utc).astimezone(JST)
                )
    start_time = min(timestamps) if timestamps else None
    end_time = max(timestamps) if timestamps else None
    duration_mins = (end_time - start_time).total_seconds() / 60 if start_time and end_time else 0

    # ── token estimation (Cursor stores tokenCount but values are always 0) ─
    composer_model = ((composer.get("modelConfig") or {}).get("modelName")) or "default"
    model_tokens: dict[str, dict[str, int]] = defaultdict(
        lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_5m": 0, "cache_1h": 0}
    )
    input_tokens = 0
    output_tokens = 0
    thinking_total_ms = 0
    turn_total_ms = 0
    request_ids: list[str] = []
    models_seen: set[str] = set()

    for b in bubbles:
        kind = _bubble_kind(b)
        bubble_model = ((b.get("modelInfo") or {}).get("modelName")) or composer_model
        models_seen.add(bubble_model)

        rid = b.get("requestId")
        if rid:
            request_ids.append(rid)

        if isinstance(b.get("thinkingDurationMs"), (int, float)):
            thinking_total_ms += int(b["thinkingDurationMs"])
        if isinstance(b.get("turnDurationMs"), (int, float)):
            turn_total_ms += int(b["turnDurationMs"])

        # Per-bubble persisted token count (almost always zero in local DB; trust if present)
        tc = b.get("tokenCount") or {}
        tc_in = tc.get("inputTokens", 0) or 0
        tc_out = tc.get("outputTokens", 0) or 0

        # Estimated tokens from text content
        if kind == "user":
            est_in = estimate_tokens(_bubble_text(b))
            est_out = 0
        elif kind == "tool":
            tf = b.get("toolFormerData") or {}
            args_text, result_text = _tool_io_text(tf)
            est_in = estimate_tokens(result_text)
            est_out = estimate_tokens(args_text)
        elif kind == "thinking":
            thinking = b.get("thinking") or {}
            txt = thinking.get("text", "") if isinstance(thinking, dict) else ""
            est_in = 0
            est_out = estimate_tokens(txt)
        else:  # assistant text
            est_in = 0
            est_out = estimate_tokens(_bubble_text(b))

        # prefer DB values when nonzero, else fall back to estimate
        u_in = tc_in if tc_in else est_in
        u_out = tc_out if tc_out else est_out
        input_tokens += u_in
        output_tokens += u_out
        mt = model_tokens[bubble_model]
        mt["input"] += u_in
        mt["output"] += u_out

    # context size at session end (current-state only)
    ptb = composer.get("promptTokenBreakdown") or {}
    ctx_total_tokens = ptb.get("totalUsedTokens", 0) or 0
    ctx_breakdown = {
        c.get("id") or c.get("label", ""): c.get("estimatedTokens", 0)
        for c in (ptb.get("categories") or [])
        if isinstance(c, dict)
    }

    models = sorted(m for m in models_seen if m)

    # first user message
    first_user = next((b for b in bubbles if _bubble_kind(b) == "user"), None)
    first_user_text = _bubble_text(first_user) if first_user else ""
    first_user_text = first_user_text[:500]
    first_user_len = len(first_user_text)

    first_asst = next((b for b in bubbles if _bubble_kind(b) == "assistant"), None)
    first_asst_text = (_bubble_text(first_asst) if first_asst else "")[:500]

    user_lens = [len(_bubble_text(b)) for b in bubbles if _bubble_kind(b) == "user"]
    avg_user_len = round(sum(user_lens) / len(user_lens)) if user_lens else 0

    project_name = os.path.basename(workspace.rstrip("/")) if workspace else ""

    return {
        "session_id": cid,
        "title": title,
        "cwd": workspace or "",
        "project_name": project_name,
        "project_key": (workspace or "<empty>").replace("/", "-").lstrip("-"),
        "source": "cursor",
        "start_time": start_time.isoformat() if start_time else None,
        "end_time": end_time.isoformat() if end_time else None,
        "duration_mins": round(duration_mins, 1),
        "hour": start_time.hour if start_time else -1,
        "dow": start_time.weekday() if start_time else -1,
        "user_messages": user_count,
        "assistant_messages": asst_count + thinking_count,
        "total_messages": user_count + asst_count + thinking_count,
        "tool_uses": len(tool_bubbles),
        "tool_counts": dict(tool_counts),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_5m_tokens": 0,
        "cache_1h_tokens": 0,
        "total_tokens": input_tokens + output_tokens,
        "model_tokens": {k: dict(v) for k, v in model_tokens.items()},
        "models": models,
        "tokens_estimated": True,
        "ctx_tokens": ctx_total_tokens,
        "ctx_breakdown": ctx_breakdown,
        "thinking_duration_ms": thinking_total_ms,
        "turn_duration_ms": turn_total_ms,
        "request_ids": request_ids,
        "avg_user_msg_len": avg_user_len,
        "first_user_msg_len": first_user_len,
        "first_user_msg_text": first_user_text,
        "first_asst_msg_text": first_asst_text,
        "status": composer.get("status", ""),
        "lines_added": composer.get("totalLinesAdded", 0) or 0,
        "lines_removed": composer.get("totalLinesRemoved", 0) or 0,
        "files_changed": composer.get("filesChangedCount", 0) or 0,
    }


def collect_all_sessions(project_path: str | None) -> tuple[list[dict], dict[str, list[dict]]]:
    conn = _open_db(GLOBAL_DB)
    if conn is None:
        print(f"Cursor global DB not found at {GLOBAL_DB}", file=sys.stderr)
        return [], {}

    ws_index = build_workspace_index()
    composers = collect_composers(conn)

    project_filter = os.path.abspath(project_path) if project_path else None

    sessions = []
    bubbles_by_session: dict[str, list[dict]] = {}
    for c in composers:
        cid = c["_composerId"]
        workspace = ws_index.get(cid, "")
        if project_filter and workspace != project_filter:
            # also accept a workspace prefix match (e.g. nested folders)
            if not (workspace and workspace.startswith(project_filter)):
                continue

        header_ids = [
            h.get("bubbleId") for h in (c.get("fullConversationHeadersOnly") or [])
            if isinstance(h, dict) and h.get("bubbleId")
        ]
        try:
            bubbles = load_bubbles(conn, cid, header_ids)
        except Exception as e:
            print(f"Warning: bubbles for {cid}: {e}", file=sys.stderr)
            bubbles = []
        # skip truly empty composers
        if not bubbles and not c.get("name"):
            continue
        s = parse_session(c, bubbles, workspace)
        sessions.append(s)
        bubbles_by_session[cid] = bubbles

    conn.close()
    sessions.sort(key=lambda s: s["start_time"] or "")
    return sessions, bubbles_by_session


# ── conversation HTML rendering ────────────────────────────────────────────

def _render_text(s: str) -> str:
    return html_mod.escape(s).replace("\n", "<br>")


def _render_bubble(bubble: dict) -> str:
    kind = _bubble_kind(bubble)
    if kind == "user":
        return f'<div class="text-part">{_render_text(_bubble_text(bubble))}</div>'
    if kind == "thinking":
        thinking = (bubble.get("thinking") or {}).get("text", "") if isinstance(bubble.get("thinking"), dict) else ""
        return f'''<details class="thinking-block">
  <summary>Thinking…</summary>
  <div class="thinking-body">{_render_text(thinking)}</div>
</details>'''
    if kind == "tool":
        tf = bubble.get("toolFormerData") or {}
        name = html_mod.escape(tf.get("name", "tool"))
        args_raw = tf.get("rawArgs") or "{}"
        try:
            args_pretty = json.dumps(json.loads(args_raw), indent=2)
        except Exception:
            args_pretty = args_raw
        result = tf.get("result") or ""
        try:
            result_pretty = json.dumps(json.loads(result), indent=2)
        except Exception:
            result_pretty = result
        return f'''<details class="tool-block">
  <summary><span class="tool-name">{name}</span> &nbsp;<span style="font-size:10px;color:var(--muted)">{html_mod.escape(tf.get('status',''))}</span></summary>
  <pre class="tool-input">{html_mod.escape(args_pretty)}</pre>
  <details class="tool-result-block">
    <summary>Result</summary>
    <div class="tool-result-body">{_render_text(result_pretty[:20000])}</div>
  </details>
</details>'''
    # assistant text
    return f'<div class="text-part">{_render_text(_bubble_text(bubble))}</div>'


def render_conversation_html(session: dict, bubbles: list[dict]) -> str:
    date = session["start_time"][:10] if session["start_time"] else ""
    title = html_mod.escape(session["title"])
    project = html_mod.escape(session["cwd"] or session["project_name"] or "<empty>")

    bubbles_html_parts = []
    # group consecutive assistant/thinking/tool bubbles into one assistant turn
    i = 0
    msg_idx = 0
    while i < len(bubbles):
        b = bubbles[i]
        kind = _bubble_kind(b)
        ts_raw = b.get("createdAt", "")
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).astimezone(JST).strftime("%H:%M")
        except (ValueError, AttributeError):
            ts = ts_raw[11:16] if isinstance(ts_raw, str) and len(ts_raw) >= 16 else ""

        if kind == "user":
            body = _render_bubble(b)
            plain = _bubble_text(b).replace('"', '&quot;').replace('\n', ' ')[:1000]
            bubbles_html_parts.append(
                f'<div class="msg msg-user" id="msg-{msg_idx}" data-role="user" data-has-tools="0" data-text="{plain}">'
                f'<div class="msg-body">{body}</div><div class="msg-meta">{ts}</div></div>'
            )
            msg_idx += 1
            i += 1
            continue

        # accumulate assistant group
        group = []
        while i < len(bubbles) and _bubble_kind(bubbles[i]) != "user":
            group.append(bubbles[i])
            i += 1
        body = "".join(_render_bubble(g) for g in group)
        plain = " ".join(_bubble_text(g) for g in group).replace('"', '&quot;').replace('\n', ' ')[:1000]
        has_tools = "1" if any(_bubble_kind(g) in ("tool",) for g in group) else "0"
        model = html_mod.escape(((bubbles[0].get("modelInfo") or {}).get("modelName")) or "")
        meta = f'<div class="msg-meta">{model} &nbsp;·&nbsp; {ts}</div>'
        bubbles_html_parts.append(
            f'<div class="msg msg-assistant" id="msg-{msg_idx}" data-role="assistant" data-has-tools="{has_tools}" data-text="{plain}">'
            f'<div class="msg-body">{body}</div>{meta}</div>'
        )
        msg_idx += 1

    bubbles_html = "\n".join(bubbles_html_parts)

    top_tools = sorted(session["tool_counts"].items(), key=lambda x: -x[1])[:12]
    tools_html = "".join(
        f'<div class="sp-row"><span class="sp-label">{html_mod.escape(t)}</span><span class="sp-val">{c}</span></div>'
        for t, c in top_tools
    ) or '<div class="sp-empty">No tool calls</div>'
    models_str = ", ".join(session.get("models", [])) or "—"
    dur = session["duration_mins"]
    total_tokens = session["total_tokens"]
    in_tokens = session["input_tokens"]
    out_tokens = session["output_tokens"]
    ctx_tokens = session.get("ctx_tokens", 0)
    ctx_breakdown = session.get("ctx_breakdown") or {}
    thinking_ms = session.get("thinking_duration_ms", 0)
    turn_ms = session.get("turn_duration_ms", 0)

    def _fmt_ms(ms: int) -> str:
        if not ms:
            return "—"
        s = ms / 1000
        if s < 60:
            return f"{s:.1f}s"
        m, sec = divmod(int(s), 60)
        return f"{m}m {sec}s"

    ctx_html = "".join(
        f'<div class="sp-row"><span class="sp-label">{html_mod.escape(k)}</span>'
        f'<span class="sp-val">{v:,}</span></div>'
        for k, v in sorted(ctx_breakdown.items(), key=lambda x: -x[1]) if v
    ) or '<div class="sp-empty">No breakdown</div>'

    denom_tok = max(total_tokens, 1)

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
  header{{padding:14px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px;flex-shrink:0}}
  .back{{color:var(--muted);text-decoration:none;font-size:13px;white-space:nowrap}}
  .back:hover{{color:var(--text)}}
  .hinfo{{flex:1;min-width:0}}
  .hinfo h1{{font-size:15px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  .hinfo p{{color:var(--muted);font-size:11px;margin-top:2px}}
  .toolbar{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:10px 20px;border-bottom:1px solid var(--border);flex-shrink:0;background:var(--bg)}}
  .search-wrap{{display:flex;align-items:center;gap:6px;flex:1;min-width:200px}}
  .search-wrap input{{flex:1;background:var(--surface);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:5px 10px;font-size:13px;outline:none}}
  .search-wrap input:focus{{border-color:var(--accent2)}}
  .match-count{{font-size:11px;color:var(--muted);white-space:nowrap}}
  .btn-nav{{background:var(--surface2);border:1px solid var(--border);color:var(--muted);border-radius:5px;padding:3px 8px;cursor:pointer;font-size:12px}}
  .btn-nav:hover{{color:var(--text)}}
  .filter-chips{{display:flex;gap:4px}}
  .chip{{padding:4px 10px;border-radius:20px;font-size:12px;font-weight:500;cursor:pointer;border:1px solid var(--border);background:var(--surface2);color:var(--muted);transition:all .15s}}
  .chip.active{{background:var(--accent2);border-color:var(--accent2);color:#fff}}
  .chip.active.user{{background:var(--user-border);border-color:var(--user-border)}}
  .chip.active.asst{{background:#334155;border-color:#475569;color:var(--text)}}
  .chip.active.tools{{background:#1a3a2a;border-color:#10b981;color:#10b981}}
  .chip.active.thinking{{background:#3a1a3a;border-color:#a855f7;color:#a855f7}}
  .divider{{width:1px;height:20px;background:var(--border);flex-shrink:0}}
  .btn-panel{{background:var(--surface2);border:1px solid var(--border);color:var(--muted);border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px;white-space:nowrap}}
  .btn-panel:hover,.btn-panel.active{{color:var(--text);border-color:var(--accent2)}}
  .main{{display:flex;flex:1;overflow:hidden}}
  .chat-col{{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:14px;min-width:0}}
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
  details summary{{cursor:pointer;padding:6px 10px;font-size:12px;font-weight:500;background:var(--surface2);color:var(--muted);list-style:none;border-radius:6px;user-select:none}}
  details summary:hover{{color:var(--text)}}
  details[open] summary{{border-radius:6px 6px 0 0}}
  .thinking-body,.tool-result-body{{padding:10px 12px;font-size:12px;white-space:pre-wrap;word-break:break-word;background:var(--surface);color:var(--muted);border-radius:0 0 6px 6px}}
  .tool-input{{padding:10px 12px;font-size:11px;white-space:pre-wrap;word-break:break-word;background:#0d1117;color:#7dd3fc;border-radius:0 0 6px 6px;font-family:monospace}}
  .tool-name{{color:var(--accent);font-family:monospace}}
  body.hide-tools .tool-block,body.hide-tools .tool-result-block{{display:none}}
  body.hide-thinking .thinking-block{{display:none}}
  .side-panel{{width:var(--sp-width);flex-shrink:0;border-left:1px solid var(--border);overflow-y:auto;background:var(--surface);transition:width .2s,opacity .2s}}
  .side-panel.hidden{{width:0;opacity:0;overflow:hidden;border:none}}
  .sp-section{{padding:14px 16px;border-bottom:1px solid var(--border)}}
  .sp-section h4{{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-bottom:10px}}
  .sp-row{{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;gap:8px}}
  .sp-label{{font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  .sp-val{{font-size:12px;font-weight:600;color:var(--text);white-space:nowrap}}
  .sp-val.amber{{color:var(--accent)}}.sp-val.indigo{{color:var(--accent2)}}.sp-val.green{{color:var(--green)}}
  .sp-empty{{font-size:12px;color:var(--muted);font-style:italic}}
</style>
</head>
<body>
<header>
  <a class="back" href="../index.html" onclick="window.close();return false;">← back</a>
  <div class="hinfo">
    <h1>{title}</h1>
    <p>{project} &nbsp;·&nbsp; {date} &nbsp;·&nbsp; {session['user_messages']}u / {session['assistant_messages']}a messages &nbsp;·&nbsp; ~{total_tokens:,} tokens (est.)</p>
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
      <div class="sp-row"><span class="sp-label">Status</span><span class="sp-val">{html_mod.escape(session.get('status',''))}</span></div>
    </div>
    <div class="sp-section">
      <h4>Messages</h4>
      <div class="sp-row"><span class="sp-label">Total</span><span class="sp-val">{session['total_messages']}</span></div>
      <div class="sp-row"><span class="sp-label">User</span><span class="sp-val amber">{session['user_messages']}</span></div>
      <div class="sp-row"><span class="sp-label">Assistant</span><span class="sp-val indigo">{session['assistant_messages']}</span></div>
      <div class="sp-row"><span class="sp-label">Tool calls</span><span class="sp-val green">{session['tool_uses']}</span></div>
    </div>
    <div class="sp-section">
      <h4>Changes</h4>
      <div class="sp-row"><span class="sp-label">Files changed</span><span class="sp-val">{session['files_changed']}</span></div>
      <div class="sp-row"><span class="sp-label">Lines added</span><span class="sp-val green">+{session['lines_added']}</span></div>
      <div class="sp-row"><span class="sp-label">Lines removed</span><span class="sp-val amber">-{session['lines_removed']}</span></div>
    </div>
    <div class="sp-section">
      <h4>Tokens (estimated)</h4>
      <div class="sp-row"><span class="sp-label">Total</span><span class="sp-val">{total_tokens:,}</span></div>
      <div class="sp-bar-wrap">
        <div class="sp-bar-label"><span>Input</span><span>{in_tokens:,}</span></div>
        <div class="sp-bar"><div class="sp-bar-fill" style="width:{round(in_tokens/denom_tok*100)}%;background:#6366f1"></div></div>
      </div>
      <div class="sp-bar-wrap">
        <div class="sp-bar-label"><span>Output</span><span>{out_tokens:,}</span></div>
        <div class="sp-bar"><div class="sp-bar-fill" style="width:{round(out_tokens/denom_tok*100)}%;background:#10b981"></div></div>
      </div>
      <div style="font-size:10px;color:var(--muted);margin-top:6px">Cursor does not persist per-turn tokens locally. Values estimated at ~4 chars/token from message text. Treat cost as order-of-magnitude only.</div>
    </div>
    <div class="sp-section" id="costSection">
      <h4>Estimated Cost</h4>
      <div class="sp-row"><span class="sp-label">Total</span><span class="sp-val amber" id="costTotal">—</span></div>
      <div id="costBreakdown" style="margin-top:8px"></div>
      <div style="font-size:10px;color:var(--muted);margin-top:8px">
        Rates editable in main report → Regenerate tab. Tokens are estimates, so cost is too.
      </div>
    </div>
    <div class="sp-section">
      <h4>Latency</h4>
      <div class="sp-row"><span class="sp-label">Thinking</span><span class="sp-val">{_fmt_ms(thinking_ms)}</span></div>
      <div class="sp-row"><span class="sp-label">Turn</span><span class="sp-val">{_fmt_ms(turn_ms)}</span></div>
    </div>
    <div class="sp-section">
      <h4>Context @ end</h4>
      <div class="sp-row"><span class="sp-label">Total</span><span class="sp-val">{ctx_tokens:,}</span></div>
      {ctx_html}
    </div>
    <div class="sp-section">
      <h4>Top Tools</h4>
      {tools_html}
    </div>
  </div>
</div>

<script>
const chipState = {{user:true, asst:true, tools:true, thinking:true}};
function toggleChip(type) {{
  chipState[type] = !chipState[type];
  document.getElementById('chip'+type.charAt(0).toUpperCase()+type.slice(1)).classList.toggle('active', chipState[type]);
  applyFilters();
}}
function applyFilters() {{
  document.body.classList.toggle('hide-tools', !chipState.tools);
  document.body.classList.toggle('hide-thinking', !chipState.thinking);
  document.querySelectorAll('.msg').forEach(m => {{
    const role = m.dataset.role;
    const visible = (role==='user' && chipState.user) || (role==='assistant' && chipState.asst);
    m.classList.toggle('hidden', !visible);
  }});
  doSearch();
}}
let matches = [], matchIdx = 0;
function doSearch() {{
  document.querySelectorAll('.msg').forEach(m => {{
    m.classList.remove('search-match','search-current');
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
      m.querySelectorAll('.text-part').forEach(el => {{
        if(!el.hasAttribute('data-orig')) el.setAttribute('data-orig', el.innerHTML);
        el.innerHTML = el.innerHTML.replace(re, match => `<mark>${{match}}</mark>`);
      }});
    }}
  }});
  document.getElementById('matchCount').textContent = matches.length ? `1 / ${{matches.length}}` : '0';
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
(function() {{
  const h = location.hash;
  if(!h) return;
  const m = h === '#first-msg' ? document.querySelector('.msg-user') : document.getElementById(h.slice(1));
  if(!m) return;
  m.classList.add('search-current');
  m.scrollIntoView({{behavior:'smooth',block:'center'}});
}})();
function togglePanel() {{
  const panel = document.getElementById('sidePanel');
  const btn = document.getElementById('btnPanel');
  const hidden = panel.classList.toggle('hidden');
  btn.classList.toggle('active', !hidden);
  btn.textContent = hidden ? '⊞ Metrics' : '⊟ Metrics';
}}
const SESSION_META = {session_meta_json};
const PRICING_DEFAULTS = {pricing_json};
const PRICING_LS_KEY = 'cursorReport.pricing';

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
  const el = document.getElementById('costTotal');
  if (!el) return;
  el.textContent = fmtUSD(total);
  const bd = document.getElementById('costBreakdown');
  bd.innerHTML = rows.length
    ? rows.map(r => `<div class="sp-row"><span class="sp-label" title="${{r.model}}">${{r.label}}</span><span class="sp-val">${{fmtUSD(r.cost)}}</span></div>`).join('')
    : '<div class="sp-empty">No usage data</div>';
}}
renderCost();
window.addEventListener('storage', e => {{ if (e.key === PRICING_LS_KEY) renderCost(); }});
</script>
</body>
</html>"""


# ── main report HTML ───────────────────────────────────────────────────────

def build_report_html(sessions: list[dict], conv_dir_name: str) -> str:
    projects: dict[str, str] = {}
    for s in sessions:
        if s["project_key"] not in projects:
            name = s["project_name"] or s["project_key"].split("-")[-1] or "<empty>"
            projects[s["project_key"]] = name

    project_options = "\n".join(
        f'<option value="{k}">{name}</option>'
        for k, name in sorted(projects.items(), key=lambda x: x[1])
    )

    data_json = json.dumps({
        "sessions": sessions,
        "projects": projects,
        "convDir": conv_dir_name,
        "pricingDefaults": PRICING_DEFAULTS,
    })

    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_template.html")
    with open(template_path) as tf:
        template = tf.read()
    generated = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    html = (
        template
        .replace("__GENERATED__", generated)
        .replace("__SESSION_COUNT__", str(len(sessions)))
        .replace("__PROJECT_COUNT__", str(len(projects)))
        .replace("__PROJECT_OPTIONS__", project_options)
        .replace("__DATA_JSON__", data_json)
    )
    # rebrand title + H1, and namespace localStorage so Claude report's
    # regen/pricing/tags filters don't leak in and hide Cursor sessions.
    html = html.replace(
        "<title>Claude Code — Conversation Metrics</title>",
        "<title>Cursor — Conversation Metrics</title>",
    )
    html = html.replace(
        "<h1>Claude Code — Conversation Metrics</h1>",
        "<h1>Cursor — Conversation Metrics</h1>",
    )
    html = html.replace("'claudeReport.", "'cursorReport.")
    return html


# ── main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Cursor IDE conversation metrics report")
    parser.add_argument("project", nargs="?", help="Pre-filter by workspace path (default: all)")
    parser.add_argument("--output", default="cursor_report.html", help="Output HTML file")
    parser.add_argument("--no-fetch", action="store_true",
                        help="Skip writing per-session conversation HTMLs; reuse existing folder")
    args = parser.parse_args()

    output_path = os.path.abspath(args.output)
    output_dir = os.path.dirname(output_path)
    output_stem = os.path.splitext(os.path.basename(output_path))[0]
    conv_dir = os.path.join(output_dir, f"{output_stem}_conversations")
    conv_dir_name = f"{output_stem}_conversations"

    print(f"Scanning Cursor data at {CURSOR_ROOT}", file=sys.stderr)
    sessions, bubbles_by_session = collect_all_sessions(args.project)

    if not sessions:
        print("No conversations found.", file=sys.stderr)
        sys.exit(1)

    projects = len({s["project_key"] for s in sessions})
    print(f"Found {len(sessions)} sessions across {projects} workspaces", file=sys.stderr)

    if args.no_fetch:
        if not os.path.isdir(conv_dir) or not any(f.endswith(".html") for f in os.listdir(conv_dir)):
            print(f"Error: --no-fetch requires existing conversation folder at {conv_dir}/", file=sys.stderr)
            sys.exit(1)
        print(f"Reusing existing conversation logs in {conv_dir}/", file=sys.stderr)
    else:
        os.makedirs(conv_dir, exist_ok=True)
        print(f"Writing conversation logs to {conv_dir}/", file=sys.stderr)
        for i, session in enumerate(sessions, 1):
            try:
                bubbles = bubbles_by_session.get(session["session_id"], [])
                conv_html = render_conversation_html(session, bubbles)
                conv_path = os.path.join(conv_dir, f"{session['session_id']}.html")
                with open(conv_path, "w") as f:
                    f.write(conv_html)
            except Exception as e:
                print(f"  Warning: {session['session_id']}: {e}", file=sys.stderr)
            if i % 50 == 0:
                print(f"  {i}/{len(sessions)} done", file=sys.stderr)

    report_html = build_report_html(sessions, conv_dir_name)
    with open(output_path, "w") as f:
        f.write(report_html)

    print(f"Report:        {output_path}", file=sys.stderr)
    print(f"Conversations: {conv_dir}/", file=sys.stderr)


if __name__ == "__main__":
    main()
