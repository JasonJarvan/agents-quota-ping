"""Per-provider one-shot follow-up scheduling via the host OS.

The ping process never sleeps until ``reset_at``. It asks systemd, launchd, or
Task Scheduler to start a fresh ``--only <provider> --no-jitter`` run later.
A second registration for the same provider replaces the previous timer.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import plistlib
import random
import shutil
import subprocess
import sys
from typing import Callable

from agents_quota_ping.config import AppConfig
from agents_quota_ping.providers import PingResult
from agents_quota_ping.quota import QuotaSnapshot

# Longer than a 5h window plus the small cron offset, shorter than "next morning".
_FOLLOWUP_HORIZON = timedelta(hours=5, minutes=30)
_COOLDOWN_REASON = "冷却期 >5.5h，交由常规 07:00 定时兜底"

# argv, env -> (returncode, combined output)
CmdRunner = Callable[[list[str], dict[str, str]], tuple[int, str]]


def concise_followup_note(note: str) -> str:
    """Collapse a planner reason into a short report fragment.

    ``已预约 14:30:22``, ``>18:30 不预约``, and ``>5.5h 不预约`` pass through
    unchanged. The long cutoff sentence is shortened the same way.
    """
    if not note:
        return ""
    stripped = note.strip()
    if stripped.startswith("已预约") or stripped.startswith(">"):
        return stripped
    if "5.5h" in stripped:
        return ">5.5h 不预约"
    marker = "已过当日截止 "
    if marker in stripped:
        rest = stripped.split(marker, 1)[1]
        cutoff = rest.split("，", 1)[0].strip()
        if cutoff:
            return f">{cutoff} 不预约"
    return stripped


def floor_minute(moment: datetime) -> datetime:
    """Drop seconds and microseconds so a minute-granularity scheduler can compensate."""
    return moment.replace(second=0, microsecond=0)


def _parse_cutoff(cutoff_hhmm: str) -> tuple[int, int] | None:
    text = (cutoff_hhmm or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _as_aware(moment: datetime, assume: datetime.tzinfo) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=assume)
    return moment


def plan_followup(
    snap: QuotaSnapshot,
    now_local: datetime | None = None,
    cutoff_hhmm: str = "18:30",
    jitter_range: tuple[int, int] = (15, 45),
    ping_status: str = "ok",
) -> tuple[datetime | None, str]:
    """Decide whether to register a follow-up at ``reset_at`` plus a few seconds of jitter.

    Returns ``(target_local, "")`` when the OS should schedule that local time.
    Returns ``(None, reason)`` when a fixed 07:00 run should take over, or
    ``(None, "")`` when there is nothing useful to schedule.

    Boundary 1: only when ``0 < reset - now <= 5h30m``. A hard failure is never
    rescheduled. A pre-flight skip with no percent data is not rescheduled
    either; an exhausted window (``remaining_percent == 0``) or an already
    active window (``used_percent > 0``) still is, and so is a real ``ok`` /
    ``degraded`` ping inside the horizon.

    Boundary 2: ``target_local`` must fall on today's local date and at or
    before ``cutoff_hhmm`` (hour and minute only; seconds do not push it over).
    """
    if ping_status == "fail" or snap.reset_at is None:
        return None, ""

    if now_local is None:
        now_local = datetime.now().astimezone()
    elif now_local.tzinfo is None:
        now_local = now_local.astimezone()

    reset_utc = _as_aware(snap.reset_at, timezone.utc).astimezone(timezone.utc)
    delta = reset_utc - now_local.astimezone(timezone.utc)
    if delta > _FOLLOWUP_HORIZON:
        return None, _COOLDOWN_REASON
    if delta <= timedelta(0):
        return None, ""
    if ping_status == "skipped" and snap.used_percent is None and snap.remaining_percent is None:
        return None, ""

    parsed = _parse_cutoff(cutoff_hhmm)
    if parsed is None:
        return None, "截止时间无效"
    cutoff_h, cutoff_m = parsed

    j_min, j_max = int(jitter_range[0]), int(jitter_range[1])
    if j_min > j_max:
        j_min, j_max = j_max, j_min
    j_min = max(0, j_min)
    j_max = max(j_min, j_max)
    reset_local = reset_utc.astimezone(now_local.tzinfo)
    target_local = reset_local + timedelta(seconds=random.randint(j_min, j_max))
    same_day = target_local.date() == now_local.date()
    before_cutoff = (target_local.hour, target_local.minute) <= (cutoff_h, cutoff_m)
    if not (same_day and before_cutoff):
        return None, f"已过当日截止 {cutoff_hhmm}，交由次日 07:00 定时"
    return target_local, ""


def _src_pythonpath() -> str | None:
    """Return the ``src`` directory when this checkout is not installed."""
    src_dir = Path(__file__).resolve().parent.parent
    if src_dir.name == "src" and (src_dir / "agents_quota_ping" / "__init__.py").is_file():
        return str(src_dir)
    return None


def build_followup_argv(
    provider_key: str,
    extra_args: list[str] | None = None,
    search_path: str | None = None,
) -> tuple[list[str], str | None]:
    """Build the command that the OS timer will execute.

    Prefer a real ``quota-ping`` executable (``search_path``, ``PATH``, or ``sys.argv[0]``).
    Otherwise use ``python -m agents_quota_ping`` and, for a src-layout
    checkout, return the directory that must be on ``PYTHONPATH``.
    """
    tail = ["--only", provider_key, "--no-jitter", *(extra_args or [])]
    found = shutil.which("quota-ping", path=search_path) if search_path else shutil.which("quota-ping")
    if found:
        return [found, *tail], None
    argv0 = sys.argv[0] if sys.argv else ""
    candidate = Path(argv0)
    try:
        if candidate.name == "quota-ping" and candidate.is_file() and os.access(candidate, os.X_OK):
            return [str(candidate), *tail], None
    except OSError:
        pass
    return [sys.executable, "-m", "agents_quota_ping", *tail], _src_pythonpath()


def _valid_provider_key(provider_key: str) -> bool:
    return bool(provider_key) and all(ch.isalnum() or ch in "-_" for ch in provider_key)


def _default_cmd_runner(argv: list[str], env: dict[str, str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
            check=False,
        )
        return int(proc.returncode), proc.stdout or ""
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 127, str(exc)


def _invoke(cmd_runner: CmdRunner, argv: list[str], env: dict[str, str]) -> tuple[int, str]:
    try:
        rc, out = cmd_runner(argv, env)
    except Exception as exc:
        return 127, str(exc)
    try:
        return int(rc), out or ""
    except (TypeError, ValueError):
        return 1, out or ""


def _short_detail(detail: str) -> str:
    text = " ".join((detail or "").split())
    if len(text) > 180:
        return text[:179] + "…"
    return text


def _scheduled_note(target_local: datetime) -> str:
    return f"已预约 {target_local:%H:%M:%S}"


def _systemd_client_env() -> dict[str, str]:
    """Env for talking to the user bus from a sparse cron environment."""
    env = dict(os.environ)
    runtime = env.get("XDG_RUNTIME_DIR", "").strip()
    if not runtime:
        runtime = f"/run/user/{os.getuid()}"
        env["XDG_RUNTIME_DIR"] = runtime
    if not env.get("DBUS_SESSION_BUS_ADDRESS", "").strip():
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime}/bus"
    return env


def _systemd_user_reachable(rc: int, output: str) -> bool:
    text = (output or "").lower()
    if rc == 127 or "not found" in text or "no such file" in text or "failed to connect" in text:
        return False
    if rc == 0:
        return True
    return any(token in text for token in ("running", "degraded", "starting", "maintenance", "initializing"))


def _schedule_linux(
    provider_key: str,
    target_local: datetime,
    cmd_runner: CmdRunner,
    log_fn: Callable[[str], None],
    search_path: str | None = None,
) -> tuple[bool, str]:
    env = _systemd_client_env()
    rc, out = _invoke(cmd_runner, ["systemctl", "--user", "is-system-running"], env)
    if not _systemd_user_reachable(rc, out):
        log_fn(f"ℹ {provider_key} 未能预约跟进: {_short_detail(out) or 'systemd --user 不可用'}")
        return False, "systemd --user 不可用"

    unit = f"agents-quota-ping-followup-{provider_key}"
    units = [f"{unit}.timer", f"{unit}.service"]
    # Stop the previous one-shot so a new registration replaces it instead of stacking.
    _invoke(cmd_runner, ["systemctl", "--user", "stop", *units], env)
    _invoke(cmd_runner, ["systemctl", "--user", "reset-failed", *units], env)

    argv, pythonpath = build_followup_argv(provider_key, search_path=search_path)
    command = [
        "systemd-run",
        "--user",
        f"--unit={unit}",
        f"--on-calendar={target_local:%Y-%m-%d %H:%M:%S}",
        "--timer-property=AccuracySec=1s",
        f"--description=agents-quota-ping follow-up for {provider_key}",
    ]
    if pythonpath:
        command.append(f"--setenv=PYTHONPATH={pythonpath}")
    command.append("--")
    command.extend(argv)
    rc, out = _invoke(cmd_runner, command, env)
    if rc != 0:
        log_fn(f"ℹ {provider_key} 未能预约跟进: {_short_detail(out) or 'systemd-run 失败'}")
        return False, "systemd-run 失败"
    note = _scheduled_note(target_local)
    log_fn(f"⏱ {provider_key} {note}")
    return True, note


def _schedule_darwin(
    provider_key: str,
    target_local: datetime,
    cmd_runner: CmdRunner,
    log_fn: Callable[[str], None],
    home: Path,
    search_path: str | None = None,
) -> tuple[bool, str]:
    floored = floor_minute(target_local)
    rem_sec = max(0, int((target_local - floored).total_seconds()))
    argv, pythonpath = build_followup_argv(provider_key, ["--delay-seconds", str(rem_sec)], search_path=search_path)
    label = f"com.agents-quota-ping.followup.{provider_key}"
    payload: dict[str, object] = {
        "Label": label,
        "ProgramArguments": argv,
        "StartCalendarInterval": {
            "Month": floored.month,
            "Day": floored.day,
            "Hour": floored.hour,
            "Minute": floored.minute,
        },
        "RunAtLoad": False,
    }
    if pythonpath:
        payload["EnvironmentVariables"] = {"PYTHONPATH": pythonpath}
    plist_path = home / "Library" / "LaunchAgents" / f"{label}.plist"
    try:
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_bytes(plistlib.dumps(payload))
    except OSError as exc:
        log_fn(f"ℹ {provider_key} 未能预约跟进: {exc}")
        return False, "plist 写入失败"

    env = dict(os.environ)
    # unload is expected to fail the first time this label is registered.
    _invoke(cmd_runner, ["launchctl", "unload", str(plist_path)], env)
    rc, out = _invoke(cmd_runner, ["launchctl", "load", "-w", str(plist_path)], env)
    if rc != 0:
        log_fn(f"ℹ {provider_key} 未能预约跟进: {_short_detail(out) or 'launchctl load 失败'}")
        return False, "launchctl load 失败"
    note = _scheduled_note(target_local)
    log_fn(f"⏱ {provider_key} {note}")
    return True, note


def _ps_single(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _windows_script(provider_key: str, target_local: datetime, argv: list[str], pythonpath: str | None) -> str:
    task = f"AgentsQuotaPing-Followup-{provider_key}"
    stamp = f"{target_local:%Y-%m-%dT%H:%M:%S}"
    if pythonpath:
        execute = "cmd.exe"
        argument = f'/c set "PYTHONPATH={pythonpath}" && {subprocess.list2cmdline(argv)}'
    else:
        execute = argv[0]
        argument = subprocess.list2cmdline(argv[1:])
    action = (
        f"$action = New-ScheduledTaskAction -Execute {_ps_single(execute)} -Argument {_ps_single(argument)}"
    )
    trigger = (
        "$trigger = New-ScheduledTaskTrigger -Once -At "
        f"([DateTime]::ParseExact('{stamp}','yyyy-MM-ddTHH:mm:ss',$null))"
    )
    register = (
        f'Register-ScheduledTask -TaskName "{task}" -Action $action -Trigger $trigger -Force | Out-Null'
    )
    return "; ".join((action, trigger, register))


def _schedule_windows(
    provider_key: str,
    target_local: datetime,
    cmd_runner: CmdRunner,
    log_fn: Callable[[str], None],
    search_path: str | None = None,
) -> tuple[bool, str]:
    argv, pythonpath = build_followup_argv(provider_key, search_path=search_path)
    script = _windows_script(provider_key, target_local, argv, pythonpath)
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]
    rc, out = _invoke(cmd_runner, command, dict(os.environ))
    if rc != 0:
        log_fn(f"ℹ {provider_key} 未能预约跟进: {_short_detail(out) or '计划任务注册失败'}")
        return False, "计划任务注册失败"
    note = _scheduled_note(target_local)
    log_fn(f"⏱ {provider_key} {note}")
    return True, note


def schedule_followup(
    provider_key: str,
    target_local: datetime,
    cfg: AppConfig,
    cmd_runner: CmdRunner | None = None,
    log_fn: Callable[[str], None] = lambda _msg: None,
    *,
    platform: str | None = None,
    home: Path | None = None,
) -> tuple[bool, str]:
    """Register a one-shot OS timer. Failures return ``(False, short_reason)`` and do not raise.

    ``cfg`` is part of the public signature so callers can pass the same
    runtime object they use for the ping. Scheduling itself only needs the
    provider key and the local target time.
    """
    runner = cmd_runner or _default_cmd_runner
    # cfg travels with the caller; the timer itself is registered with the OS.
    _ = cfg.followup_cutoff
    if not _valid_provider_key(provider_key):
        return False, "无效的提供商"
    plat = sys.platform if platform is None else platform
    try:
        if plat == "linux":
            return _schedule_linux(provider_key, target_local, runner, log_fn, search_path=cfg.search_path)
        if plat == "darwin":
            return _schedule_darwin(
                provider_key,
                target_local,
                runner,
                log_fn,
                Path.home() if home is None else home,
                search_path=cfg.search_path,
            )
        if plat == "win32":
            return _schedule_windows(provider_key, target_local, runner, log_fn, search_path=cfg.search_path)
        return False, "不支持的平台"
    except Exception as exc:
        log_fn(f"ℹ {provider_key} 未能预约跟进: {exc}")
        return False, "预约失败"


def attach_followup(
    provider_key: str,
    result: PingResult,
    cfg: AppConfig,
    *,
    enabled: bool = True,
    cutoff_hhmm: str | None = None,
    now_local: datetime | None = None,
    cmd_runner: CmdRunner | None = None,
    log_fn: Callable[[str], None] = lambda _msg: None,
    platform: str | None = None,
    home: Path | None = None,
) -> PingResult:
    """Plan a follow-up for ``result`` and store a concise ``followup_note``.

    ``enabled`` combines ``followup_enabled`` with ``--no-followup``. OS
    failures are written onto the result; they never fail the ping itself.
    """
    if not enabled or not cfg.followup_enabled or result.quota is None:
        return result
    cutoff = (cutoff_hhmm or cfg.followup_cutoff or "18:30").strip() or "18:30"
    try:
        target, reason = plan_followup(
            result.quota,
            now_local=now_local,
            cutoff_hhmm=cutoff,
            jitter_range=(cfg.followup_jitter_min, cfg.followup_jitter_max),
            ping_status=result.status,
        )
        if target is None:
            note = concise_followup_note(reason)
            if not note:
                return result
            log_fn(f"ℹ {provider_key} {note}")
            return replace(result, followup_note=note)
        _ok, msg = schedule_followup(
            provider_key,
            target,
            cfg,
            cmd_runner=cmd_runner,
            log_fn=log_fn,
            platform=platform,
            home=home,
        )
        note = concise_followup_note(msg) or "预约失败"
        return replace(result, followup_note=note)
    except Exception as exc:
        log_fn(f"ℹ {provider_key} 未能预约跟进: {exc}")
        return replace(result, followup_note="预约失败")
