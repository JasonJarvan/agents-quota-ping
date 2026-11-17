"""Tests for follow-up planning, OS one-shot registration, and recent-ping dedup."""

from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import sys
import plistlib
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from agents_quota_ping.config import AppConfig, load_config
from agents_quota_ping.providers import PingResult, run_single_provider, should_skip_preflight
from agents_quota_ping.quota import QuotaSnapshot
from agents_quota_ping.runner import _serialize_results, main, parse_args
from agents_quota_ping.scheduler import attach_followup, floor_minute, plan_followup, schedule_followup

TZ = timezone(timedelta(hours=8))
_COOLDOWN = "冷却期 >5.5h，交由常规 07:00 定时兜底"
_CUTOFF = "已过当日截止 18:30，交由次日 07:00 定时"


def _cfg(root: Path) -> AppConfig:
    loaded = load_config(root / "missing.json")
    empty = root / "empty"
    empty.mkdir(exist_ok=True)
    return replace(
        loaded,
        state_dir=root,
        empty_workdir=empty,
        lock_file=root / "run.lock",
        log_file=root / "quota-ping.log",
        followup_enabled=True,
        followup_cutoff="18:30",
        followup_jitter_min=15,
        followup_jitter_max=45,
    )


def _snap(reset_local: datetime, **overrides: object) -> QuotaSnapshot:
    reset_at = reset_local.astimezone(timezone.utc) if reset_local.tzinfo else reset_local.replace(tzinfo=timezone.utc)
    fields: dict[str, object] = {
        "provider": "Codex",
        "reset_at": reset_at,
        "used_percent": 10,
        "remaining_percent": 90,
    }
    fields.update(overrides)
    return QuotaSnapshot(**fields)  # type: ignore[arg-type]


def _at(hour: int, minute: int, second: int = 0, day: int = 17) -> datetime:
    return datetime(2026, 11, day, hour, minute, second, tzinfo=TZ)


