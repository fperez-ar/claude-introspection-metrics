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

def _patch_sources(monkeypatch, tmp_path, code=True, desktop=True):
    """Point SOURCES at tmp_path subdirs. Returns (code_base, desktop_base)."""
    code_base = tmp_path / "code_projects"
    desktop_base = tmp_path / "desktop_sessions"
    src = {}
    if code:
        src["code"] = str(code_base)
    if desktop:
        src["desktop"] = str(desktop_base)
    monkeypatch.setattr(cm, "SOURCES", src)
    # bypass expanduser since we pass absolute tmp paths
    monkeypatch.setattr(os.path, "expanduser", lambda p: p)
    return code_base, desktop_base


class TestFindJsonlFiles:
    def test_filter_by_project(self, tmp_path, monkeypatch):
        code_base, _ = _patch_sources(monkeypatch, tmp_path, desktop=False)
        proj = tmp_path / "myproj"
        proj.mkdir()
        key = cm.get_project_key(str(proj))
        (code_base / key).mkdir(parents=True)
        (code_base / key / "s1.jsonl").write_text("")
        (code_base / "other").mkdir()
        (code_base / "other" / "s2.jsonl").write_text("")

        result = cm.find_jsonl_files(str(proj), sources=("code",))
        assert len(result) == 1
        assert result[0] == ("code", key, str(code_base / key / "s1.jsonl"))

    def test_all_projects(self, tmp_path, monkeypatch):
        code_base, _ = _patch_sources(monkeypatch, tmp_path, desktop=False)
        (code_base / "p1").mkdir(parents=True)
        (code_base / "p2").mkdir()
        (code_base / "p1" / "a.jsonl").write_text("")
        (code_base / "p2" / "b.jsonl").write_text("")

        result = cm.find_jsonl_files(None, sources=("code",))
        keys = sorted(k for _, k, _ in result)
        assert keys == ["p1", "p2"]
        assert all(s == "code" for s, _, _ in result)

    def test_no_files(self, tmp_path, monkeypatch):
        _patch_sources(monkeypatch, tmp_path, desktop=False)
        assert cm.find_jsonl_files(None, sources=("code",)) == []

    # ── multi-source ───────────────────────────────────────────────────────

    def test_desktop_only(self, tmp_path, monkeypatch):
        _, desktop_base = _patch_sources(monkeypatch, tmp_path, code=False)
        sess = desktop_base / "agent1" / "run1" / "local_xyz" / ".claude" / "projects" / "myproj"
        sess.mkdir(parents=True)
        (sess / "session-1.jsonl").write_text("")

        result = cm.find_jsonl_files(None, sources=("desktop",))
        assert len(result) == 1
        src, key, path = result[0]
        assert src == "desktop"
        assert key == "myproj"
        assert path.endswith("session-1.jsonl")

    def test_desktop_skips_audit_jsonl(self, tmp_path, monkeypatch):
        _, desktop_base = _patch_sources(monkeypatch, tmp_path, code=False)
        local = desktop_base / "a" / "b" / "local_x"
        (local).mkdir(parents=True)
        (local / "audit.jsonl").write_text("")  # outside projects/, ignored anyway
        sess = local / ".claude" / "projects" / "p"
        sess.mkdir(parents=True)
        (sess / "real.jsonl").write_text("")
        (sess / "audit.jsonl").write_text("")  # inside, must be skipped

        result = cm.find_jsonl_files(None, sources=("desktop",))
        names = sorted(os.path.basename(p) for _, _, p in result)
        assert names == ["real.jsonl"]

    def test_both_sources_default(self, tmp_path, monkeypatch):
        code_base, desktop_base = _patch_sources(monkeypatch, tmp_path)
        (code_base / "p").mkdir(parents=True)
        (code_base / "p" / "c.jsonl").write_text("")
        sess = desktop_base / "a" / "b" / "local_x" / ".claude" / "projects" / "q"
        sess.mkdir(parents=True)
        (sess / "d.jsonl").write_text("")

        result = cm.find_jsonl_files(None)  # default both
        srcs = sorted(s for s, _, _ in result)
        assert srcs == ["code", "desktop"]

    def test_missing_source_dir_skipped(self, tmp_path, monkeypatch):
        # desktop dir absent — should not raise
        _patch_sources(monkeypatch, tmp_path, code=False)
        # don't create desktop_base
        assert cm.find_jsonl_files(None, sources=("desktop",)) == []

    def test_desktop_project_filter(self, tmp_path, monkeypatch):
        _, desktop_base = _patch_sources(monkeypatch, tmp_path, code=False)
        proj = tmp_path / "wanted"
        proj.mkdir()
        key = cm.get_project_key(str(proj))
        s1 = desktop_base / "a" / "b" / "local_x" / ".claude" / "projects" / key
        s1.mkdir(parents=True)
        (s1 / "match.jsonl").write_text("")
        s2 = desktop_base / "a" / "b" / "local_x" / ".claude" / "projects" / "other"
        s2.mkdir(parents=True)
        (s2 / "skip.jsonl").write_text("")

        result = cm.find_jsonl_files(str(proj), sources=("desktop",))
        names = [os.path.basename(p) for _, _, p in result]
        assert names == ["match.jsonl"]


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
        code_base, _ = _patch_sources(monkeypatch, tmp_path, desktop=False)
        proj = code_base / "proj"
        proj.mkdir(parents=True)
        (proj / "later.jsonl").write_text(json.dumps({"type": "user", "message": {"content": "x"}, "timestamp": "2026-02-01T00:00:00Z"}))
        (proj / "earlier.jsonl").write_text(json.dumps({"type": "user", "message": {"content": "x"}, "timestamp": "2026-01-01T00:00:00Z"}))

        sessions = cm.collect_all_metrics(None, sources=("code",))
        ids = [s["session_id"] for s in sessions]
        assert ids == ["earlier", "later"]
        assert all(s["project_key"] == "proj" for s in sessions)
        assert all(s["source"] == "code" for s in sessions)

    def test_continues_on_parse_error(self, tmp_path, monkeypatch, capsys):
        code_base, _ = _patch_sources(monkeypatch, tmp_path, desktop=False)
        proj = code_base / "p"
        proj.mkdir(parents=True)
        (proj / "good.jsonl").write_text(json.dumps({"type": "user", "message": {"content": "x"}, "timestamp": "2026-01-01T00:00:00Z"}))
        (proj / "bad.jsonl").write_text("not json\n")

        sessions = cm.collect_all_metrics(None, sources=("code",))
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "good"
        err = capsys.readouterr().err
        assert "Warning" in err and "bad.jsonl" in err

    def test_tags_source_per_session(self, tmp_path, monkeypatch):
        code_base, desktop_base = _patch_sources(monkeypatch, tmp_path)
        (code_base / "p").mkdir(parents=True)
        (code_base / "p" / "c.jsonl").write_text(
            json.dumps({"type": "user", "message": {"content": "x"}, "timestamp": "2026-01-01T00:00:00Z"})
        )
        dsess = desktop_base / "a" / "b" / "local_x" / ".claude" / "projects" / "q"
        dsess.mkdir(parents=True)
        (dsess / "d.jsonl").write_text(
            json.dumps({"type": "user", "message": {"content": "y"}, "timestamp": "2026-01-02T00:00:00Z"})
        )

        sessions = cm.collect_all_metrics(None)
        by_id = {s["session_id"]: s for s in sessions}
        assert by_id["c"]["source"] == "code"
        assert by_id["d"]["source"] == "desktop"


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


