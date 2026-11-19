"""Tests for live quota API payload parsers."""

from datetime import datetime, timezone
import unittest

from agents_quota_ping.quota import (
    parse_agy_models_payload,
    parse_agy_pool2_models_payload,
    parse_claude_usage_payload,
    parse_codex_usage_payload,
)


class QuotaParserTests(unittest.TestCase):
    def test_parse_claude_usage_payload(self) -> None:
        payload = {
            "five_hour": {"utilization": 81.0, "resets_at": "2026-11-17T11:20:00.347156+00:00"},
            "seven_day": {"utilization": 75.0, "resets_at": "2026-11-18T04:00:01+00:00"},
        }
        snap = parse_claude_usage_payload(payload)
        self.assertEqual(snap.used_percent, 81)
        self.assertEqual(snap.remaining_percent, 19)
        self.assertEqual(snap.secondary_label, "7d")
        self.assertEqual(snap.secondary_used_percent, 75)
        self.assertEqual(snap.reset_at, datetime(2026, 11, 17, 11, 20, 0, 347156, tzinfo=timezone.utc))

    def test_parse_codex_usage_payload(self) -> None:
        payload = {
            "rate_limit": {
                "primary_window": {"used_percent": 90, "limit_window_seconds": 18000, "reset_at": 1794899367},
                "secondary_window": {"used_percent": 43, "limit_window_seconds": 604800, "reset_at": 1795403110},
            }
        }
        snap = parse_codex_usage_payload(payload)
        self.assertEqual(snap.window_label, "5h")
        self.assertEqual(snap.used_percent, 90)
        self.assertEqual(snap.secondary_label, "7d")
        self.assertEqual(snap.secondary_used_percent, 43)
        self.assertEqual(snap.reset_at, datetime.fromtimestamp(1794899367, tz=timezone.utc))

    def test_parse_agy_models_payload_prefers_pool1_and_ignores_argon(self) -> None:
        payload = {
            "models": {
                "gemini-4-argon-low": {"quotaInfo": {"remainingFraction": 0.14, "resetTime": "2026-11-21T10:00:00Z"}},
                "claude-sonnet-5-5-low": {"quotaInfo": {"remainingFraction": 1.0, "resetTime": "2026-11-17T12:05:15Z"}},
                "gemini-3.8-flash-tiered": {"quotaInfo": {"remainingFraction": 0.595, "resetTime": "2026-11-17T07:39:24Z"}},
            }
        }
        snap = parse_agy_models_payload(payload)
        self.assertEqual(snap.remaining_percent, 60)
        self.assertEqual(snap.used_percent, 40)
        self.assertEqual(snap.reset_at, datetime(2026, 11, 17, 7, 39, 24, tzinfo=timezone.utc))

    def test_parse_agy_pool2_models_payload_prefers_claude_gpt_and_ignores_gemini(self) -> None:
        payload = {
            "models": {
                "gemini-3.8-flash-tiered": {"quotaInfo": {"remainingFraction": 0.96, "resetTime": "2026-11-19T17:54:08Z"}},
                "gpt-oss-120b-medium": {"quotaInfo": {"remainingFraction": 0.89, "resetTime": "2026-11-19T16:27:04Z"}},
                "claude-sonnet-5-5-low": {"quotaInfo": {"remainingFraction": 0.89, "resetTime": "2026-11-19T16:27:04Z"}},
            }
        }
        snap = parse_agy_pool2_models_payload(payload)
        self.assertEqual(snap.provider, "Antigravity (Claude/GPT)")
        self.assertEqual(snap.remaining_percent, 89)
        self.assertEqual(snap.used_percent, 11)
        self.assertEqual(snap.reset_at, datetime(2026, 11, 19, 16, 27, 4, tzinfo=timezone.utc))

    def test_parse_agy_pool2_models_payload_missing_returns_error(self) -> None:
        payload = {
            "models": {
                "gemini-3.8-flash-tiered": {"quotaInfo": {"remainingFraction": 0.96, "resetTime": "2026-11-19T17:54:08Z"}},
            }
        }
        snap = parse_agy_pool2_models_payload(payload)
        self.assertIsNone(snap.reset_at)
        self.assertIn("Pool 2", snap.error)
