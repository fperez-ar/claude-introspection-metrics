"""Tests for pricing.json + load_pricing in claude_metrics / cursor_metrics."""

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import claude_metrics as cm  # noqa: E402
import cursor_metrics as cu  # noqa: E402


PRICING_FIELDS = ("input", "cache_5m", "cache_1h", "cache_hit", "output")


def _pick_rate(model: str, pricing: list[dict]) -> dict:
    """Mirror of JS pickRate: first non-empty substring match, else last entry."""
    for r in pricing:
        if r["pattern"] and model and r["pattern"] in model:
            return r
    return pricing[-1]


# ── pricing.json structural integrity ──────────────────────────────────────

class TestPricingJsonFile:
    def setup_method(self):
        with open(ROOT / "pricing.json") as f:
            self.data = json.load(f)
        self.entries = self.data["entries"]

    def test_top_level_keys(self):
        assert "entries" in self.data
        assert isinstance(self.entries, list)
        assert self.entries, "pricing.json must contain at least one entry"

    def test_every_entry_has_required_fields(self):
        required = {"id", "pattern", "label", "vendor", *PRICING_FIELDS}
        for e in self.entries:
            missing = required - set(e)
            assert not missing, f"entry {e.get('id')!r} missing fields {missing}"

    def test_numeric_fields_are_non_negative_numbers(self):
        for e in self.entries:
            for f in PRICING_FIELDS:
                v = e[f]
                assert isinstance(v, (int, float)), f"{e['id']}.{f} not numeric"
                assert v >= 0, f"{e['id']}.{f} is negative"

    def test_ids_are_unique(self):
        ids = [e["id"] for e in self.entries]
        assert len(ids) == len(set(ids)), "duplicate ids in pricing.json"

    def test_vendors_in_allowed_set(self):
        allowed = {"claude", "openai", "xai", "kimi", "cursor"}
        seen = {e["vendor"] for e in self.entries}
        unknown = seen - allowed
        assert not unknown, f"unknown vendors: {unknown}"

    def test_exactly_one_empty_pattern_fallback(self):
        empties = [e for e in self.entries if e["pattern"] == ""]
        assert len(empties) == 1, "expect exactly one fallback entry with empty pattern"
        assert empties[0] is self.entries[-1], "fallback must be the last entry"

    def test_more_specific_patterns_precede_generic_ones(self):
        # First-match-wins requires e.g. opus-4-7 before opus-4 before opus.
        # Verify for every pair where pattern A is a strict superstring of B,
        # A appears at a lower index than B.
        patterns = [(i, e["pattern"]) for i, e in enumerate(self.entries) if e["pattern"]]
        for i, a in patterns:
            for j, b in patterns:
                if i == j or not b:
                    continue
                if a != b and b in a:
                    assert i < j, (
                        f"pattern {a!r} (index {i}) more specific than {b!r} (index {j}) "
                        f"but appears after it"
                    )


# ── load_pricing helpers ───────────────────────────────────────────────────

