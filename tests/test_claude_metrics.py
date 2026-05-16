"""Unit tests for claude_metrics.py"""

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import claude_metrics as cm  # noqa: E402


# ── get_project_key ────────────────────────────────────────────────────────

class TestGetProjectKey:
    def test_simple_absolute_path(self):
        assert cm.get_project_key("/foo/bar") == "foo-bar"

    def test_strips_leading_dash(self):
        key = cm.get_project_key("/a/b/c")
        assert not key.startswith("-")
        assert key == "a-b-c"

    def test_relative_path_resolved(self):
        key = cm.get_project_key(".")
        assert key == os.path.abspath(".").replace("/", "-").lstrip("-")

    def test_nested_path(self):
        assert cm.get_project_key("/Users/foo/code/proj") == "Users-foo-code-proj"


# ── find_jsonl_files ───────────────────────────────────────────────────────

class TestFindJsonlFiles:
    def test_filter_by_project(self, tmp_path, monkeypatch):
        proj = tmp_path / "myproj"
        proj.mkdir()
        base = tmp_path / ".claude" / "projects"
        key = cm.get_project_key(str(proj))
        (base / key).mkdir(parents=True)
        f1 = base / key / "s1.jsonl"
        f1.write_text("")
        (base / "other").mkdir()
        (base / "other" / "s2.jsonl").write_text("")

        monkeypatch.setattr(os.path, "expanduser", lambda p: str(base) if p == "~/.claude/projects" else p)
        result = cm.find_jsonl_files(str(proj))
        assert len(result) == 1
        assert result[0][0] == key
        assert result[0][1].endswith("s1.jsonl")

    def test_all_projects(self, tmp_path, monkeypatch):
        base = tmp_path / ".claude" / "projects"
        (base / "p1").mkdir(parents=True)
        (base / "p2").mkdir()
        (base / "p1" / "a.jsonl").write_text("")
        (base / "p2" / "b.jsonl").write_text("")

        monkeypatch.setattr(os.path, "expanduser", lambda p: str(base) if p == "~/.claude/projects" else p)
        result = cm.find_jsonl_files(None)
        keys = sorted(k for k, _ in result)
        assert keys == ["p1", "p2"]

    def test_no_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "missing") if p == "~/.claude/projects" else p)
        assert cm.find_jsonl_files(None) == []


# ── load_messages ──────────────────────────────────────────────────────────

class TestLoadMessages:
    def test_loads_jsonl(self, tmp_path):
        f = tmp_path / "x.jsonl"
        f.write_text('{"a":1}\n{"b":2}\n')
        assert cm.load_messages(str(f)) == [{"a": 1}, {"b": 2}]

    def test_skips_blank_lines(self, tmp_path):
        f = tmp_path / "x.jsonl"
        f.write_text('{"a":1}\n\n   \n{"b":2}\n')
        assert cm.load_messages(str(f)) == [{"a": 1}, {"b": 2}]

    def test_empty_file(self, tmp_path):
        f = tmp_path / "x.jsonl"
        f.write_text("")
        assert cm.load_messages(str(f)) == []

    def test_invalid_json_raises(self, tmp_path):
        f = tmp_path / "x.jsonl"
        f.write_text("not json\n")
        with pytest.raises(json.JSONDecodeError):
            cm.load_messages(str(f))


# ── parse_session ──────────────────────────────────────────────────────────

def _make_jsonl(tmp_path, messages, name="session-abc12345.jsonl"):
    f = tmp_path / name
    f.write_text("\n".join(json.dumps(m) for m in messages))
    return str(f)