# ── tags feature (browser-side localStorage; Python side has no schema) ────

import re


def _extract_data_json(html: str) -> dict:
    """Pull the DATA = {...} payload out of the built dashboard HTML."""
    m = re.search(r'const DATA = (\{.*?\});', html)
    assert m, "DATA literal not found in dashboard HTML"
    return json.loads(m.group(1))


class TestParseSessionNoTagSidecarFields:
    def test_parse_session_has_no_tag_fields(self, tmp_path):
        # tags live in browser localStorage; the Python parse path must not
        # emit success/tags/notes/jsonl_dir keys.
        path = _make_jsonl(tmp_path, [])
        s = cm.parse_session(path)
        for k in ("success", "tags", "notes", "jsonl_dir"):
            assert k not in s, f"unexpected key {k!r} in parse_session output"

    def test_no_sidecar_loader_exists(self):
        # Defensive: the sidecar loader was removed when storage moved to
        # browser localStorage. Calling it should fail loudly.
        assert not hasattr(cm, "load_tags_sidecar")


class TestBuildReportHtmlTagsUI:
    def _sessions(self):
        return [
            _full_session(
                session_id="a", jsonl_path="/var/jsonls/a.jsonl",
                project_key="proj1", source="code",
            ),
            _full_session(
                session_id="b", jsonl_path="/var/jsonls/b.jsonl",
                project_key="proj1", source="code",
            ),
        ]

    def test_data_json_strips_jsonl_path(self):
        html = cm.build_report_html(self._sessions(), "convs")
        data = _extract_data_json(html)
        for s in data["sessions"]:
            assert "jsonl_path" not in s
            assert "jsonl_dir" not in s

    def test_template_renders_tags_chart_and_filter(self):
        html = cm.build_report_html(self._sessions(), "convs")
        assert "tagsChart" in html
        assert 'id="fTag"' in html

    def test_template_drops_success_widgets(self):
        # No star rating UI; no success charts/filter must survive.
        html = cm.build_report_html(self._sessions(), "convs")
        assert "successDistChart" not in html
        assert "successTrendChart" not in html
        assert 'id="fSuccessMin"' not in html
        assert "starWidget" not in html

    def test_template_uses_localstorage_key(self):
        html = cm.build_report_html(self._sessions(), "convs")
        assert "claudeReport.tags" in html
        # FS Access bridge was removed.
        assert "showDirectoryPicker" not in html


