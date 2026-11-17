"""Tests for cross-platform config, binary paths, and Windows command wrapping."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agents_quota_ping.config import (
    default_config_dir,
    default_state_dir,
    load_config,
    prepare_command_argv,
)


class ConfigTests(unittest.TestCase):
    def test_windows_paths_and_cmd_wrapper(self) -> None:
        home = Path("/tmp/winhome")
        with patch.dict(os.environ, {"APPDATA": "C:\\Users\\demo\\AppData\\Roaming", "LOCALAPPDATA": "C:\\Users\\demo\\AppData\\Local"}, clear=False):
            cfg_dir = default_config_dir(home=home, platform="win32")
            self.assertTrue(str(cfg_dir).endswith("agents-quota-ping"))
        self.assertEqual(
            prepare_command_argv(["C:\\npm\\codex.cmd", "exec"], platform="win32"),
            ["cmd.exe", "/c", "C:\\npm\\codex.cmd", "exec"],
        )
        self.assertEqual(
            prepare_command_argv(["/usr/bin/codex", "exec"], platform="linux"),
            ["/usr/bin/codex", "exec"],
        )

    def test_load_config_reads_json_without_hardcoded_personal_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "config.json"
            cfg_file.write_text(
                json.dumps(
                    {
                        "proxy": {"http_proxy": "http://127.0.0.1:8888"},
                        "notify": {"webhook_url": "https://example.invalid/hook"},
                    }
                ),
                encoding="utf-8",
            )
            clean_env = {"HOME": tmp, "USERPROFILE": tmp}
            with patch.dict(os.environ, clean_env, clear=True):
                cfg = load_config(cfg_file)
            self.assertEqual(cfg.http_proxy, "http://127.0.0.1:8888")
            self.assertEqual(cfg.webhook_url, "https://example.invalid/hook")
            self.assertEqual(cfg.feishu_user_id, "")
            self.assertTrue(cfg.followup_enabled)
            self.assertEqual(cfg.followup_cutoff, "18:30")
            self.assertEqual((cfg.followup_jitter_min, cfg.followup_jitter_max), (15, 45))

    def test_followup_schedule_json_and_env_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "config.json"
            cfg_file.write_text(
                json.dumps(
                    {
                        "schedule": {
                            "followup_enabled": False,
                            "followup_cutoff": "19:00",
                            "followup_jitter_min": 20,
                            "followup_jitter_max": 40,
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"HOME": tmp, "USERPROFILE": tmp}, clear=True):
                cfg = load_config(cfg_file)
            self.assertFalse(cfg.followup_enabled)
            self.assertEqual(cfg.followup_cutoff, "19:00")
            self.assertEqual(cfg.followup_jitter_min, 20)
            self.assertEqual(cfg.followup_jitter_max, 40)

            with patch.dict(
                os.environ,
                {
                    "HOME": tmp,
                    "USERPROFILE": tmp,
                    "QUOTA_PING_FOLLOWUP_ENABLED": "true",
                    "QUOTA_PING_FOLLOWUP_CUTOFF": "17:45",
                },
                clear=True,
            ):
                overridden = load_config(cfg_file)
            self.assertTrue(overridden.followup_enabled)
            self.assertEqual(overridden.followup_cutoff, "17:45")
            self.assertEqual(overridden.followup_jitter_min, 20)

            with patch.dict(
                os.environ,
                {"HOME": tmp, "USERPROFILE": tmp, "QUOTA_PING_FOLLOWUP_ENABLED": "off"},
                clear=True,
            ):
                disabled = load_config(cfg_file)
            self.assertFalse(disabled.followup_enabled)

    def test_state_dir_respects_xdg_and_override(self) -> None:
        with patch.dict(os.environ, {"QUOTA_PING_STATE_DIR": "/tmp/custom-state", "HOME": "/tmp"}, clear=True):
            self.assertEqual(default_state_dir(platform="linux"), Path("/tmp/custom-state"))