class TestParseSession:
    def test_empty_session(self, tmp_path):
        path = _make_jsonl(tmp_path, [])
        s = cm.parse_session(path)
        assert s["user_messages"] == 0
        assert s["assistant_messages"] == 0
        assert s["total_messages"] == 0
        assert s["total_tokens"] == 0
        assert s["start_time"] is None
        assert s["end_time"] is None
        assert s["duration_mins"] == 0
        assert s["hour"] == -1
        assert s["dow"] == -1
        assert s["tool_counts"] == {}
        assert s["models"] == []

    def test_session_id_from_filename(self, tmp_path):
        path = _make_jsonl(tmp_path, [], name="my-session-id.jsonl")
        s = cm.parse_session(path)
        assert s["session_id"] == "my-session-id"

    def test_counts_user_assistant_messages(self, tmp_path):
        msgs = [
            {"type": "user", "message": {"content": "hi"}, "timestamp": "2026-01-01T00:00:00Z"},
            {"type": "user", "message": {"content": "again"}, "timestamp": "2026-01-01T00:01:00Z"},
            {"type": "assistant", "message": {"content": [], "usage": {}}, "timestamp": "2026-01-01T00:00:30Z"},
        ]
        s = cm.parse_session(_make_jsonl(tmp_path, msgs))
        assert s["user_messages"] == 2
        assert s["assistant_messages"] == 1
        assert s["total_messages"] == 3

    def test_skips_meta_user_messages(self, tmp_path):
        msgs = [
            {"type": "user", "isMeta": True, "message": {"content": "meta"}},
            {"type": "user", "message": {"content": "real"}},
        ]
        s = cm.parse_session(_make_jsonl(tmp_path, msgs))
        assert s["user_messages"] == 1

    def test_token_aggregation(self, tmp_path):
        msgs = [
            {"type": "assistant", "message": {"content": [], "usage": {
                "input_tokens": 10, "output_tokens": 20,
                "cache_read_input_tokens": 5, "cache_creation_input_tokens": 3,
            }}},
            {"type": "assistant", "message": {"content": [], "usage": {
                "input_tokens": 1, "output_tokens": 2,
                "cache_read_input_tokens": 4, "cache_creation_input_tokens": 6,
            }}},
        ]
        s = cm.parse_session(_make_jsonl(tmp_path, msgs))
        assert s["input_tokens"] == 11
        assert s["output_tokens"] == 22
        assert s["cache_read_tokens"] == 9
        assert s["cache_creation_tokens"] == 9
        assert s["total_tokens"] == 33

    def test_tool_uses_counted(self, tmp_path):
        msgs = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash"},
                {"type": "tool_use", "name": "Read"},
                {"type": "tool_use", "name": "Bash"},
                {"type": "text", "text": "hi"},
            ], "usage": {}}},
        ]
        s = cm.parse_session(_make_jsonl(tmp_path, msgs))
        assert s["tool_uses"] == 3
        assert s["tool_counts"] == {"Bash": 2, "Read": 1}

    def test_tool_use_missing_name(self, tmp_path):
        msgs = [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use"},
            ], "usage": {}}},
        ]
        s = cm.parse_session(_make_jsonl(tmp_path, msgs))
        assert s["tool_counts"] == {"unknown": 1}

    def test_timestamps_and_duration(self, tmp_path):
        msgs = [
            {"type": "user", "message": {"content": "a"}, "timestamp": "2026-01-01T00:00:00Z"},
            {"type": "user", "message": {"content": "b"}, "timestamp": "2026-01-01T00:30:00Z"},
        ]
        s = cm.parse_session(_make_jsonl(tmp_path, msgs))
        assert s["duration_mins"] == 30.0
        # 00:00 UTC == 09:00 JST
        assert s["hour"] == 9

    def test_models_dedup_and_filter_synthetic(self, tmp_path):
        msgs = [
            {"type": "assistant", "message": {"content": [], "model": "claude-opus", "usage": {}}},
            {"type": "assistant", "message": {"content": [], "model": "claude-opus", "usage": {}}},
            {"type": "assistant", "message": {"content": [], "model": "claude-sonnet", "usage": {}}},
            {"type": "assistant", "message": {"content": [], "model": "<synthetic>", "usage": {}}},
        ]
        s = cm.parse_session(_make_jsonl(tmp_path, msgs))
        assert sorted(s["models"]) == ["claude-opus", "claude-sonnet"]

    def test_title_from_ai_title(self, tmp_path):
        msgs = [{"type": "ai-title", "aiTitle": "My Session"}]
        s = cm.parse_session(_make_jsonl(tmp_path, msgs))
        assert s["title"] == "My Session"

    def test_title_fallback_to_session_id(self, tmp_path):
        path = _make_jsonl(tmp_path, [], name="abcdef1234567890.jsonl")
        s = cm.parse_session(path)
        assert s["title"] == "abcdef12"

    def test_cwd_and_project_name(self, tmp_path):
        msgs = [{"type": "user", "cwd": "/home/u/code/proj", "message": {"content": "x"}}]
        s = cm.parse_session(_make_jsonl(tmp_path, msgs))
        assert s["cwd"] == "/home/u/code/proj"
        assert s["project_name"] == "proj"

    def test_cwd_missing(self, tmp_path):
        msgs = [{"type": "user", "message": {"content": "x"}}]
        s = cm.parse_session(_make_jsonl(tmp_path, msgs))
        assert s["cwd"] == ""
        assert s["project_name"] == ""

    def test_avg_user_msg_len_string_content(self, tmp_path):
        msgs = [
            {"type": "user", "message": {"content": "abcd"}},   # 4
            {"type": "user", "message": {"content": "ab"}},     # 2
        ]
        s = cm.parse_session(_make_jsonl(tmp_path, msgs))
        assert s["avg_user_msg_len"] == 3

    def test_avg_user_msg_len_list_content(self, tmp_path):
        msgs = [
            {"type": "user", "message": {"content": [
                {"type": "text", "text": "hello"},
                {"type": "text", "text": "!"},
            ]}},
        ]
        s = cm.parse_session(_make_jsonl(tmp_path, msgs))
        assert s["avg_user_msg_len"] == 6

    def test_first_user_msg_text_truncated(self, tmp_path):
        long_text = "x" * 600
        msgs = [{"type": "user", "message": {"content": long_text}}]
        s = cm.parse_session(_make_jsonl(tmp_path, msgs))
        assert s["first_user_msg_len"] == 600
        assert len(s["first_user_msg_text"]) == 500

    def test_first_user_msg_list_content(self, tmp_path):
        msgs = [{"type": "user", "message": {"content": [
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"},
        ]}}]
        s = cm.parse_session(_make_jsonl(tmp_path, msgs))
        assert s["first_user_msg_text"].startswith("hello  world")
        assert s["first_user_msg_len"] == len("hello  world")

    def test_first_asst_msg_text(self, tmp_path):
        msgs = [{"type": "assistant", "message": {"content": [
            {"type": "text", "text": "answer"},
            {"type": "tool_use", "name": "Bash"},
        ], "usage": {}}}]
        s = cm.parse_session(_make_jsonl(tmp_path, msgs))
        assert "answer" in s["first_asst_msg_text"]


