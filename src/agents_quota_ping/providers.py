"""Provider command definitions, pre-flight checks, JSON token parsing, and fallback."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable, Iterator

from agents_quota_ping.classify import (
    ErrorKind,
    classify_failure,
    extract_error_summary,
    pick_agy_pool1_models,
    pick_agy_pool2_models,
    pick_codex_fallback_models,
)
from agents_quota_ping.config import AppConfig, claude_env, prepare_command_argv, proxy_env
from agents_quota_ping.quota import QuotaSnapshot, parse_iso_utc, probe_quota


@dataclass(frozen=True)
class TokenUsage:
    """Token consumption metrics extracted from a CLI JSON/JSONL response."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_create_tokens: int = 0
    total_tokens: int | None = None
    cost_usd: float | None = None

    def total(self) -> int:
        if self.total_tokens is not None and self.total_tokens > 0:
            return self.total_tokens
        return self.input_tokens + self.cached_tokens + self.cache_create_tokens + self.output_tokens

    def format_short(self) -> str:
        """Format compact token breakdown (uncached vs cached) and optional USD cost."""
        fresh_in = self.input_tokens + self.cache_create_tokens
        if self.cached_tokens > 0:
            in_part = f"in {fresh_in}+cache {self.cached_tokens}"
        else:
            in_part = f"in {fresh_in}"
        base = f"{self.total()} tok ({in_part}/out {self.output_tokens})"
        if self.cost_usd is not None:
            return f"{base}, ~${self.cost_usd:.5f}"
        return base


@dataclass(frozen=True)
class CommandTier:
    """Single attempt tier for a provider."""

    tier: int
    model: str
    argv: list[str]
    note: str = ""


@dataclass(frozen=True)
class PingResult:
    """Outcome of pinging (or smart-skipping) a single provider."""

    provider: str
    status: str  # "ok" | "degraded" | "skipped" | "fail"
    tier: int
    model: str
    elapsed_sec: float
    detail: str = ""
    usage: TokenUsage | None = None
    quota: QuotaSnapshot | None = None
    followup_note: str = ""


# (returncode, stdout, stderr, timed_out)
RunCmd = Callable[[list[str], dict[str, str], Path, int], tuple[int | None, str, str, bool]]

# Suppress a second ping when a follow-up lands next to a fixed cron run.
_RECENT_PING = timedelta(minutes=15)


@contextmanager
def acquire_lock(lock_path: Path) -> Iterator[bool]:
    """Acquire a non-blocking cross-platform advisory file lock.

    Yields ``True`` when this process holds the lock. Yields ``False`` when
    another live process already holds it; callers should skip rather than wait.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("a+", encoding="utf-8")
    acquired = False
    try:
        if sys.platform == "win32":
            import msvcrt

            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                acquired = False
        else:
            import fcntl

            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                acquired = False
        yield acquired
    finally:
        if acquired:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def last_ping_path(cfg: AppConfig, name: str) -> Path:
    """Return the per-provider receipt path (``last-ping-<name>.json``)."""
    return cfg.state_dir / f"last-ping-{name}.json"


def load_last_ping_at(cfg: AppConfig, name: str) -> datetime | None:
    """Return the UTC time of the last real ping for ``name``, if the receipt is readable."""
    path = last_ping_path(cfg, name)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(raw, dict):
        return None
    stamp = raw.get("last_ping_at")
    return parse_iso_utc(stamp if isinstance(stamp, str) else None)


def record_last_ping(cfg: AppConfig, name: str, when: datetime | None = None) -> None:
    """Persist ``last_ping_at`` so a follow-up and a fixed cron do not double-ping."""
    moment = when or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    path = last_ping_path(cfg, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"last_ping_at": moment.astimezone(timezone.utc).isoformat(timespec="seconds")}
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def default_run_cmd(
    argv: list[str],
    env: dict[str, str],
    cwd: Path,
    timeout_sec: int,
) -> tuple[int | None, str, str, bool]:
    """Execute a CLI subprocess safely and return (rc, stdout, stderr, timed_out)."""
    prepared = prepare_command_argv(argv)
    try:
        proc = subprocess.run(
            prepared,
            env=env,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_sec,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or "", False
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return None, out, err or f"Timed out after {timeout_sec}s", True
    except OSError as exc:
        return 127, "", str(exc), False


def _parse_json_object(stdout: str) -> dict | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    continue
    return None


def parse_claude_json_usage(stdout: str) -> TokenUsage | None:
    """Extract token usage and cost from `claude --output-format json`."""
    obj = _parse_json_object(stdout)
    if not obj:
        return None
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return None
    cost = obj.get("total_cost_usd")
    return TokenUsage(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cached_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_create_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
    )


def parse_codex_jsonl_usage(stdout: str) -> TokenUsage | None:
    """Extract token usage from `codex exec --json` NDJSON turn.completed event."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict):
                return TokenUsage(
                    input_tokens=int(usage.get("input_tokens") or 0),
                    output_tokens=int(usage.get("output_tokens") or 0),
                    cached_tokens=int(usage.get("cached_input_tokens") or 0),
                )
    return None


