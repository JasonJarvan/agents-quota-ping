"""Tests for retry, fallback execution, and JSON token usage extraction."""

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
)


class ProviderExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workdir = Path(self.tmp.name)
        self.cfg = load_config(self.workdir / "missing-config.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_tier1_success_extracts_claude_usage(self) -> None:
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
        self.assertIsNotNone(res.usage)
        assert res.usage is not None
        self.assertEqual(res.usage.total(), 174)
        self.assertAlmostEqual(res.usage.cost_usd or 0.0, 0.00012)

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

    def test_usage_parsers_handle_edge_cases(self) -> None:
        self.assertIsNone(parse_claude_json_usage("not json"))
        self.assertIsNone(parse_codex_jsonl_usage("plain text"))
        u = parse_agy_json_usage('{"stats":{"usage":{"input_tokens":10,"output_tokens":2,"total_tokens":12}}}')
        assert u is not None
        self.assertEqual(u.total(), 12)
