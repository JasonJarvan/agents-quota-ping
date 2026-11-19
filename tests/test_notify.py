"""Tests for report formatting, Feishu delivery, and webhook fallback."""

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from agents_quota_ping.config import load_config
from agents_quota_ping.notify import format_report, send_notifications, should_notify
from agents_quota_ping.providers import PingResult, TokenUsage
from agents_quota_ping.quota import QuotaSnapshot
from agents_quota_ping import runner


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

    def test_format_skipped_does_not_trigger_error_section(self) -> None:
        dt = datetime(2026, 11, 17, 12, 2, 18)
        msg = format_report(
            [
                PingResult("Claude", "skipped", 0, "preflight-skip", 0.0, detail="5h 窗口已激活 (已用 40%，15:20:00 重置)"),
                PingResult("Codex", "ok", 1, "gpt-6-luna", 2.0),
            ],
            finished_at=dt,
        )
        self.assertIn("✅", msg)
        self.assertIn("⏭", msg)
        self.assertNotIn("异常/降级详情", msg)

    def test_format_includes_concise_followup_note(self) -> None:
        dt = datetime(2026, 11, 17, 12, 0, 0)
        msg = format_report(
            [
                PingResult("Claude", "ok", 1, "haiku", 1.0, followup_note="已预约 14:30:22"),
                PingResult(
                    "Codex",
                    "skipped",
                    0,
                    "preflight-skip",
                    0.0,
                    detail="配额已耗尽",
                    followup_note="已过当日截止 18:30，交由次日 07:00 定时",
                ),
                PingResult(
                    "Antigravity",
                    "ok",
                    1,
                    "gemini-3.8-flash-low",
                    1.0,
                    followup_note="冷却期 >5.5h，交由常规 07:00 定时兜底",
                ),
            ],
            finished_at=dt,
        )
        self.assertIn("已预约 14:30:22", msg)
        self.assertIn(">18:30 不预约", msg)
        self.assertIn(">5.5h 不预约", msg)
        self.assertNotIn("交由次日", msg)

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

    def test_should_notify_suppresses_all_skipped_runs(self) -> None:
        self.assertFalse(should_notify([]))
        self.assertFalse(
            should_notify(
                [
                    PingResult("Claude", "skipped", 0, "preflight-skip", 0.0, detail="active"),
                    PingResult("Antigravity (Claude/GPT)", "skipped", 0, "preflight-skip", 0.0, detail="active"),
                ]
            )
        )
        self.assertTrue(
            should_notify(
                [
                    PingResult("Claude", "skipped", 0, "preflight-skip", 0.0, detail="active"),
                    PingResult("Antigravity (Claude/GPT)", "ok", 1, "gpt-oss-120b-medium", 2.0),
                ]
            )
        )
        self.assertTrue(should_notify([PingResult("Claude", "fail", 1, "haiku", 1.0, detail="401")]))

    def test_runner_skips_notifications_when_all_providers_skipped(self) -> None:
        from unittest.mock import patch

        sent: list[str] = []

        def fake_run_single(name, *_args, **_kwargs):
            return PingResult(runner.PROVIDER_DISPLAY[name], "skipped", 0, "preflight-skip", 0.0, detail="active")

        def fake_send(msg, *_args, **_kwargs):
            sent.append(msg)
            return True

        with (
            patch.object(runner, "load_config", return_value=self.cfg),
            patch.object(runner, "run_single_provider", side_effect=fake_run_single),
            patch.object(runner, "attach_followup", side_effect=lambda _n, r, *_a, **_k: r),
            patch.object(runner, "send_notifications", side_effect=fake_send),
        ):
            rc = runner.main(["--only", "claude,agy-claude", "--no-jitter"])
        self.assertEqual(rc, 0)
        self.assertEqual(sent, [])

        with (
            patch.object(runner, "load_config", return_value=self.cfg),
            patch.object(
                runner,
                "run_single_provider",
                side_effect=lambda name, *_a, **_k: PingResult(
                    runner.PROVIDER_DISPLAY[name],
                    "ok" if name == "agy-claude" else "skipped",
                    1 if name == "agy-claude" else 0,
                    "gpt-oss-120b-medium" if name == "agy-claude" else "preflight-skip",
                    1.0 if name == "agy-claude" else 0.0,
                ),
            ),
            patch.object(runner, "attach_followup", side_effect=lambda _n, r, *_a, **_k: r),
            patch.object(runner, "send_notifications", side_effect=fake_send),
        ):
            rc = runner.main(["--only", "claude,agy-claude", "--no-jitter"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(sent), 1)
        self.assertIn("Antigravity (Claude/GPT)", sent[0])