def parse_agy_json_usage(stdout: str) -> TokenUsage | None:
    """Extract token usage from `agy --output-format json`."""
    obj = _parse_json_object(stdout)
    if not obj:
        return None
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        stats = obj.get("stats")
        if isinstance(stats, dict):
            usage = stats.get("usage") if isinstance(stats.get("usage"), dict) else stats
    if not isinstance(usage, dict):
        return None
    inp = int(usage.get("input_tokens") or usage.get("total_input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    cached = int(usage.get("cached_tokens") or usage.get("cache_read_tokens") or 0)
    total = usage.get("total_tokens")
    if not any((inp, out, cached, total)):
        return None
    total_int = int(total) if isinstance(total, (int, float)) else None
    fresh_inp = inp
    if cached > 0 and inp >= cached and (total_int is None or total_int == inp + out):
        fresh_inp = inp - cached
    return TokenUsage(
        input_tokens=fresh_inp,
        output_tokens=out,
        cached_tokens=cached,
        total_tokens=total_int,
    )


def _parse_usage_for(provider_key: str, stdout: str) -> TokenUsage | None:
    if provider_key == "claude":
        return parse_claude_json_usage(stdout)
    if provider_key == "codex":
        return parse_codex_jsonl_usage(stdout)
    if provider_key in ("agy", "agy-claude"):
        return parse_agy_json_usage(stdout)
    return None


def should_skip_preflight(
    snap: QuotaSnapshot,
    now_utc: datetime | None = None,
    skip_if_active: bool = True,
    last_ping_at: datetime | None = None,
) -> str | None:
    """Decide whether to skip pinging based on live quota API state; returns skip reason or None.

    A ping recorded within the last 15 minutes is skipped so a one-shot follow-up
    and the fixed cron do not both hit the provider. Codex ``used_percent`` can
    still round to 0% right after a real ping, which would otherwise look unused.
    ``--force`` bypasses this by turning preflight off.
    """
    now = datetime.now(timezone.utc) if now_utc is None else now_utc
    if last_ping_at is not None:
        ping_at = last_ping_at if last_ping_at.tzinfo is not None else last_ping_at.replace(tzinfo=timezone.utc)
        now_aware = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        age = now_aware.astimezone(timezone.utc) - ping_at.astimezone(timezone.utc)
        if timedelta(0) <= age <= _RECENT_PING:
            return f"刚刚已触发过 ({ping_at.astimezone():%H:%M:%S})"

    if snap.error and snap.reset_at is None and snap.remaining_percent is None:
        return None
    if snap.remaining_percent is not None and snap.remaining_percent <= 0:
        if snap.reset_at is not None:
            return f"配额已耗尽 (0% 剩余，等待 {snap.reset_at.astimezone():%H:%M:%S} 重置)"
        return "配额已耗尽 (0% 剩余)"

    if snap.reset_at is not None and snap.reset_at > now + timedelta(hours=5, minutes=30):
        return f"处于长周期冷却 (重置 {snap.reset_at.astimezone():%Y-%m-%d %H:%M})"

    # Only skip an active 5h window when used_percent > 0 (unused 0% windows often return a floating now+5h).
    if (
        skip_if_active
        and snap.used_percent is not None
        and snap.used_percent > 0
        and snap.reset_at is not None
        and snap.reset_at > now + timedelta(seconds=60)
    ):
        return f"5h 窗口已激活 (已用 {snap.used_percent}%，{snap.reset_at.astimezone():%H:%M:%S} 重置)"

    return None


def build_claude_tiers(cfg: AppConfig) -> list[CommandTier]:
    """Build Tier 1/2/3 command list for Claude Code."""
    return [
        CommandTier(
            tier=1,
            model="haiku",
            argv=[
                cfg.claude_bin,
                "-p",
                "OK",
                "--model",
                "haiku",
                "--effort",
                "low",
                "--tools",
                "",
                "--strict-mcp-config",
                "--disable-slash-commands",
                "--safe-mode",
                "--setting-sources",
                "user",
                "--system-prompt",
                "Reply OK only.",
                "--no-session-persistence",
                "--output-format",
                "json",
            ],
            note="cheapest model + isolated system prompt + JSON usage",
        ),
        CommandTier(
            tier=2,
            model="sonnet",
            argv=[
                cfg.claude_bin,
                "-p",
                "OK",
                "--model",
                "sonnet",
                "--effort",
                "low",
                "--tools",
                "",
                "--no-session-persistence",
                "--output-format",
                "json",
            ],
            note="fallback to sonnet with minimal flags",
        ),
        CommandTier(
            tier=3,
            model="default",
            argv=[
                cfg.claude_bin,
                "-p",
                "OK",
                "--no-session-persistence",
            ],
            note="last-resort default claude flags",
        ),
    ]


_CODEX_DISABLE_TOOLS = (
    "--disable",
    "apps",
    "--disable",
    "shell_tool",
    "--disable",
    "view_image",
    "--disable",
    "skill_search",
    "--disable",
    "computer_use",
    "--disable",
    "browser_use",
    "--disable",
    "sleep_tool",
)


def build_codex_tiers(cfg: AppConfig, workdir: Path) -> list[CommandTier]:
    """Build Tier 1/2/3 command list for Codex CLI."""
    models_cache = cfg.codex_home / "models_cache.json"
    tiers = [
        CommandTier(
            tier=1,
            model="gpt-6-luna",
            argv=[
                cfg.codex_bin,
                "exec",
                "-m",
                "gpt-6-luna",
                "-c",
                'model_reasoning_effort="low"',
                "-c",
                'model_verbosity="low"',
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                *_CODEX_DISABLE_TOOLS,
                "--json",
                "-C",
                str(workdir),
                "Reply OK",
            ],
            note="cheapest Luna model + config/hooks/MCP/non-essential tools disabled",
        ),
        CommandTier(
            tier=2,
            model="gpt-6-luna",
            argv=[
                cfg.codex_bin,
                "exec",
                "-m",
                "gpt-6-luna",
                "-c",
                'model_reasoning_effort="low"',
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--json",
                "-C",
                str(workdir),
                "Reply OK",
            ],
            note="gpt-6-luna without --disable feature flags",
        ),
    ]

    fallbacks = pick_codex_fallback_models(models_cache, exclude={"gpt-6-luna"})
    for model in fallbacks[:2]:
        tiers.append(
            CommandTier(
                tier=2,
                model=model,
                argv=[
                    cfg.codex_bin,
                    "exec",
                    "-m",
                    model,
                    "-c",
                    'model_reasoning_effort="low"',
                    "--ephemeral",
                    "--skip-git-repo-check",
                    "--sandbox",
                    "read-only",
                    "--json",
                    "-C",
                    str(workdir),
                    "Reply OK",
                ],
                note=f"fallback model from models_cache ({model})",
            )
        )

    tiers.append(
        CommandTier(
            tier=3,
            model="default",
            argv=[
                cfg.codex_bin,
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "-C",
                str(workdir),
                "Reply OK",
            ],
            note="last-resort default codex exec flags",
        )
    )
    return tiers


def _agy_minimal_settings_json() -> str:
    return json.dumps(
        {
            "allowNonWorkspaceAccess": False,
            "statusLine": {"enabled": False},
            "title": {"enabled": False},
        },
        separators=(",", ":"),
    )


def _agy_effort_args(model: str) -> list[str]:
    lower = model.lower()
    if lower.endswith(("-low", "-medium", "-high", "-xhigh")):
        return []
    return ["--effort", "low"]


def build_agy_tier1(cfg: AppConfig) -> CommandTier:
    """Build Tier 1 command for Antigravity Pool 1 (Flash + Hydrogen)."""
    return CommandTier(
        tier=1,
        model="gemini-3.8-flash-low",
        argv=[
            cfg.agy_bin,
            "--model",
            "gemini-3.8-flash-low",
            "--effort",
            "low",
            "--disable-slash-commands",
            "--mode",
            "plan",
            "--settings",
            _agy_minimal_settings_json(),
            "--output-format",
            "json",
            "-p",
            "Reply OK only. Do not use any tools.",
        ],
        note="Pool 1 cheapest model (Flash shares quota with Hydrogen)",
    )


def build_agy_claude_tier1(cfg: AppConfig) -> CommandTier:
    """Build Tier 1 command for Antigravity Pool 2 (Claude / GPT-OSS).

    ``gpt-oss-120b-medium`` shares the Claude/GPT 5h quota pool at ~0.0025% per ping
    (~400x cheaper than ``claude-sonnet-5-5-low``). Its slug already pins ``medium``
    so ``--effort`` must be omitted. Once retired (scheduled 2026-11-30), Tier 2
    automatically falls back to ``claude-sonnet-5-5-low``.
    """
    return CommandTier(
        tier=1,
        model="gpt-oss-120b-medium",
        argv=[
            cfg.agy_bin,
            "--model",
            "gpt-oss-120b-medium",
            "--disable-slash-commands",
            "--mode",
            "plan",
            "--settings",
            _agy_minimal_settings_json(),
            "--output-format",
            "json",
            "-p",
            "Reply OK only. Do not use any tools.",
        ],
        note="Pool 2 cheapest model (GPT-OSS shares quota with Claude; falls back to claude-sonnet-5-5-low)",
    )


def discover_agy_fallbacks(
    cfg: AppConfig,
    run_cmd: RunCmd,
    workdir: Path,
    env: dict[str, str],
) -> list[CommandTier]:
    """Query `agy models` dynamically to find Pool 1 fallback models."""
    rc, stdout, stderr, timed_out = run_cmd([cfg.agy_bin, "models"], env, workdir, 20)
    tiers: list[CommandTier] = []
    if rc == 0 and not timed_out:
        models = pick_agy_pool1_models(stdout + "\n" + stderr, exclude={"gemini-3.8-flash-low"})
        for model in models[:2]:
            tiers.append(
                CommandTier(
                    tier=2,
                    model=model,
                    argv=[
                        cfg.agy_bin,
                        "--model",
                        model,
                        *_agy_effort_args(model),
                        "--disable-slash-commands",
                        "--output-format",
                        "json",
                        "-p",
                        "Reply OK only.",
                    ],
                    note=f"Pool 1 fallback from agy models ({model})",
                )
            )
    tiers.append(
        CommandTier(
            tier=3,
            model="default",
            argv=[cfg.agy_bin, "-p", "Reply OK only."],
            note="last-resort default agy flags",
        )
    )
    return tiers


def discover_agy_claude_fallbacks(
    cfg: AppConfig,
    run_cmd: RunCmd,
    workdir: Path,
    env: dict[str, str],
) -> list[CommandTier]:
    """Query `agy models` dynamically to find Pool 2 (Claude/GPT) fallback models.

    Never appends bare ``default`` (which would silently hit Gemini Pool 1).
    """
    rc, stdout, stderr, timed_out = run_cmd([cfg.agy_bin, "models"], env, workdir, 20)
    models: list[str] = []
    if rc == 0 and not timed_out:
        models = pick_agy_pool2_models(stdout + "\n" + stderr, exclude={"gpt-oss-120b-medium"})
    if not models:
        models = ["claude-sonnet-5-5-low"]

    tiers: list[CommandTier] = []
    for model in models[:2]:
        tiers.append(
            CommandTier(
                tier=2,
                model=model,
                argv=[
                    cfg.agy_bin,
                    "--model",
                    model,
                    *_agy_effort_args(model),
                    "--disable-slash-commands",
                    "--mode",
                    "plan",
                    "--settings",
                    _agy_minimal_settings_json(),
                    "--output-format",
                    "json",
                    "-p",
                    "Reply OK only. Do not use any tools.",
                ],
                note=f"Pool 2 fallback from agy models ({model})",
            )
        )
    return tiers


def _combined_output(stdout: str, stderr: str) -> str:
    return "\n".join(part for part in (stderr.strip(), stdout.strip()) if part)


def execute_with_fallback(
    provider_label: str,
    provider_key: str,
    tier1: CommandTier,
    fallback_loader: Callable[[], list[CommandTier]],
    env: dict[str, str],
    workdir: Path,
    run_cmd: RunCmd = default_run_cmd,
    timeout_sec: int = 90,
    retry_delay_sec: float = 8.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    log_fn: Callable[[str], None] = lambda _msg: None,
) -> PingResult:
    """Run Tier 1, retry once if transient/unknown, and fall back only when safe."""
    started = time.monotonic()

    log_fn(f"→ {provider_label} [Tier 1: {tier1.model}]")
    rc, stdout, stderr, timed_out = run_cmd(tier1.argv, env, workdir, timeout_sec)
    if rc == 0:
        elapsed = time.monotonic() - started
        usage = _parse_usage_for(provider_key, stdout)
        tok_note = f" [{usage.format_short()}]" if usage else ""
        log_fn(f"✓ {provider_label} OK [Tier 1: {tier1.model}] ({elapsed:.1f}s){tok_note}")
        return PingResult(
            provider=provider_label,
            status="ok",
            tier=1,
            model=tier1.model,
            elapsed_sec=elapsed,
            usage=usage,
        )

    combined = _combined_output(stdout, stderr)
    kind = classify_failure(rc, combined, timed_out=timed_out)
    first_summary = extract_error_summary(combined)
    log_fn(f"✗ {provider_label} Tier 1 failed ({kind.value}): {first_summary}")

    if kind == ErrorKind.AUTH_OR_QUOTA:
        elapsed = time.monotonic() - started
        return PingResult(
            provider=provider_label,
            status="fail",
            tier=1,
            model=tier1.model,
            elapsed_sec=elapsed,
            detail=f"{kind.value}: {first_summary}",
        )

    if kind in (ErrorKind.TRANSIENT, ErrorKind.UNKNOWN):
        log_fn(f"↻ {provider_label} retrying Tier 1 after {retry_delay_sec:.0f}s...")
        sleep_fn(retry_delay_sec)
        rc, stdout, stderr, timed_out = run_cmd(tier1.argv, env, workdir, timeout_sec)
        if rc == 0:
            elapsed = time.monotonic() - started
            usage = _parse_usage_for(provider_key, stdout)
            tok_note = f" [{usage.format_short()}]" if usage else ""
            log_fn(f"✓ {provider_label} OK on retry [Tier 1: {tier1.model}] ({elapsed:.1f}s){tok_note}")
            return PingResult(
                provider=provider_label,
                status="ok",
                tier=1,
                model=tier1.model,
                elapsed_sec=elapsed,
                usage=usage,
            )

        combined = _combined_output(stdout, stderr)
        kind = classify_failure(rc, combined, timed_out=timed_out)
        first_summary = extract_error_summary(combined)
        log_fn(f"✗ {provider_label} Tier 1 retry failed ({kind.value}): {first_summary}")
        if kind in (ErrorKind.AUTH_OR_QUOTA, ErrorKind.TRANSIENT):
            elapsed = time.monotonic() - started
            return PingResult(
                provider=provider_label,
                status="fail",
                tier=1,
                model=tier1.model,
                elapsed_sec=elapsed,
                detail=f"{kind.value}: {first_summary}",
            )

    last_detail = first_summary
    for candidate in fallback_loader():
        log_fn(f"⚠ {provider_label} trying Tier {candidate.tier} ({candidate.model})...")
        rc, fb_out, fb_err, fb_timed_out = run_cmd(candidate.argv, env, workdir, timeout_sec)
        if rc == 0:
            elapsed = time.monotonic() - started
            usage = _parse_usage_for(provider_key, fb_out)
            detail = f"Tier 1({tier1.model}) 失败 [{first_summary}]，已降级 Tier {candidate.tier}({candidate.model})"
            log_fn(f"⚠ {provider_label} DEGRADED OK [Tier {candidate.tier}: {candidate.model}] ({elapsed:.1f}s)")
            return PingResult(
                provider=provider_label,
                status="degraded",
                tier=candidate.tier,
                model=candidate.model,
                elapsed_sec=elapsed,
                detail=detail,
                usage=usage,
            )

        fb_combined = _combined_output(fb_out, fb_err)
        fb_kind = classify_failure(rc, fb_combined, timed_out=fb_timed_out)
        last_detail = extract_error_summary(fb_combined)
        log_fn(f"✗ {provider_label} Tier {candidate.tier} ({candidate.model}) failed ({fb_kind.value}): {last_detail}")
        if fb_kind == ErrorKind.AUTH_OR_QUOTA:
            last_detail = f"{fb_kind.value}: {last_detail}"
            break

    elapsed = time.monotonic() - started
    return PingResult(
        provider=provider_label,
        status="fail",
        tier=1,
        model=tier1.model,
        elapsed_sec=elapsed,
        detail=f"Tier 1({first_summary}); 兜底亦失败({last_detail})",
    )


_PROVIDER_LABELS = {
    "claude": "Claude",
    "codex": "Codex",
    "agy": "Antigravity",
    "agy-claude": "Antigravity (Claude/GPT)",
}


def run_single_provider(
    name: str,
    cfg: AppConfig,
    base_env: dict[str, str],
    run_cmd: RunCmd = default_run_cmd,
    quota_probe: Callable[[str, AppConfig], QuotaSnapshot] = probe_quota,
    timeout_sec: int = 90,
    retry_delay_sec: float = 8.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    log_fn: Callable[[str], None] = lambda _msg: None,
    preflight: bool = True,
    skip_if_active: bool = True,
) -> PingResult:
    """Run one provider under its own lock, then record a receipt when a ping really ran.

    The lock file is ``state_dir/run-<provider>.lock``. A Claude follow-up therefore
    never skips or blocks Codex or Antigravity. ``ok`` and ``degraded`` write
    ``last-ping-<provider>.json``; skips and hard failures do not.
    """
    if name not in _PROVIDER_LABELS:
        raise ValueError(f"Unknown provider: {name}")
    provider_label = _PROVIDER_LABELS[name]

    with acquire_lock(cfg.state_dir / f"run-{name}.lock") as acquired:
        if not acquired:
            log_fn(f"⏭ {provider_label} skipped (另一实例正在运行)")
            return PingResult(
                provider=provider_label,
                status="skipped",
                tier=0,
                model="lock-skip",
                elapsed_sec=0.0,
                detail="另一实例正在运行",
            )
        result = _run_single_provider_locked(
            name,
            cfg,
            base_env,
            run_cmd=run_cmd,
            quota_probe=quota_probe,
            timeout_sec=timeout_sec,
            retry_delay_sec=retry_delay_sec,
            sleep_fn=sleep_fn,
            log_fn=log_fn,
            preflight=preflight,
            skip_if_active=skip_if_active,
        )
        if result.status in ("ok", "degraded"):
            try:
                record_last_ping(cfg, name)
            except OSError as exc:
                log_fn(f"ℹ {provider_label} 未能记录最近触发时间: {exc}")
        return result


def _run_single_provider_locked(
    name: str,
    cfg: AppConfig,
    base_env: dict[str, str],
    run_cmd: RunCmd = default_run_cmd,
    quota_probe: Callable[[str, AppConfig], QuotaSnapshot] = probe_quota,
    timeout_sec: int = 90,
    retry_delay_sec: float = 8.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    log_fn: Callable[[str], None] = lambda _msg: None,
    preflight: bool = True,
    skip_if_active: bool = True,
) -> PingResult:
    """Run pre-flight quota check, ping if needed, and measure post-ping reset time."""
    if name not in _PROVIDER_LABELS:
        raise ValueError(f"Unknown provider: {name}")
    provider_label = _PROVIDER_LABELS[name]

    if preflight:
        pre_snap = quota_probe(name, cfg)
        skip_reason = should_skip_preflight(
            pre_snap,
            skip_if_active=skip_if_active,
            last_ping_at=load_last_ping_at(cfg, name),
        )
        if skip_reason:
            log_fn(f"⏭ {provider_label} skipped ({skip_reason})")
            return PingResult(
                provider=provider_label,
                status="skipped",
                tier=0,
                model="preflight-skip",
                elapsed_sec=0.0,
                detail=skip_reason,
                quota=pre_snap,
            )

    if name == "claude":
        env = claude_env(cfg, base_env)
        tiers = build_claude_tiers(cfg)
        res = execute_with_fallback(
            "Claude",
            "claude",
            tiers[0],
            lambda: tiers[1:],
            env,
            cfg.empty_workdir,
            run_cmd=run_cmd,
            timeout_sec=timeout_sec,
            retry_delay_sec=retry_delay_sec,
            sleep_fn=sleep_fn,
            log_fn=log_fn,
        )
    elif name == "codex":
        env = proxy_env(cfg, base_env)
        tiers = build_codex_tiers(cfg, cfg.empty_workdir)
        res = execute_with_fallback(
            "Codex",
            "codex",
            tiers[0],
            lambda: tiers[1:],
            env,
            cfg.empty_workdir,
            run_cmd=run_cmd,
            timeout_sec=timeout_sec,
            retry_delay_sec=retry_delay_sec,
            sleep_fn=sleep_fn,
            log_fn=log_fn,
        )
    elif name == "agy":
        env = proxy_env(cfg, base_env)
        tier1 = build_agy_tier1(cfg)
        res = execute_with_fallback(
            provider_label,
            "agy",
            tier1,
            lambda: discover_agy_fallbacks(cfg, run_cmd, cfg.empty_workdir, env),
            env,
            cfg.empty_workdir,
            run_cmd=run_cmd,
            timeout_sec=timeout_sec,
            retry_delay_sec=retry_delay_sec,
            sleep_fn=sleep_fn,
            log_fn=log_fn,
        )
    else:
        env = proxy_env(cfg, base_env)
        tier1 = build_agy_claude_tier1(cfg)
        res = execute_with_fallback(
            provider_label,
            "agy-claude",
            tier1,
            lambda: discover_agy_claude_fallbacks(cfg, run_cmd, cfg.empty_workdir, env),
            env,
            cfg.empty_workdir,
            run_cmd=run_cmd,
            timeout_sec=timeout_sec,
            retry_delay_sec=retry_delay_sec,
            sleep_fn=sleep_fn,
            log_fn=log_fn,
        )

    snap = quota_probe(name, cfg)
    if snap.error:
        log_fn(f"  ℹ {res.provider} quota API unavailable: {snap.error}")
    else:
        log_fn(f"  ⏱ {res.provider} {snap.format_summary()}")
    return PingResult(
        provider=res.provider,
        status=res.status,
        tier=res.tier,
        model=res.model,
        elapsed_sec=res.elapsed_sec,
        detail=res.detail,
        usage=res.usage,
        quota=snap,
    )
