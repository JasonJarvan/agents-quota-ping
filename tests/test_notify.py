"""Tests for report formatting, Feishu delivery, and webhook fallback."""

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from agents_quota_ping.config import load_config
from agents_quota_ping.notify import format_report, send_notifications
from agents_quota_ping.providers import PingResult, TokenUsage
from agents_quota_ping.quota import QuotaSnapshot


class NotifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config(Path(self.tmp.name) / "none.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_format_all_ok_with_real_reset_and_tokens(self) -> None:
        dt = datetime(2026, 11, 17, 7, 0, 20)
        reset_utc = datetime(2026, 11, 17, 4, 0, 0, tzinfo=timezone.utc)
        msg = format_report(
            [
                PingResult(
                    "Claude",
                    "ok",
                    1,
                    "haiku",
                    2.1,
                    usage=TokenUsage(input_tokens=150, output_tokens=3, cost_usd=0.0001),
                    quota=QuotaSnapshot("Claude", reset_at=reset_utc, used_percent=5, secondary_label="7d", secondary_used_percent=20),
                ),
                PingResult("Codex", "ok", 1, "gpt-6-luna", 1.8),
            ],
            finished_at=dt,
            codex_profile="work",
            jitter_sec=15,
        )
        self.assertIn("✅", msg)
        self.assertIn("153 tok", msg)
        self.assertIn("实测重置", msg)
        self.assertIn("5h已用5%", msg)
        self.assertIn("Codex (work)", msg)
        self.assertIn("预计重置 ~12:00", msg)

    def test_format_degraded_and_fail(self) -> None:
        dt = datetime(2026, 11, 17, 12, 2, 18)
        msg = format_report(
            [
                PingResult("Claude", "degraded", 2, "sonnet", 4.0, detail="haiku retired"),
                PingResult("Codex", "fail", 1, "gpt-6-luna", 3.1, detail="auth_or_quota: 401"),
            ],
            finished_at=dt,
        )
        self.assertIn("❌", msg)
        self.assertIn("异常/降级详情", msg)
        self.assertIn("haiku retired", msg)

    def test_send_feishu_falls_back_from_bot_to_user_and_disables_proxy(self) -> None:
        calls: list[tuple[list[str], dict[str, str]]] = []

        def fake_sender(argv, env, _timeout):
            calls.append((argv, env))
            if "--as" in argv and argv[argv.index("--as") + 1] == "bot":
                return 1, "bot disabled"
            return 0, "ok"

        cfg = self.cfg.__class__(**{**self.cfg.__dict__, "feishu_user_id": "ou_test"})
        ok = send_notifications(
            "hello",
            cfg,
            base_env={"http_proxy": "http://127.0.0.1:7890", "HTTPS_PROXY": "http://127.0.0.1:7890"},
            sender=fake_sender,
        )
        self.assertTrue(ok)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1].get("LARK_CLI_NO_PROXY"), "1")
        self.assertNotIn("http_proxy", calls[0][1])
        self.assertNotIn("HTTPS_PROXY", calls[0][1])

    def test_send_webhook_when_configured(self) -> None:
        seen: list[tuple[str, str]] = []

        def fake_webhook(url, msg, _proxy):
            seen.append((url, msg))
            return True, "HTTP 200"

        cfg = self.cfg.__class__(**{**self.cfg.__dict__, "webhook_url": "https://example.invalid/hook"})
        ok = send_notifications("hi", cfg, webhook_fn=fake_webhook)
        self.assertTrue(ok)
        self.assertEqual(seen, [("https://example.invalid/hook", "hi")])