# ── collect_all_metrics ────────────────────────────────────────────────────

class TestCollectAllMetrics:
    def test_sorts_by_start_time(self, tmp_path, monkeypatch):
        base = tmp_path / ".claude" / "projects" / "proj"
        base.mkdir(parents=True)
        later = base / "later.jsonl"
        earlier = base / "earlier.jsonl"
        later.write_text(json.dumps({"type": "user", "message": {"content": "x"}, "timestamp": "2026-02-01T00:00:00Z"}))
        earlier.write_text(json.dumps({"type": "user", "message": {"content": "x"}, "timestamp": "2026-01-01T00:00:00Z"}))

        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / ".claude" / "projects") if p == "~/.claude/projects" else p)
        sessions = cm.collect_all_metrics(None)
        ids = [s["session_id"] for s in sessions]
        assert ids == ["earlier", "later"]
        assert all(s["project_key"] == "proj" for s in sessions)

    def test_continues_on_parse_error(self, tmp_path, monkeypatch, capsys):
        base = tmp_path / ".claude" / "projects" / "p"
        base.mkdir(parents=True)
        (base / "good.jsonl").write_text(json.dumps({"type": "user", "message": {"content": "x"}, "timestamp": "2026-01-01T00:00:00Z"}))
        (base / "bad.jsonl").write_text("not json\n")

        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / ".claude" / "projects") if p == "~/.claude/projects" else p)
        sessions = cm.collect_all_metrics(None)
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "good"
        err = capsys.readouterr().err
        assert "Warning" in err and "bad.jsonl" in err


# ── _content_to_parts ──────────────────────────────────────────────────────

class TestContentToParts:
    def test_string(self):
        assert cm._content_to_parts("hi") == [{"type": "text", "text": "hi"}]

    def test_list_passthrough(self):
        parts = [{"type": "text", "text": "x"}]
        assert cm._content_to_parts(parts) == parts

    def test_other(self):
        assert cm._content_to_parts(None) == []
        assert cm._content_to_parts(42) == []


# ── extract_turns ──────────────────────────────────────────────────────────

