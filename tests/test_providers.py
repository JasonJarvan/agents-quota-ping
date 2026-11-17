"""Tests for pre-flight checks, retry, fallback execution, and JSON token usage extraction."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from agents_quota_ping.config import load_config
from agents_quota_ping.providers import (
    CommandTier,
    discover_agy_fallbacks,
    execute_with_fallback,
    parse_agy_json_usage,
    parse_claude_json_usage,
    parse_codex_jsonl_usage,
    run_single_provider,
    should_skip_preflight,
)
from agents_quota_ping.quota import QuotaSnapshot


class ProviderExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workdir = Path(self.tmp.name)
        loaded = load_config(self.workdir / "missing-config.json")
        empty = self.workdir / "empty"
        empty.mkdir()
        self.cfg = replace(
            loaded,
            state_dir=self.workdir,
            empty_workdir=empty,
            lock_file=self.workdir / "run.lock",
            log_file=self.workdir / "quota-ping.log",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_tier1_success_extracts_claude_usage_and_splits_cached(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv, _env, _cwd, _timeout):
            calls.append(argv)
            stdout = '{"type":"result","is_error":false,"total_cost_usd":0.00012,"usage":{"input_tokens":120,"cache_read_input_tokens":50,"output_tokens":4}}'
            return 0, stdout, "", False

        t1 = CommandTier(1, "haiku", ["claude", "--model", "haiku"])
        res = execute_with_fallback(
            "Claude",
            "claude",
            t1,
            lambda: [CommandTier(2, "sonnet", ["claude", "--model", "sonnet"])],
            {},
            self.workdir,
            run_cmd=fake_run,
            sleep_fn=lambda _s: None,
        )
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.tier, 1)
        self.assertEqual(len(calls), 1)
        assert res.usage is not None
        self.assertEqual(res.usage.total(), 174)
        self.assertIn("in 120+cache 50/out 4", res.usage.format_short())

    def test_transient_retries_tier1_without_escalating(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv, _env, _cwd, _timeout):
            calls.append(argv)
            if len(calls) == 1:
                return 1, "", "HTTP 502 Bad Gateway", False
            return 0, '{"type":"turn.completed","usage":{"input_tokens":300,"cached_input_tokens":200,"output_tokens":5}}', "", False

        t1 = CommandTier(1, "gpt-6-luna", ["codex", "-m", "gpt-6-luna"])
        res = execute_with_fallback(
            "Codex",
            "codex",
            t1,
            lambda: [CommandTier(2, "gpt-6.1-sol", ["codex", "-m", "gpt-6.1-sol"])],
            {},
            self.workdir,
            run_cmd=fake_run,
            sleep_fn=lambda _s: None,
        )
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.tier, 1)
        self.assertEqual(len(calls), 2)
        assert res.usage is not None
        self.assertEqual(res.usage.total(), 505)

    def test_auth_error_never_falls_back_to_expensive_model(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv, _env, _cwd, _timeout):
            calls.append(argv)
            return 1, "", "401 Unauthorized: login required", False

        t1 = CommandTier(1, "haiku", ["claude", "--model", "haiku"])
        res = execute_with_fallback(
            "Claude",
            "claude",
            t1,
            lambda: [CommandTier(2, "sonnet", ["claude", "--model", "sonnet"])],
            {},
            self.workdir,
            run_cmd=fake_run,
            sleep_fn=lambda _s: None,
        )
        self.assertEqual(res.status, "fail")
        self.assertEqual(len(calls), 1)
        self.assertIn("auth_or_quota", res.detail)

    def test_model_retired_falls_back_to_tier2_degraded(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv, _env, _cwd, _timeout):
            calls.append(argv)
            if "--model" in argv and "gemini-3.8-flash-low" in argv:
                return 1, "", "Error: unknown model gemini-3.8-flash-low", False
            return 0, '{"response":"OK","usage":{"input_tokens":88,"output_tokens":3,"total_tokens":91}}', "", False

        t1 = CommandTier(1, "gemini-3.8-flash-low", ["agy", "--model", "gemini-3.8-flash-low"])
        res = execute_with_fallback(
            "Antigravity",
            "agy",
            t1,
            lambda: [CommandTier(2, "gemini-3.8-flash-medium", ["agy", "--model", "gemini-3.8-flash-medium"])],
            {},
            self.workdir,
            run_cmd=fake_run,
            sleep_fn=lambda _s: None,
        )
        self.assertEqual(res.status, "degraded")
        self.assertEqual(res.tier, 2)
        self.assertEqual(res.model, "gemini-3.8-flash-medium")
        self.assertEqual(len(calls), 2)
        assert res.usage is not None
        self.assertEqual(res.usage.total(), 91)

    def test_discover_agy_fallbacks_stays_in_pool1(self) -> None:
        def fake_run(_argv, _env, _cwd, _timeout):
            return (
                0,
                "gemini-4-argon-low Argon\n"
                "claude-opus-5-5-low Claude\n"
                "gemini-4-hydrogen-low Hydrogen\n"
                "gemini-3.8-flash-medium FlashMed\n",
                "",
                False,
            )

        tiers = discover_agy_fallbacks(self.cfg, fake_run, self.workdir, {})
        self.assertEqual([t.model for t in tiers], ["gemini-3.8-flash-medium", "gemini-4-hydrogen-low", "default"])

    def test_preflight_skips_exhausted_and_active_but_pings_unused(self) -> None:
        now = datetime(2026, 11, 17, 12, 2, 0, tzinfo=timezone.utc)

        # Exhausted -> always skip
        exhausted = QuotaSnapshot("Antigravity", reset_at=now + timedelta(hours=2), used_percent=100, remaining_percent=0)
        self.assertIsNotNone(should_skip_preflight(exhausted, now_utc=now, skip_if_active=False))

        # Weekly cooldown (> 5h30m) -> always skip
        weekly = QuotaSnapshot("Antigravity", reset_at=now + timedelta(days=3), used_percent=86, remaining_percent=14)
        self.assertIsNotNone(should_skip_preflight(weekly, now_utc=now, skip_if_active=False))

        # Active 5h window (used > 0) -> skip when skip_if_active=True, ping when False
        active = QuotaSnapshot("Claude", reset_at=now + timedelta(hours=3), used_percent=20, remaining_percent=80)
        self.assertIsNotNone(should_skip_preflight(active, now_utc=now, skip_if_active=True))
        self.assertIsNone(should_skip_preflight(active, now_utc=now, skip_if_active=False))

        # Unused 0% window (floating now+5h) -> never skip!
        unused = QuotaSnapshot("Codex", reset_at=now + timedelta(hours=5), used_percent=0, remaining_percent=100)
        self.assertIsNone(should_skip_preflight(unused, now_utc=now, skip_if_active=True))

    def test_run_single_provider_preflight_skip_avoids_calling_cli(self) -> None:
        called = False

        def fake_run(_argv, _env, _cwd, _timeout):
            nonlocal called
            called = True
            return 0, "", "", False

        future_reset = datetime.now(timezone.utc) + timedelta(hours=2)
        res = run_single_provider(
            "claude",
            self.cfg,
            {},
            run_cmd=fake_run,
            quota_probe=lambda _n, _c: QuotaSnapshot("Claude", reset_at=future_reset, used_percent=40, remaining_percent=60),
        )
        self.assertFalse(called)
        self.assertEqual(res.status, "skipped")

    def test_usage_parsers_handle_edge_cases(self) -> None:
        self.assertIsNone(parse_claude_json_usage("not json"))
        self.assertIsNone(parse_codex_jsonl_usage("plain text"))
        u = parse_agy_json_usage('{"stats":{"usage":{"input_tokens":10,"output_tokens":2,"total_tokens":12}}}')
        assert u is not None
        self.assertEqual(u.total(), 12)
