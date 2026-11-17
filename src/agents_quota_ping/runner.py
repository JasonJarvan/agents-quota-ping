"""Main runner: CLI flags, cross-platform locking, parallel execution, and notifications."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import random
import shlex
import sys
import threading
import time
from typing import Iterator

from agents_quota_ping.config import AppConfig, base_cron_env, load_config
from agents_quota_ping.notify import format_report, send_notifications
from agents_quota_ping.providers import (
    PingResult,
    build_agy_tier1,
    build_claude_tiers,
    build_codex_tiers,
    run_single_provider,
)
from agents_quota_ping.quota import QuotaSnapshot, probe_quota

ALL_PROVIDERS = ("claude", "codex", "agy")
PROVIDER_DISPLAY = {"claude": "Claude", "codex": "Codex", "agy": "Antigravity"}
MAX_LOG_BYTES = 1024 * 1024


@contextmanager
def acquire_lock(lock_path: Path) -> Iterator[bool]:
    """Acquire a non-blocking cross-platform advisory file lock."""
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


def rotate_log_if_needed(log_path: Path, max_bytes: int = MAX_LOG_BYTES) -> None:
    """Keep the log bounded by rotating to `.1` once above max_bytes."""
    try:
        if log_path.exists() and log_path.stat().st_size > max_bytes:
            backup = log_path.with_suffix(log_path.suffix + ".1")
            log_path.replace(backup)
    except OSError:
        pass


def read_codex_profile(cfg: AppConfig) -> str | None:
    """Return active codex-auth profile name if available."""
    path = cfg.codex_home / "accounts" / "current_profile"
    try:
        name = path.read_text(encoding="utf-8").strip()
        return name or None
    except OSError:
        return None


def make_logger(log_path: Path, quiet_stdout: bool = False):
    """Create a thread-safe line logger writing timestamps to log_path and optionally stdout."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    rotate_log_if_needed(log_path)
    lock = threading.Lock()

    def log(msg: str) -> None:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
        with lock:
            if not quiet_stdout:
                print(line, flush=True)
            try:
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass

    return log


def _serialize_results(results: list[PingResult], finished_at: datetime, codex_profile: str | None) -> dict:
    items = []
    for r in results:
        entry: dict[str, object] = {
            "provider": r.provider,
            "status": r.status,
            "tier": r.tier,
            "model": r.model,
            "elapsed_sec": round(r.elapsed_sec, 2),
            "detail": r.detail,
        }
        if r.usage is not None:
            entry["usage"] = {
                "input_tokens": r.usage.input_tokens,
                "cached_tokens": r.usage.cached_tokens,
                "cache_create_tokens": r.usage.cache_create_tokens,
                "output_tokens": r.usage.output_tokens,
                "total_tokens": r.usage.total(),
                "cost_usd": r.usage.cost_usd,
            }
        if r.quota is not None:
            entry["quota"] = {
                "window": r.quota.window_label,
                "reset_at": r.quota.reset_at.astimezone().isoformat() if r.quota.reset_at else None,
                "used_percent": r.quota.used_percent,
                "remaining_percent": r.quota.remaining_percent,
                "secondary_label": r.quota.secondary_label or None,
                "secondary_used_percent": r.quota.secondary_used_percent,
                "error": r.quota.error or None,
            }
        items.append(entry)
    return {
        "finished_at": finished_at.astimezone().isoformat(),
        "codex_profile": codex_profile,
        "results": items,
    }