class TestExtractTurns:
    def test_basic_user_assistant(self):
        msgs = [
            {"type": "user", "timestamp": "t1", "message": {"content": "hi"}},
            {"type": "assistant", "timestamp": "t2", "message": {
                "content": [{"type": "text", "text": "hello"}],
                "model": "claude-opus", "usage": {"input_tokens": 1}
            }},
        ]
        turns = cm.extract_turns(msgs)
        assert len(turns) == 2
        assert turns[0]["role"] == "user"
        assert turns[0]["parts"] == [{"type": "text", "text": "hi"}]
        assert turns[1]["role"] == "assistant"
        assert turns[1]["model"] == "claude-opus"
        assert turns[1]["usage"] == {"input_tokens": 1}

    def test_meta_user_skipped(self):
        msgs = [{"type": "user", "isMeta": True, "message": {"content": "meta"}}]
        assert cm.extract_turns(msgs) == []

    def test_tool_results_attach_to_prev_assistant(self):
        msgs = [
            {"type": "assistant", "timestamp": "t1", "message": {
                "content": [{"type": "tool_use", "name": "Bash"}], "usage": {}}},
            {"type": "user", "timestamp": "t2", "message": {
                "content": [{"type": "tool_result", "content": "out"}]}},
        ]
        turns = cm.extract_turns(msgs)
        assert len(turns) == 1
        assert turns[0]["role"] == "assistant"
        assert len(turns[0]["tool_results"]) == 1
        assert turns[0]["tool_results"][0]["type"] == "tool_result"

    def test_tool_results_orphan(self):
        msgs = [{"type": "user", "timestamp": "t1", "message": {
            "content": [{"type": "tool_result", "content": "out"}]}}]
        turns = cm.extract_turns(msgs)
        assert len(turns) == 1
        assert turns[0]["role"] == "tool_results"

    def test_unknown_message_types_ignored(self):
        msgs = [{"type": "system", "message": {"content": "x"}}, {"type": "summary"}]
        assert cm.extract_turns(msgs) == []


# ── _render_part ───────────────────────────────────────────────────────────

class TestRenderPart:
    def test_text_escapes_html(self):
        out = cm._render_part({"type": "text", "text": "<script>x</script>"})
        assert "&lt;script&gt;" in out
        assert "<script>" not in out

    def test_text_preserves_newlines_as_br(self):
        out = cm._render_part({"type": "text", "text": "a\nb"})
        assert "a<br>b" in out

    def test_thinking_block(self):
        out = cm._render_part({"type": "thinking", "thinking": "hmm"})
        assert "thinking-block" in out
        assert "hmm" in out

    def test_tool_use_json_input(self):
        out = cm._render_part({"type": "tool_use", "name": "Bash", "input": {"cmd": "ls"}})
        assert "Bash" in out
        assert "cmd" in out and "ls" in out

    def test_tool_result_string(self):
        out = cm._render_part({"type": "tool_result", "content": "ok\nthen"})
        assert "ok<br>then" in out

    def test_tool_result_list(self):
        out = cm._render_part({"type": "tool_result", "content": [
            {"text": "part1"}, {"text": "part2"},
        ]})
        assert "part1" in out and "part2" in out

    def test_unknown_part_fallback(self):
        out = cm._render_part({"type": "weird", "foo": "bar"})
        assert "unknown-part" in out


# ── render_conversation_html ───────────────────────────────────────────────

def _full_session(**overrides):
    s = {
        "session_id": "abc", "title": "Test <run>",
        "cwd": "/home/u/p", "project_name": "p",
        "start_time": "2026-01-01T00:00:00+09:00",
        "end_time": "2026-01-01T00:05:00+09:00",
        "duration_mins": 5, "hour": 0, "dow": 0,
        "user_messages": 1, "assistant_messages": 1, "total_messages": 2,
        "tool_uses": 4, "tool_counts": {"Bash": 3, "Read": 1},
        "input_tokens": 100, "output_tokens": 80,
        "cache_read_tokens": 50, "cache_creation_tokens": 50,
        "total_tokens": 180, "models": ["claude-opus"],
        "avg_user_msg_len": 5, "first_user_msg_len": 5,
        "first_user_msg_text": "", "first_asst_msg_text": "",
    }
    s.update(overrides)
    return s


class TestRenderConversationHtml:
    def test_smoke(self):
        session = _full_session()
        turns = [
            {"role": "user", "timestamp": "2026-01-01T00:00:00Z",
             "parts": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "timestamp": "2026-01-01T00:00:30Z",
             "parts": [{"type": "text", "text": "hi"}],
             "model": "claude-opus", "usage": {"input_tokens": 5, "output_tokens": 10, "cache_read_input_tokens": 0},
             "tool_results": []},
        ]
        html = cm.render_conversation_html(session, turns)
        assert "<!DOCTYPE html>" in html
        assert "Test &lt;run&gt;" in html  # title escaped
        assert "hello" in html and "hi" in html
        assert "Bash" in html  # tool count panel

    def test_handles_bad_timestamp(self):
        session = _full_session(
            start_time=None, end_time=None, duration_mins=0,
            user_messages=0, assistant_messages=0, total_messages=0,
            total_tokens=0, input_tokens=0, output_tokens=0,
            cache_read_tokens=0, cache_creation_tokens=0,
            tool_uses=0, tool_counts={}, models=[],
        )
        turns = [{"role": "user", "timestamp": "bogus",
                  "parts": [{"type": "text", "text": "hi"}]}]
        html = cm.render_conversation_html(session, turns)
        assert "hi" in html
