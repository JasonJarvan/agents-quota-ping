"""Provider command definitions, JSON token parsing, retry, and dynamic fallback."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import time
from typing import Callable

from agents_quota_ping.classify import (
    ErrorKind,
    classify_failure,
    extract_error_summary,
    pick_agy_pool1_models,
    pick_codex_fallback_models,
)
from agents_quota_ping.config import AppConfig, claude_env, prepare_command_argv, proxy_env
from agents_quota_ping.quota import QuotaSnapshot, probe_quota


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
        """Format compact token + optional USD summary."""
        inp = self.input_tokens + self.cached_tokens + self.cache_create_tokens
        base = f"{self.total()} tok (in {inp}/out {self.output_tokens})"
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
    """Outcome of pinging a single provider."""

    provider: str
    status: str  # "ok" | "degraded" | "fail"
    tier: int
    model: str
    elapsed_sec: float
    detail: str = ""
    usage: TokenUsage | None = None
    quota: QuotaSnapshot | None = None


# (returncode, stdout, stderr, timed_out)
RunCmd = Callable[[list[str], dict[str, str], Path, int], tuple[int | None, str, str, bool]]


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
    inp = usage.get("input_tokens") or usage.get("total_input_tokens") or 0
    out = usage.get("output_tokens") or 0
    cached = usage.get("cached_tokens") or 0
    total = usage.get("total_tokens")
    if not any((inp, out, cached, total)):
        return None
    return TokenUsage(
        input_tokens=int(inp),
        output_tokens=int(out),
        cached_tokens=int(cached),
        total_tokens=int(total) if isinstance(total, (int, float)) else None,
    )


def _parse_usage_for(provider_key: str, stdout: str) -> TokenUsage | None:
    if provider_key == "claude":
        return parse_claude_json_usage(stdout)
    if provider_key == "codex":
        return parse_codex_jsonl_usage(stdout)
    if provider_key == "agy":
        return parse_agy_json_usage(stdout)
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
                "--json",
                "-C",
                str(workdir),
                "Reply OK",
            ],
            note="cheapest Luna model + config/hooks/MCP ignored",
        )
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


def build_agy_tier1(cfg: AppConfig) -> CommandTier:
    """Build Tier 1 command for Antigravity Pool 1 (Flash + Hydrogen)."""
    minimal_settings = json.dumps(
        {
            "allowNonWorkspaceAccess": False,
            "statusLine": {"enabled": False},
            "title": {"enabled": False},
        },
        separators=(",", ":"),
    )
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
            minimal_settings,
            "--output-format",
            "json",
            "-p",
            "Reply OK only. Do not use any tools.",
        ],
        note="Pool 1 cheapest model (Flash shares quota with Hydrogen)",
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
                        "--effort",
                        "low",
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
) -> PingResult:
    """Dispatch execution + post-ping real quota API measurement for one provider."""
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
            "Antigravity",
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
        raise ValueError(f"Unknown provider: {name}")

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