class TestRenderConversationHtmlTagsWidget:
    def test_viewer_has_tag_input_no_stars(self):
        session = _full_session()
        turns = []
        html = cm.render_conversation_html(session, turns)
        assert 'id="tagInput"' in html
        assert "claudeReport.tags" in html
        # star UI removed
        assert 'id="starWidget"' not in html
        # FS Access bridge removed
        assert "showDirectoryPicker" not in html


# ── main() CLI smoke ───────────────────────────────────────────────────────

class TestMain:
    """End-to-end smoke tests for the main() entrypoint.

    Invokes main() in-process (preserving coverage instrumentation) with
    SOURCES patched to a tmp tree and sys.argv set accordingly.
    """

    def _seed_code_session(self, tmp_path, monkeypatch):
        code_base, _ = _patch_sources(monkeypatch, tmp_path, desktop=False)
        proj = code_base / "smoke"
        proj.mkdir(parents=True)
        msgs = [
            {"type": "user", "message": {"content": "hello"},
             "timestamp": "2026-01-01T00:00:00Z", "cwd": "/work/smoke"},
            {"type": "assistant", "message": {
                "content": [{"type": "text", "text": "hi back"}],
                "model": "claude-opus-4-7",
                "usage": {"input_tokens": 5, "output_tokens": 3},
            }, "timestamp": "2026-01-01T00:00:30Z"},
        ]
        (proj / "smoke-1.jsonl").write_text("\n".join(json.dumps(m) for m in msgs))
        return code_base

    def test_writes_report_and_conversation(self, tmp_path, monkeypatch, capsys):
        self._seed_code_session(tmp_path, monkeypatch)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        output = out_dir / "report.html"
        monkeypatch.setattr(sys, "argv", [
            "claude_metrics.py", "--output", str(output), "--source", "code",
        ])

        cm.main()

        assert output.exists()
        report_html = output.read_text()
        assert "<!DOCTYPE html>" in report_html
        assert "smoke" in report_html  # project name appears

        conv_dir = out_dir / "report_conversations"
        assert conv_dir.is_dir()
        conv_files = list(conv_dir.glob("*.html"))
        assert len(conv_files) == 1
        assert conv_files[0].name == "smoke-1.html"
        assert "<!DOCTYPE html>" in conv_files[0].read_text()

        err = capsys.readouterr().err
        assert "Found 1 sessions" in err
        assert "Report:" in err

    def test_no_sessions_exits_1(self, tmp_path, monkeypatch, capsys):
        _patch_sources(monkeypatch, tmp_path, desktop=False)
        output = tmp_path / "empty.html"
        monkeypatch.setattr(sys, "argv", [
            "claude_metrics.py", "--output", str(output), "--source", "code",
        ])

        with pytest.raises(SystemExit) as ei:
            cm.main()
        assert ei.value.code == 1
        assert "No conversations found" in capsys.readouterr().err
        assert not output.exists()

    def test_no_fetch_missing_conv_dir_exits_1(self, tmp_path, monkeypatch, capsys):
        self._seed_code_session(tmp_path, monkeypatch)
        output = tmp_path / "out" / "report.html"
        output.parent.mkdir()
        monkeypatch.setattr(sys, "argv", [
            "claude_metrics.py", "--output", str(output),
            "--source", "code", "--no-fetch",
        ])

        with pytest.raises(SystemExit) as ei:
            cm.main()
        assert ei.value.code == 1
        err = capsys.readouterr().err
        assert "--no-fetch requires existing conversation folder" in err

    def test_no_fetch_reuses_existing_conv_dir(self, tmp_path, monkeypatch, capsys):
        self._seed_code_session(tmp_path, monkeypatch)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        conv_dir = out_dir / "report_conversations"
        conv_dir.mkdir()
        stub = conv_dir / "smoke-1.html"
        stub.write_text("<!-- pre-existing stub -->")
        output = out_dir / "report.html"
        monkeypatch.setattr(sys, "argv", [
            "claude_metrics.py", "--output", str(output),
            "--source", "code", "--no-fetch",
        ])

        cm.main()

        assert output.exists()
        # --no-fetch must leave the existing conv HTML untouched
        assert stub.read_text() == "<!-- pre-existing stub -->"
        err = capsys.readouterr().err
        assert "Reusing existing conversation logs" in err

    def test_project_filter_argument(self, tmp_path, monkeypatch, capsys):
        code_base, _ = _patch_sources(monkeypatch, tmp_path, desktop=False)
        wanted = tmp_path / "wanted"
        wanted.mkdir()
        key = cm.get_project_key(str(wanted))
        (code_base / key).mkdir(parents=True)
        (code_base / key / "w.jsonl").write_text(
            json.dumps({"type": "user", "message": {"content": "x"},
                        "timestamp": "2026-01-01T00:00:00Z"})
        )
        (code_base / "other").mkdir()
        (code_base / "other" / "o.jsonl").write_text(
            json.dumps({"type": "user", "message": {"content": "y"},
                        "timestamp": "2026-01-01T00:00:00Z"})
        )
        output = tmp_path / "filtered.html"
        monkeypatch.setattr(sys, "argv", [
            "claude_metrics.py", str(wanted),
            "--output", str(output), "--source", "code",
        ])

        cm.main()

        assert "Found 1 sessions" in capsys.readouterr().err
        conv_dir = tmp_path / "filtered_conversations"
        assert {p.name for p in conv_dir.glob("*.html")} == {"w.html"}
