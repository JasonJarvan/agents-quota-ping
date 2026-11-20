"""Tests for failure classification and dynamic cheapest fallback selection."""

import json
from pathlib import Path
import tempfile
import unittest

from agents_quota_ping.classify import (
    ErrorKind,
    classify_failure,
    extract_error_summary,
    pick_agy_pool1_models,
    pick_agy_pool2_models,
    pick_codex_fallback_models,
)


class ClassifyTests(unittest.TestCase):
    def test_timeout_is_transient(self) -> None:
        self.assertEqual(classify_failure(None, "", timed_out=True), ErrorKind.TRANSIENT)

    def test_network_502_is_transient(self) -> None:
        self.assertEqual(classify_failure(1, "HTTP 502 Bad Gateway"), ErrorKind.TRANSIENT)
        self.assertEqual(classify_failure(78, "AI trace request failed\nnetwork check failed"), ErrorKind.TRANSIENT)
        self.assertEqual(
            classify_failure(1, '{"type":"turn.failed","error":{"message":"workspace routing discovery failed"}}'),
            ErrorKind.TRANSIENT,
        )

    def test_auth_and_quota_are_hard_stop(self) -> None:
        self.assertEqual(classify_failure(1, "401 Unauthorized: invalid_jwt"), ErrorKind.AUTH_OR_QUOTA)
        self.assertEqual(classify_failure(1, "Error 429 RESOURCE_EXHAUSTED"), ErrorKind.AUTH_OR_QUOTA)
        self.assertEqual(
            classify_failure(1, "Claude agent network cage unavailable; refusing an unguarded launch."),
            ErrorKind.AUTH_OR_QUOTA,
        )

    def test_model_retired_and_unknown_flag_are_fallback_eligible(self) -> None:
        self.assertEqual(classify_failure(1, "Error: model 'gpt-6-luna' does not exist"), ErrorKind.MODEL_OR_FLAG)
        self.assertEqual(classify_failure(1, "403 model claude-haiku-5-5 not available"), ErrorKind.MODEL_OR_FLAG)
        self.assertEqual(classify_failure(2, "error: unrecognized option '--safe-mode'"), ErrorKind.MODEL_OR_FLAG)

    def test_extract_error_summary_strips_ansi(self) -> None:
        raw = "\x1b[31mError: token expired\x1b[0m\n"
        self.assertEqual(extract_error_summary(raw), "Error: token expired")

    def test_pick_codex_fallback_models_prefers_cheapest_visible(self) -> None:
        sample = {
            "models": [
                {"slug": "gpt-6-astra", "visibility": "list", "priority": 2, "default_reasoning_level": "medium"},
                {"slug": "gpt-6.1-sol", "visibility": "list", "priority": 1, "default_reasoning_level": "low"},
                {"slug": "gpt-6-luna", "visibility": "list", "priority": 6, "default_reasoning_level": "medium"},
                {"slug": "gpt-5.6-luna", "visibility": "list", "priority": 9, "default_reasoning_level": "medium"},
                {"slug": "gpt-reserve", "visibility": "hide", "priority": 5, "default_reasoning_level": "medium"},
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "models_cache.json"
            cache.write_text(json.dumps(sample), encoding="utf-8")
            picked = pick_codex_fallback_models(cache, exclude={"gpt-6-luna"})
        self.assertEqual(picked[0], "gpt-5.6-luna")
        self.assertEqual(picked[1], "gpt-6.1-sol")
        self.assertNotIn("gpt-reserve", picked)
        self.assertNotIn("gpt-6-luna", picked)

    def test_pick_agy_pool1_models_excludes_argon_claude_gpt(self) -> None:
        raw = (
            "\r⠋ Fetching available models...\r"
            "gemini-4-argon-low              Gemini 4 Argon (Low)\n"
            "gemini-4-hydrogen-low           Gemini 4 Hydrogen (Low)\n"
            "gemini-pro-orchestrator-g1-pro  Orchestrator (Argon)\n"
            "gemini-3.8-flash-medium         Gemini 3.8 Flash (Medium)\n"
            "gemini-3.8-flash-low            Gemini 3.8 Flash (Low)\n"
            "claude-sonnet-5-5-low           Claude Sonnet 5.5 (Low)\n"
            "gpt-oss-120b-medium             GPT-OSS 120B (Medium)\n"
        )
        picked = pick_agy_pool1_models(raw, exclude={"gemini-3.8-flash-low"})
        self.assertEqual(picked, ["gemini-3.8-flash-medium", "gemini-4-hydrogen-low"])

    def test_pick_agy_pool2_models_ranks_cheapest_and_excludes_gemini(self) -> None:
        raw = (
            "gemini-4-neon-low               Gemini 4 Neon (Low)\n"
            "gemini-3.8-flash-low            Gemini 3.8 Flash (Low)\n"
            "claude-opus-5-5-low             Claude Opus 5.5 (Low)\n"
            "claude-sonnet-5-5-medium        Claude Sonnet 5.5 (Medium)\n"
            "claude-sonnet-5-5-low           Claude Sonnet 5.5 (Low)\n"
            "gpt-oss-120b-medium             GPT-OSS 120B (Medium)\n"
        )
        self.assertEqual(
            pick_agy_pool2_models(raw),
            ["gpt-oss-120b-medium", "claude-sonnet-5-5-low", "claude-sonnet-5-5-medium", "claude-opus-5-5-low"],
        )
        self.assertEqual(
            pick_agy_pool2_models(raw, exclude={"gpt-oss-120b-medium"}),
            ["claude-sonnet-5-5-low", "claude-sonnet-5-5-medium", "claude-opus-5-5-low"],
        )