class TestClaudeLoadPricing:
    def test_default_vendors_is_claude_only(self):
        entries = cm.load_pricing()
        assert {e["vendor"] for e in entries} == {"claude"}

    def test_excludes_other_vendors(self):
        entries = cm.load_pricing(("claude",))
        for e in entries:
            assert "gpt" not in e["id"]
            assert "grok" not in e["id"]
            assert "kimi" not in e["id"]
            assert "composer" not in e["id"]

    def test_fallback_appended_when_missing(self):
        entries = cm.load_pricing(("claude",))
        last = entries[-1]
        assert last["pattern"] == ""
        # synthesized fallback uses Sonnet-tier rates
        assert last["vendor"] == "claude"
        assert last["output"] > 0

    def test_pattern_matching_resolves_claude_models(self):
        entries = cm.load_pricing(("claude",))
        assert _pick_rate("claude-opus-4-7-20251201", entries)["id"] == "opus-4-7"
        assert _pick_rate("claude-sonnet-4-6", entries)["id"] == "sonnet-4-6"
        assert _pick_rate("claude-haiku-4-5-20251001", entries)["id"] == "haiku-4-5"
        assert _pick_rate("claude-opus-4-1-foo", entries)["id"] == "opus-4-1"

    def test_pattern_matching_falls_back_for_unknown(self):
        entries = cm.load_pricing(("claude",))
        assert _pick_rate("totally-unknown-model", entries)["pattern"] == ""

    def test_module_level_constant_matches_loader(self):
        assert cm.PRICING_DEFAULTS == cm.load_pricing(("claude",))

    def test_claude_rates_match_official_table(self):
        entries = {e["id"]: e for e in cm.load_pricing(("claude",))}
        # https://platform.claude.com/docs/en/about-claude/pricing
        assert entries["opus-4-7"]["input"] == 5.0
        assert entries["opus-4-7"]["output"] == 25.0
        assert entries["opus-4-7"]["cache_hit"] == 0.50
        assert entries["opus-4-1"]["input"] == 15.0
        assert entries["opus-4-1"]["output"] == 75.0
        assert entries["sonnet-4-6"]["input"] == 3.0
        assert entries["sonnet-4-6"]["output"] == 15.0
        assert entries["haiku-4-5"]["input"] == 1.0
        assert entries["haiku-4-5"]["output"] == 5.0


class TestCursorLoadPricing:
    def test_no_filter_returns_all_vendors(self):
        entries = cu.load_pricing()
        vendors = {e["vendor"] for e in entries}
        assert vendors == {"claude", "openai", "xai", "kimi", "cursor"}

    def test_vendor_filter_subset(self):
        entries = cu.load_pricing(("openai", "xai"))
        assert {e["vendor"] for e in entries} == {"openai", "xai"}

    def test_pattern_matching_resolves_cursor_default_to_auto(self):
        entries = cu.load_pricing()
        rate = _pick_rate("default", entries)
        assert rate["id"] == "auto"
        assert rate["label"] == "Cursor Auto"

    def test_pattern_matching_resolves_composer_2(self):
        entries = cu.load_pricing()
        assert _pick_rate("composer-2", entries)["id"] == "composer-2"

    def test_pattern_matching_resolves_gpt_grok_kimi(self):
        entries = cu.load_pricing()
        assert _pick_rate("gpt-5.5", entries)["id"] == "gpt-5-5"
        assert _pick_rate("gpt-4o-mini", entries)["id"] == "gpt-4o-mini"
        assert _pick_rate("grok-4.3-fast", entries)["id"] == "grok-4-3"
        assert _pick_rate("kimi-k2.5-preview", entries)["id"] == "kimi-k2-5"

    def test_fallback_present_for_unknown(self):
        entries = cu.load_pricing()
        rate = _pick_rate("brand-new-mystery-model", entries)
        assert rate["pattern"] == ""

    def test_module_level_constant_matches_loader(self):
        assert cu.PRICING_DEFAULTS == cu.load_pricing()

    def test_cursor_auto_pool_rates(self):
        # https://cursor.com/docs/models — Auto + Composer 2 pool
        entries = {e["id"]: e for e in cu.load_pricing()}
        assert entries["auto"]["input"] == 1.25
        assert entries["auto"]["output"] == 6.00
        assert entries["auto"]["cache_hit"] == 0.25
        assert entries["composer-2"]["input"] == 1.25
        assert entries["composer-2"]["output"] == 6.00


# ── pricing injected into rendered HTML ───────────────────────────────────

def _extract_pricing_defaults(html: str) -> list[dict]:
    """Pull the pricing JSON array out of the report HTML.
    Main report embeds it as `"pricingDefaults": [...]` inside DATA;
    per-conversation viewer embeds it as `const PRICING_DEFAULTS = [...];`.
    """
    i = html.find('"pricingDefaults"')
    if i < 0:
        i = html.find("PRICING_DEFAULTS =")
    assert i >= 0, "pricing array not found in HTML"
    start = html.find("[", i)
    depth = 0
    for j in range(start, len(html)):
        ch = html[j]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return json.loads(html[start:j + 1])
    raise AssertionError("unterminated pricingDefaults array")