class PlanFollowupTests(unittest.TestCase):
    def _plan(self, snap: QuotaSnapshot, now: datetime, **kwargs: object):
        jitter = kwargs.pop("jitter", 22)
        with patch("agents_quota_ping.scheduler.random.randint", return_value=jitter) as rand:
            target, reason = plan_followup(snap, now_local=now, **kwargs)  # type: ignore[arg-type]
        return target, reason, rand

    def test_schedules_inside_horizon_and_cutoff_with_second_jitter(self) -> None:
        now = _at(12, 0)
        reset = _at(14, 30)
        target, reason, rand = self._plan(_snap(reset), now)
        self.assertEqual(reason, "")
        self.assertEqual(target, reset + timedelta(seconds=22))
        rand.assert_called_once_with(15, 45)

    def test_ok_with_rounded_zero_percent_still_schedules(self) -> None:
        now = _at(9, 0)
        reset = _at(13, 0)
        target, reason, _rand = self._plan(_snap(reset, used_percent=0, remaining_percent=100), now, ping_status="ok")
        self.assertEqual(reason, "")
        self.assertEqual(target, reset + timedelta(seconds=22))

    def test_degraded_and_skipped_active_or_exhausted_still_schedule(self) -> None:
        now = _at(9, 0)
        reset = _at(12, 30)
        degraded, degraded_reason, _rand = self._plan(_snap(reset), now, ping_status="degraded")
        active, active_reason, _rand = self._plan(
            _snap(reset, used_percent=40, remaining_percent=60),
            now,
            ping_status="skipped",
        )
        empty, empty_reason, _rand = self._plan(
            _snap(reset, used_percent=100, remaining_percent=0),
            now,
            ping_status="skipped",
        )
        self.assertEqual(degraded_reason, "")
        self.assertEqual(active_reason, "")
        self.assertEqual(empty_reason, "")
        self.assertEqual(degraded, reset + timedelta(seconds=22))
        self.assertEqual(active, reset + timedelta(seconds=22))
        self.assertEqual(empty, reset + timedelta(seconds=22))

    def test_horizon_is_inclusive_at_5h30m_and_rejects_anything_longer(self) -> None:
        now = _at(8, 0)
        on_limit, on_reason, _rand = self._plan(_snap(now + timedelta(hours=5, minutes=30)), now)
        self.assertEqual(on_reason, "")
        self.assertEqual(on_limit, now + timedelta(hours=5, minutes=30, seconds=22))

        past, past_reason, _rand = self._plan(_snap(now + timedelta(hours=5, minutes=30, seconds=1)), now)
        self.assertIsNone(past)
        self.assertEqual(past_reason, _COOLDOWN)

    def test_fail_never_schedules_even_when_reset_is_far(self) -> None:
        now = _at(8, 0)
        near = _snap(now + timedelta(hours=2))
        far = _snap(now + timedelta(days=2))
        self.assertEqual(plan_followup(near, now_local=now, ping_status="fail"), (None, ""))
        self.assertEqual(plan_followup(far, now_local=now, ping_status="fail"), (None, ""))
        self.assertEqual(plan_followup(QuotaSnapshot("Codex"), now_local=now, ping_status="ok"), (None, ""))
        self.assertEqual(plan_followup(_snap(now - timedelta(minutes=1)), now_local=now), (None, ""))

    def test_skipped_without_percent_data_does_not_schedule(self) -> None:
        now = _at(10, 0)
        snap = _snap(now + timedelta(hours=2), used_percent=None, remaining_percent=None)
        self.assertEqual(plan_followup(snap, now_local=now, ping_status="skipped"), (None, ""))

    def test_cutoff_uses_hour_and_minute_and_rejects_the_next_minute(self) -> None:
        now = _at(14, 0)
        allowed, allowed_reason, _rand = self._plan(_snap(_at(18, 29, 40)), now, jitter=20)
        self.assertEqual(allowed_reason, "")
        self.assertEqual(allowed, _at(18, 30, 0))

        blocked, blocked_reason, _rand = self._plan(_snap(_at(18, 30, 50)), now, jitter=15)
        self.assertIsNone(blocked)
        self.assertEqual(blocked_reason, _CUTOFF)

        late, late_reason, _rand = self._plan(_snap(_at(18, 40)), now)
        self.assertIsNone(late)
        self.assertEqual(late_reason, _CUTOFF)

    def test_cross_midnight_is_left_to_the_morning_run(self) -> None:
        now = _at(22, 0)
        target, reason, _rand = self._plan(_snap(_at(1, 0, day=18)), now)
        self.assertIsNone(target)
        self.assertEqual(reason, _CUTOFF)

        rolled, rolled_reason, _rand = self._plan(_snap(_at(23, 59, 50)), _at(20, 0), jitter=20)
        self.assertIsNone(rolled)
        self.assertEqual(rolled_reason, _CUTOFF)

    def test_invalid_cutoff_does_not_raise(self) -> None:
        now = _at(9, 0)
        target, reason, _rand = self._plan(_snap(now + timedelta(hours=2)), now, cutoff_hhmm="25:99")
        self.assertIsNone(target)
        self.assertEqual(reason, "截止时间无效")


class _SchedulerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = _cfg(self.root)
        self.target = datetime(2026, 11, 17, 14, 30, 22)

    def tearDown(self) -> None:
        self.tmp.cleanup()