def _run_parallel_providers(selected: list[str], worker_fn) -> list[PingResult]:
    if len(selected) <= 1:
        return [worker_fn(name) for name in selected]
    with ThreadPoolExecutor(max_workers=len(selected)) as pool:
        futures = [pool.submit(worker_fn, name) for name in selected]
        return [fut.result() for fut in futures]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="quota-ping",
        description="Minimal-cost 5-hour quota window warmer for Claude Code, Codex, and Antigravity.",
    )
    parser.add_argument("--config", type=Path, default=None, help="Path to config.json override")
    parser.add_argument(
        "--only",
        default=",".join(ALL_PROVIDERS),
        help="Comma-separated providers to run (claude,codex,agy)",
    )
    parser.add_argument("--status-only", action="store_true", help="Query real reset times & usage APIs without sending prompts")
    parser.add_argument("--force", action="store_true", help="Bypass pre-flight checks and force sending pings")
    parser.add_argument(
        "--no-skip-active",
        action="store_true",
        help="Still ping when a 5h window is already active (only skips exhausted/cooldown)",
    )
    parser.add_argument("--json", action="store_true", help="Print structured JSON summary to stdout")
    parser.add_argument("--no-jitter", action="store_true", help="Skip startup random sleep")
    parser.add_argument("--min-jitter", type=int, default=10, help="Min jitter seconds (default: 10)")
    parser.add_argument("--max-jitter", type=int, default=25, help="Max jitter seconds (default: 25)")
    parser.add_argument("--timeout", type=int, default=90, help="Per-command timeout in seconds (default: 90)")
    parser.add_argument("--dry-run", action="store_true", help="Print planned commands without calling models")
    parser.add_argument("--no-notify", action="store_true", help="Skip Feishu/webhook notification")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    selected = [p.strip().lower() for p in args.only.split(",") if p.strip()]
    for p in selected:
        if p not in ALL_PROVIDERS:
            print(f"Unknown provider '{p}'. Expected one of: {', '.join(ALL_PROVIDERS)}", file=sys.stderr)
            return 2

    cfg = load_config(args.config)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.empty_workdir.mkdir(parents=True, exist_ok=True)
    codex_profile = read_codex_profile(cfg)

    if args.dry_run:
        print(f"Workdir: {cfg.empty_workdir}")
        print(f"Codex active profile: {codex_profile or '(unknown)'}")
        print(f"HTTP proxy: {cfg.http_proxy or '(none)'}")
        for name in selected:
            if name == "claude":
                for t in build_claude_tiers(cfg):
                    print(f"[claude Tier {t.tier} ({t.model})] {shlex.join(t.argv)}")
            elif name == "codex":
                for t in build_codex_tiers(cfg, cfg.empty_workdir):
                    print(f"[codex Tier {t.tier} ({t.model})] {shlex.join(t.argv)}")
            elif name == "agy":
                t = build_agy_tier1(cfg)
                print(f"[agy Tier {t.tier} ({t.model})] {shlex.join(t.argv)}")
        return 0

    if args.status_only:
        finished_at = datetime.now()

        def probe_one(name: str) -> PingResult:
            snap: QuotaSnapshot = probe_quota(name, cfg)
            status = "fail" if snap.error and snap.reset_at is None else "ok"
            return PingResult(
                provider=PROVIDER_DISPLAY[name],
                status=status,
                tier=0,
                model="status-only",
                elapsed_sec=0.0,
                detail=snap.error,
                quota=snap,
            )

        results = _run_parallel_providers(selected, probe_one)
        if args.json:
            print(json.dumps(_serialize_results(results, finished_at, codex_profile), indent=2, ensure_ascii=False))
        else:
            print(format_report(results, finished_at, codex_profile=codex_profile, status_only=True))
        return 0

    log = make_logger(cfg.log_file, quiet_stdout=args.json)
    with acquire_lock(cfg.lock_file) as acquired:
        if not acquired:
            log("Another quota-ping instance is already running; skipping.")
            return 0

        jitter_sec = 0
        if not args.no_jitter and args.max_jitter >= args.min_jitter and args.max_jitter > 0:
            jitter_sec = random.randint(max(0, args.min_jitter), args.max_jitter)
            if jitter_sec > 0:
                log(f"Sleep {jitter_sec}s jitter")
                time.sleep(jitter_sec)

        env = base_cron_env(cfg)
        results = _run_parallel_providers(
            selected,
            lambda name: run_single_provider(
                name,
                cfg,
                env,
                timeout_sec=args.timeout,
                log_fn=log,
                preflight=not args.force,
                skip_if_active=not args.no_skip_active,
            ),
        )

        finished_at = datetime.now()
        report = format_report(
            results,
            finished_at=finished_at,
            codex_profile=codex_profile,
            jitter_sec=jitter_sec,
        )
        if args.json:
            print(json.dumps(_serialize_results(results, finished_at, codex_profile), indent=2, ensure_ascii=False))

        if not args.no_notify:
            send_notifications(report, cfg, base_env=env, log_fn=log)

        return 1 if any(r.status == "fail" for r in results) else 0