def _full_session(**overrides):
    s = {
        "session_id": "abc", "title": "X", "cwd": "/x", "project_name": "x",
        "project_key": "x", "start_time": "2026-01-01T00:00:00+09:00",
        "end_time": "2026-01-01T00:01:00+09:00", "duration_mins": 1,
        "hour": 0, "dow": 0, "user_messages": 1, "assistant_messages": 1,
        "total_messages": 2, "tool_uses": 0, "tool_counts": {},
        "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0,
        "cache_creation_tokens": 0, "total_tokens": 15, "models": [],
        "avg_user_msg_len": 5, "first_user_msg_len": 5,
        "first_user_msg_text": "", "first_asst_msg_text": "",
        "source": "code",
    }
    s.update(overrides)
    return s


class TestPricingInClaudeReport:
    def test_report_html_only_contains_claude_pricing(self):
        sessions = [_full_session()]
        html = cm.build_report_html(sessions, "convs")
        entries = _extract_pricing_defaults(html)
        vendors = {e["vendor"] for e in entries}
        assert vendors == {"claude"}, f"non-claude vendors leaked: {vendors - {'claude'}}"

    def test_report_html_does_not_mention_other_vendor_labels(self):
        sessions = [_full_session()]
        html = cm.build_report_html(sessions, "convs")
        entries = _extract_pricing_defaults(html)
        labels = " ".join(e["label"] for e in entries)
        for forbidden in ("GPT", "Grok", "Kimi", "Composer", "Cursor Auto"):
            assert forbidden not in labels, f"{forbidden!r} found in Claude report pricing"

    def test_per_conversation_html_only_contains_claude_pricing(self):
        session = _full_session(models=["claude-opus-4-7"])
        html = cm.render_conversation_html(session, turns=[])
        entries = _extract_pricing_defaults(html)
        assert {e["vendor"] for e in entries} == {"claude"}


class TestPricingInCursorReport:
    def _session(self):
        return {
            "session_id": "c1", "title": "X", "cwd": "/x", "project_name": "x",
            "project_key": "x", "source": "cursor",
            "start_time": "2026-01-01T00:00:00+09:00",
            "end_time": "2026-01-01T00:01:00+09:00", "duration_mins": 1,
            "hour": 0, "dow": 0, "user_messages": 1, "assistant_messages": 1,
            "total_messages": 2, "tool_uses": 0, "tool_counts": {},
            "input_tokens": 10, "output_tokens": 5,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "cache_5m_tokens": 0, "cache_1h_tokens": 0,
            "total_tokens": 15, "model_tokens": {"default": {"input": 10, "output": 5}},
            "models": ["default"], "tokens_estimated": True,
            "ctx_tokens": 0, "ctx_breakdown": {},
            "thinking_duration_ms": 0, "turn_duration_ms": 0,
            "request_ids": [], "avg_user_msg_len": 5,
            "first_user_msg_len": 5, "first_user_msg_text": "",
            "first_asst_msg_text": "", "status": "",
            "lines_added": 0, "lines_removed": 0, "files_changed": 0,
        }

    def test_report_html_contains_all_vendors(self):
        html = cu.build_report_html([self._session()], "convs")
        entries = _extract_pricing_defaults(html)
        vendors = {e["vendor"] for e in entries}
        assert vendors == {"claude", "openai", "xai", "kimi", "cursor"}

    def test_per_conversation_html_contains_all_vendors(self):
        html = cu.render_conversation_html(self._session(), bubbles=[])
        entries = _extract_pricing_defaults(html)
        assert "cursor" in {e["vendor"] for e in entries}
        assert "openai" in {e["vendor"] for e in entries}