class LinuxSchedulerTests(_SchedulerCase):
    def _run(self, runner, **kwargs):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("agents_quota_ping.scheduler.os.getuid", return_value=1000, create=True),
            patch("agents_quota_ping.scheduler.shutil.which", return_value="/usr/local/bin/quota-ping"),
            patch("agents_quota_ping.scheduler.sys.argv", ["python"]),
        ):
            return schedule_followup(
                "claude",
                self.target,
                self.cfg,
                cmd_runner=runner,
                platform="linux",
                **kwargs,
            )

    def test_systemd_run_user_argv_env_and_cleanup(self) -> None:
        calls: list[tuple[list[str], dict[str, str]]] = []

        def runner(argv, env):
            calls.append((list(argv), dict(env)))
            if argv[:3] == ["systemctl", "--user", "stop"]:
                return 5, "Unit agents-quota-ping-followup-claude.timer not loaded"
            if argv[:3] == ["systemctl", "--user", "is-system-running"]:
                return 0, "running"
            return 0, ""

        ok, note = self._run(runner)
        self.assertTrue(ok)
        self.assertEqual(note, "已预约 14:30:22")
        timer = "agents-quota-ping-followup-claude.timer"
        service = "agents-quota-ping-followup-claude.service"
        self.assertEqual(calls[0][0], ["systemctl", "--user", "is-system-running"])
        self.assertEqual(calls[1][0], ["systemctl", "--user", "stop", timer, service])
        self.assertEqual(calls[2][0], ["systemctl", "--user", "reset-failed", timer, service])
        self.assertEqual(
            calls[3][0],
            [
                "systemd-run",
                "--user",
                "--unit=agents-quota-ping-followup-claude",
                "--on-calendar=2026-11-17 14:30:22",
                "--timer-property=AccuracySec=1s",
                "--description=agents-quota-ping follow-up for claude",
                "--",
                "/usr/local/bin/quota-ping",
                "--only",
                "claude",
                "--no-jitter",
            ],
        )
        self.assertNotIn("--delay-seconds", calls[3][0])
        self.assertNotIn("--setenv=PYTHONPATH", " ".join(calls[3][0]))
        for _argv, env in calls:
            self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/1000")
            self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/run/user/1000/bus")

    def test_preserves_existing_user_bus_env(self) -> None:
        seen: dict[str, str] = {}

        def runner(_argv, env):
            seen.update(env)
            if _argv[:3] == ["systemctl", "--user", "is-system-running"]:
                return 0, "running"
            return 0, ""

        with (
            patch.dict(
                os.environ,
                {"XDG_RUNTIME_DIR": "/tmp/runtime-dir", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/runtime-dir/bus"},
                clear=True,
            ),
            patch("agents_quota_ping.scheduler.shutil.which", return_value="/usr/local/bin/quota-ping"),
        ):
            ok, _note = schedule_followup("claude", self.target, self.cfg, cmd_runner=runner, platform="linux")
        self.assertTrue(ok)
        self.assertEqual(seen["XDG_RUNTIME_DIR"], "/tmp/runtime-dir")
        self.assertEqual(seen["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/tmp/runtime-dir/bus")

    def test_module_fallback_sets_pythonpath_for_src_layout(self) -> None:
        from agents_quota_ping import scheduler as scheduler_mod

        calls: list[list[str]] = []

        def runner(argv, _env):
            calls.append(list(argv))
            if argv[:3] == ["systemctl", "--user", "is-system-running"]:
                return 0, "running"
            return 0, ""

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("agents_quota_ping.scheduler.os.getuid", return_value=1000, create=True),
            patch("agents_quota_ping.scheduler.shutil.which", return_value=None),
            patch("agents_quota_ping.scheduler.sys.argv", ["python"]),
        ):
            ok, note = schedule_followup("claude", self.target, self.cfg, cmd_runner=runner, platform="linux")
        self.assertTrue(ok)
        self.assertEqual(note, "已预约 14:30:22")
        run = next(argv for argv in calls if argv[0] == "systemd-run")
        src = str(Path(scheduler_mod.__file__).resolve().parent.parent)
        self.assertIn(f"--setenv=PYTHONPATH={src}", run)
        self.assertEqual(
            run[run.index("--") + 1 :],
            [sys.executable, "-m", "agents_quota_ping", "--only", "claude", "--no-jitter"],
        )

    def test_argv0_quota_ping_script_is_used_directly(self) -> None:
        script = self.root / "quota-ping"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        calls: list[list[str]] = []

        def runner(argv, _env):
            calls.append(list(argv))
            if argv[:3] == ["systemctl", "--user", "is-system-running"]:
                return 0, "running"
            return 0, ""

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("agents_quota_ping.scheduler.os.getuid", return_value=1000, create=True),
            patch("agents_quota_ping.scheduler.shutil.which", return_value=None),
            patch("agents_quota_ping.scheduler.sys.argv", [str(script)]),
        ):
            ok, _note = schedule_followup("codex", self.target, self.cfg, cmd_runner=runner, platform="linux")
        self.assertTrue(ok)
        run = next(argv for argv in calls if argv[0] == "systemd-run")
        self.assertEqual(run[run.index("--") + 1 :], [str(script), "--only", "codex", "--no-jitter"])
        self.assertFalse(any(arg.startswith("--setenv=PYTHONPATH=") for arg in run))

    def test_degraded_user_manager_still_schedules(self) -> None:
        def runner(argv, _env):
            if argv[:3] == ["systemctl", "--user", "is-system-running"]:
                return 1, "degraded"
            return 0, ""

        ok, note = self._run(runner)
        self.assertTrue(ok)
        self.assertEqual(note, "已预约 14:30:22")

    def test_unreachable_or_failed_systemd_does_not_raise(self) -> None:
        def missing(_argv, _env):
            return 127, "systemctl: not found"

        ok, note = self._run(missing)
        self.assertFalse(ok)
        self.assertEqual(note, "systemd --user 不可用")

        def bus_down(_argv, _env):
            return 1, "Failed to connect to bus: No such file or directory"

        ok, note = self._run(bus_down)
        self.assertFalse(ok)
        self.assertEqual(note, "systemd --user 不可用")

        def run_fails(argv, _env):
            if argv[0] == "systemd-run":
                return 1, "Failed to start transient timer unit"
            if argv[:3] == ["systemctl", "--user", "is-system-running"]:
                return 0, "running"
            return 0, ""

        ok, note = self._run(run_fails)
        self.assertFalse(ok)
        self.assertEqual(note, "systemd-run 失败")


class DarwinSchedulerTests(_SchedulerCase):
    def test_launchd_plist_uses_floor_minute_and_delay_seconds(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, _env):
            calls.append(list(argv))
            if argv[1] == "unload":
                return 1, "not loaded"
            return 0, ""

        with patch("agents_quota_ping.scheduler.shutil.which", return_value="/usr/local/bin/quota-ping"):
            ok, note = schedule_followup(
                "codex",
                self.target,
                self.cfg,
                cmd_runner=runner,
                platform="darwin",
                home=self.root,
            )
        self.assertTrue(ok)
        self.assertEqual(note, "已预约 14:30:22")
        self.assertEqual(floor_minute(self.target), datetime(2026, 11, 17, 14, 30, 0))
        plist_path = self.root / "Library" / "LaunchAgents" / "com.agents-quota-ping.followup.codex.plist"
        data = plistlib.loads(plist_path.read_bytes())
        self.assertEqual(data["Label"], "com.agents-quota-ping.followup.codex")
        self.assertEqual(
            data["ProgramArguments"],
            ["/usr/local/bin/quota-ping", "--only", "codex", "--no-jitter", "--delay-seconds", "22"],
        )
        self.assertEqual(data["StartCalendarInterval"], {"Month": 11, "Day": 17, "Hour": 14, "Minute": 30})
        self.assertFalse(data["RunAtLoad"])
        self.assertNotIn("EnvironmentVariables", data)
        self.assertEqual(calls[0][:2], ["launchctl", "unload"])
        self.assertEqual(calls[1][:3], ["launchctl", "load", "-w"])
        self.assertEqual(calls[0][2], calls[1][3])
        self.assertTrue(str(calls[0][2]).endswith("com.agents-quota-ping.followup.codex.plist"))

    def test_src_layout_plist_exports_pythonpath(self) -> None:
        from agents_quota_ping import scheduler as scheduler_mod

        def runner(argv, _env):
            return (1, "not loaded") if argv[1] == "unload" else (0, "")

        with (
            patch("agents_quota_ping.scheduler.shutil.which", return_value=None),
            patch("agents_quota_ping.scheduler.sys.argv", ["python"]),
        ):
            ok, _note = schedule_followup(
                "agy",
                self.target,
                self.cfg,
                cmd_runner=runner,
                platform="darwin",
                home=self.root,
            )
        self.assertTrue(ok)
        plist_path = self.root / "Library" / "LaunchAgents" / "com.agents-quota-ping.followup.agy.plist"
        data = plistlib.loads(plist_path.read_bytes())
        src = str(Path(scheduler_mod.__file__).resolve().parent.parent)
        self.assertEqual(data["EnvironmentVariables"], {"PYTHONPATH": src})
        self.assertEqual(data["ProgramArguments"][:3], [sys.executable, "-m", "agents_quota_ping"])
        self.assertEqual(
            data["ProgramArguments"][3:],
            ["--only", "agy", "--no-jitter", "--delay-seconds", "22"],
        )

    def test_launchctl_load_failure_is_reported(self) -> None:
        def runner(argv, _env):
            if argv[1] == "load":
                return 1, "Load failed"
            return 0, ""

        with patch("agents_quota_ping.scheduler.shutil.which", return_value="/usr/local/bin/quota-ping"):
            ok, note = schedule_followup(
                "codex",
                self.target,
                self.cfg,
                cmd_runner=runner,
                platform="darwin",
                home=self.root,
            )
        self.assertFalse(ok)
        self.assertEqual(note, "launchctl load 失败")


class WindowsSchedulerTests(_SchedulerCase):
    def test_register_scheduled_task_is_locale_independent(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, _env):
            calls.append(list(argv))
            return 0, ""

        with patch("agents_quota_ping.scheduler.shutil.which", return_value=r"C:\Tools\quota-ping.exe"):
            ok, note = schedule_followup("agy", self.target, self.cfg, cmd_runner=runner, platform="win32")
        self.assertTrue(ok)
        self.assertEqual(note, "已预约 14:30:22")
        self.assertEqual(calls[0][:4], ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"])
        script = calls[0][4]
        self.assertIn('Register-ScheduledTask -TaskName "AgentsQuotaPing-Followup-agy"', script)
        self.assertIn("-Force", script)
        self.assertIn(
            "([DateTime]::ParseExact('2026-11-17T14:30:22','yyyy-MM-ddTHH:mm:ss',$null))",
            script,
        )
        self.assertIn("New-ScheduledTaskTrigger -Once -At ", script)
        self.assertIn("--only", script)
        self.assertIn("agy", script)
        self.assertIn("--no-jitter", script)
        self.assertNotIn("--delay-seconds", script)
        self.assertIn(r"C:\Tools\quota-ping.exe", script)

    def test_src_layout_sets_pythonpath_inside_the_task(self) -> None:
        from agents_quota_ping import scheduler as scheduler_mod

        calls: list[str] = []

        def runner(argv, _env):
            calls.append(argv[4])
            return 0, ""

        with (
            patch("agents_quota_ping.scheduler.shutil.which", return_value=None),
            patch("agents_quota_ping.scheduler.sys.argv", ["python"]),
        ):
            ok, _note = schedule_followup("claude", self.target, self.cfg, cmd_runner=runner, platform="win32")
        self.assertTrue(ok)
        src = str(Path(scheduler_mod.__file__).resolve().parent.parent)
        self.assertIn("/c ", calls[0])
        self.assertIn("PYTHONPATH", calls[0])
        self.assertIn(src, calls[0])
        self.assertIn("agents_quota_ping", calls[0])
        self.assertIn(
            "([DateTime]::ParseExact('2026-11-17T14:30:22','yyyy-MM-ddTHH:mm:ss',$null))",
            calls[0],
        )

    def test_task_registration_failure_is_reported(self) -> None:
        def runner(_argv, _env):
            return 1, "Access is denied"

        with patch("agents_quota_ping.scheduler.shutil.which", return_value=r"C:\Tools\quota-ping.exe"):
            ok, note = schedule_followup("agy", self.target, self.cfg, cmd_runner=runner, platform="win32")
        self.assertFalse(ok)
        self.assertEqual(note, "计划任务注册失败")


class AttachFollowupTests(_SchedulerCase):
    def test_notes_are_concise_and_failures_do_not_ping_the_os(self) -> None:
        def boom(_argv, _env):
            raise AssertionError("OS scheduler should not be called")

        now = _at(8, 0)
        far = PingResult("Codex", "ok", 1, "gpt-6-luna", 1.0, quota=_snap(now + timedelta(hours=6)))
        skipped = attach_followup("codex", far, self.cfg, now_local=now, cmd_runner=boom, platform="linux")
        self.assertEqual(skipped.followup_note, ">5.5h 不预约")

        late_now = _at(14, 0)
        late = PingResult("Codex", "skipped", 0, "preflight-skip", 0.0, quota=_snap(_at(18, 40)))
        cutoff = attach_followup("codex", late, self.cfg, now_local=late_now, cmd_runner=boom, platform="linux")
        self.assertEqual(cutoff.followup_note, ">18:30 不预约")

        failed = PingResult("Codex", "fail", 1, "gpt-6-luna", 1.0, quota=_snap(now + timedelta(hours=2)))
        untouched = attach_followup("codex", failed, self.cfg, now_local=now, cmd_runner=boom, platform="linux")
        self.assertEqual(untouched.followup_note, "")

        disabled = attach_followup(
            "codex",
            far,
            replace(self.cfg, followup_enabled=False),
            enabled=True,
            now_local=now,
            cmd_runner=boom,
            platform="linux",
        )
        self.assertEqual(disabled.followup_note, "")
        off = attach_followup("codex", far, self.cfg, enabled=False, now_local=now, cmd_runner=boom, platform="linux")
        self.assertEqual(off.followup_note, "")

    def test_successful_attach_records_scheduled_note(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, _env):
            calls.append(list(argv))
            if argv[:3] == ["systemctl", "--user", "is-system-running"]:
                return 0, "running"
            return 0, ""

        result = PingResult("Claude", "ok", 1, "haiku", 1.0, quota=_snap(_at(14, 30)))
        with (
            patch("agents_quota_ping.scheduler.random.randint", return_value=22) as rand,
            patch("agents_quota_ping.scheduler.shutil.which", return_value="/usr/local/bin/quota-ping"),
            patch("agents_quota_ping.scheduler.os.getuid", return_value=1000, create=True),
            patch.dict(os.environ, {}, clear=True),
        ):
            updated = attach_followup(
                "claude",
                result,
                replace(self.cfg, followup_jitter_min=20, followup_jitter_max=40),
                now_local=_at(12, 0),
                cmd_runner=runner,
                platform="linux",
            )
        self.assertEqual(updated.followup_note, "已预约 14:30:22")
        rand.assert_called_once_with(20, 40)
        self.assertTrue(any(argv[0] == "systemd-run" for argv in calls))


class LastPingDedupTests(_SchedulerCase):
    def test_recent_ping_window_is_fifteen_minutes(self) -> None:
        now = datetime(2026, 11, 17, 1, 0, tzinfo=timezone.utc)
        snap = QuotaSnapshot(
            "Codex",
            reset_at=now + timedelta(hours=5),
            used_percent=0,
            remaining_percent=100,
        )
        last = now - timedelta(minutes=15)
        reason = should_skip_preflight(snap, now_utc=now, last_ping_at=last)
        self.assertEqual(reason, f"刚刚已触发过 ({last.astimezone():%H:%M:%S})")
        self.assertIsNone(
            should_skip_preflight(snap, now_utc=now, last_ping_at=now - timedelta(minutes=15, seconds=1))
        )
        self.assertIsNone(should_skip_preflight(snap, now_utc=now, last_ping_at=now + timedelta(seconds=5)))

    def test_receipt_skips_a_second_ping_until_force_and_is_per_provider(self) -> None:
        calls: list[int] = []

        def ok_run(_argv, _env, _cwd, _timeout):
            calls.append(1)
            return 0, "OK", "", False

        snap = QuotaSnapshot(
            "Codex",
            reset_at=datetime.now(timezone.utc) + timedelta(hours=4),
            used_percent=0,
            remaining_percent=100,
        )
        first = run_single_provider("codex", self.cfg, {}, run_cmd=ok_run, quota_probe=lambda _n, _c: snap)
        self.assertEqual(first.status, "ok")
        receipt = json.loads((self.cfg.state_dir / "last-ping-codex.json").read_text(encoding="utf-8"))
        self.assertTrue(str(receipt["last_ping_at"]).endswith("+00:00"))
        self.assertFalse((self.cfg.state_dir / "last-ping-claude.json").exists())

        second = run_single_provider("codex", self.cfg, {}, run_cmd=ok_run, quota_probe=lambda _n, _c: snap)
        self.assertEqual(second.status, "skipped")
        self.assertIn("刚刚已触发过", second.detail)
        self.assertEqual(calls, [1])

        forced = run_single_provider(
            "codex",
            self.cfg,
            {},
            run_cmd=ok_run,
            quota_probe=lambda _n, _c: snap,
            preflight=False,
        )
        self.assertEqual(forced.status, "ok")
        self.assertEqual(len(calls), 2)

    def test_old_or_corrupt_receipt_does_not_block_an_unused_window(self) -> None:
        path = self.cfg.state_dir / "last-ping-codex.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        old = (datetime.now(timezone.utc) - timedelta(minutes=16)).replace(microsecond=0)
        path.write_text(json.dumps({"last_ping_at": old.isoformat()}), encoding="utf-8")
        snap = QuotaSnapshot(
            "Codex",
            reset_at=datetime.now(timezone.utc) + timedelta(hours=4),
            used_percent=0,
            remaining_percent=100,
        )
        calls: list[int] = []

        def ok_run(_argv, _env, _cwd, _timeout):
            calls.append(1)
            return 0, "OK", "", False

        res = run_single_provider("codex", self.cfg, {}, run_cmd=ok_run, quota_probe=lambda _n, _c: snap)
        self.assertEqual(res.status, "ok")
        self.assertEqual(calls, [1])

        path.write_text("{", encoding="utf-8")
        again = run_single_provider("codex", self.cfg, {}, run_cmd=ok_run, quota_probe=lambda _n, _c: snap)
        self.assertEqual(again.status, "ok")
        self.assertEqual(len(calls), 2)

    def test_failure_does_not_record_but_degraded_does(self) -> None:
        def auth(_argv, _env, _cwd, _timeout):
            return 1, "", "401 Unauthorized", False

        failed = run_single_provider(
            "claude",
            self.cfg,
            {},
            run_cmd=auth,
            quota_probe=lambda _n, _c: QuotaSnapshot("Claude"),
            preflight=False,
        )
        self.assertEqual(failed.status, "fail")
        self.assertFalse((self.cfg.state_dir / "last-ping-claude.json").exists())

        def degrade(argv, _env, _cwd, _timeout):
            if "haiku" in argv:
                return 1, "", "unknown model haiku", False
            return 0, "OK", "", False

        degraded = run_single_provider(
            "claude",
            self.cfg,
            {},
            run_cmd=degrade,
            quota_probe=lambda _n, _c: QuotaSnapshot("Claude"),
            preflight=False,
        )
        self.assertEqual(degraded.status, "degraded")
        self.assertTrue((self.cfg.state_dir / "last-ping-claude.json").is_file())

    def test_per_provider_locks_do_not_block_other_providers(self) -> None:
        started = threading.Event()
        release = threading.Event()
        holder: dict[str, PingResult] = {}
        errors: list[BaseException] = []

        def blocking_run(_argv, _env, _cwd, _timeout):
            started.set()
            release.wait(5)
            return 0, "OK", "", False

        def hold() -> None:
            try:
                holder["res"] = run_single_provider(
                    "codex",
                    self.cfg,
                    {},
                    run_cmd=blocking_run,
                    quota_probe=lambda _n, _c: QuotaSnapshot("Codex"),
                    preflight=False,
                )
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(started.wait(5))
        try:
            claude_calls: list[int] = []

            def claude_run(_argv, _env, _cwd, _timeout):
                claude_calls.append(1)
                return 0, "OK", "", False

            claude = run_single_provider(
                "claude",
                self.cfg,
                {},
                run_cmd=claude_run,
                quota_probe=lambda _n, _c: QuotaSnapshot("Claude"),
                preflight=False,
            )
            self.assertEqual(claude.status, "ok")
            self.assertEqual(claude_calls, [1])

            def forbidden(*_args):
                raise AssertionError("locked provider should not ping")

            blocked = run_single_provider(
                "codex",
                self.cfg,
                {},
                run_cmd=forbidden,
                quota_probe=lambda _n, _c: QuotaSnapshot("Codex"),
                preflight=False,
            )
            self.assertEqual(blocked.status, "skipped")
            self.assertIn("另一实例正在运行", blocked.detail)
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(holder["res"].status, "ok")


class ReportAndCliTests(unittest.TestCase):
    def test_serialize_shortens_followup_note(self) -> None:
        long_note = "已过当日截止 18:30，交由次日 07:00 定时"
        payload = _serialize_results(
            [PingResult("Claude", "ok", 1, "haiku", 1.0, followup_note=long_note)],
            datetime(2026, 11, 17, 7, 0),
            None,
        )
        self.assertEqual(payload["results"][0]["followup_note"], ">18:30 不预约")
        empty = _serialize_results(
            [PingResult("Claude", "ok", 1, "haiku", 1.0)],
            datetime(2026, 11, 17, 7, 0),
            None,
        )
        self.assertNotIn("followup_note", empty["results"][0])

    def test_parse_followup_flags(self) -> None:
        args = parse_args(["--no-followup", "--followup-cutoff", "19:15", "--delay-seconds", "12"])
        self.assertTrue(args.no_followup)
        self.assertEqual(args.followup_cutoff, "19:15")
        self.assertEqual(args.delay_seconds, 12)
        defaults = parse_args([])
        self.assertFalse(defaults.no_followup)
        self.assertEqual(defaults.delay_seconds, 0)
        self.assertEqual(defaults.followup_cutoff, "")

    def _env(self, root: Path) -> dict[str, str]:
        return {
            "HOME": str(root),
            "USERPROFILE": str(root),
            "PATH": os.environ.get("PATH", "/usr/bin"),
            "QUOTA_PING_STATE_DIR": str(root / "state"),
        }

    def test_main_wires_followup_json_delay_and_skips_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_path = root / "config.json"
            cfg_path.write_text("{}", encoding="utf-8")
            seen: list[dict[str, object]] = []

            def fake_run(_name, _cfg, _env, **_kwargs):
                return PingResult("Claude", "ok", 1, "haiku", 0.2)

            def fake_attach(_name, result, _cfg, **kwargs):
                seen.append(kwargs)
                return replace(result, followup_note="已预约 14:30:22")

            slept: list[int] = []
            env = self._env(root)
            with patch.dict(os.environ, env, clear=True):
                with (
                    patch("agents_quota_ping.runner.run_single_provider", side_effect=fake_run),
                    patch("agents_quota_ping.runner.attach_followup", side_effect=fake_attach),
                    patch("agents_quota_ping.runner.time.sleep", side_effect=lambda seconds: slept.append(seconds)),
                ):
                    buf = StringIO()
                    with redirect_stdout(buf):
                        rc = main(
                            [
                                "--no-jitter",
                                "--delay-seconds",
                                "4",
                                "--only",
                                "claude",
                                "--json",
                                "--no-notify",
                                "--followup-cutoff",
                                "19:00",
                                "--config",
                                str(cfg_path),
                            ]
                        )
                    self.assertEqual(rc, 0)
                    payload = json.loads(buf.getvalue())
                    self.assertEqual(payload["results"][0]["followup_note"], "已预约 14:30:22")
                    self.assertEqual(seen[-1]["cutoff_hhmm"], "19:00")
                    self.assertTrue(seen[-1]["enabled"])
                    self.assertIn(4, slept)

                    buf = StringIO()
                    with redirect_stdout(buf):
                        rc = main(
                            [
                                "--no-jitter",
                                "--no-notify",
                                "--no-followup",
                                "--only",
                                "claude",
                                "--json",
                                "--config",
                                str(cfg_path),
                            ]
                        )
                    self.assertEqual(rc, 0)
                    self.assertFalse(seen[-1]["enabled"])

            disabled = root / "disabled.json"
            disabled.write_text(json.dumps({"schedule": {"followup_enabled": False}}), encoding="utf-8")
            with patch.dict(os.environ, env, clear=True):
                with (
                    patch("agents_quota_ping.runner.run_single_provider", side_effect=fake_run),
                    patch("agents_quota_ping.runner.attach_followup", side_effect=fake_attach),
                ):
                    with redirect_stdout(StringIO()):
                        main(["--no-jitter", "--no-notify", "--only", "claude", "--json", "--config", str(disabled)])
            self.assertFalse(seen[-1]["enabled"])

            with patch.dict(os.environ, env, clear=True):
                with (
                    patch("agents_quota_ping.runner.attach_followup") as attach,
                    patch("agents_quota_ping.runner.run_single_provider") as run,
                ):
                    with redirect_stdout(StringIO()):
                        rc = main(["--dry-run", "--only", "claude", "--config", str(cfg_path)])
            self.assertEqual(rc, 0)
            self.assertFalse(attach.called)
            self.assertFalse(run.called)

            with patch.dict(os.environ, env, clear=True):
                with (
                    patch(
                        "agents_quota_ping.runner.probe_quota",
                        return_value=QuotaSnapshot("Claude", used_percent=1, remaining_percent=99),
                    ),
                    patch("agents_quota_ping.runner.attach_followup") as attach,
                ):
                    with redirect_stdout(StringIO()):
                        rc = main(["--status-only", "--only", "claude", "--json", "--config", str(cfg_path)])
            self.assertEqual(rc, 0)
            self.assertFalse(attach.called)
