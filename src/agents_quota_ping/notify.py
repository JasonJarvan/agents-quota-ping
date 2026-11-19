"""Report formatting and notification delivery (Feishu lark-cli or webhook)."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import subprocess
from typing import Callable
import urllib.error
import urllib.request

from agents_quota_ping.classify import extract_error_summary
from agents_quota_ping.config import AppConfig, feishu_env, prepare_command_argv
from agents_quota_ping.providers import PingResult
from agents_quota_ping.scheduler import concise_followup_note


def should_notify(results: list[PingResult]) -> bool:
    """Return False when every provider in the run was skipped (or nothing ran)."""
    return bool(results) and not all(r.status == "skipped" for r in results)


def _format_reset_fallback(finished_at: datetime) -> str:
    est = finished_at + timedelta(hours=5)
    return f"预计重置 ~{est:%H:%M}"


def format_report(
    results: list[PingResult],
    finished_at: datetime,
    codex_profile: str | None = None,
    jitter_sec: int = 0,
    status_only: bool = False,
) -> str:
    """Format a concise Markdown report with measured reset times and token counts."""
    has_fail = any(r.status == "fail" for r in results)
    has_degraded = any(r.status == "degraded" for r in results)

    if status_only:
        header = f"📊 **AI 5h 配额实时状态** ({finished_at:%Y-%m-%d %H:%M:%S})"
    elif has_fail:
        header = f"❌ **AI 5h 额度唤醒部分/全部失败** ({finished_at:%Y-%m-%d %H:%M:%S})"
    elif has_degraded:
        header = f"⚠️ **AI 5h 额度唤醒完成（含降级）** ({finished_at:%Y-%m-%d %H:%M:%S})"
    else:
        header = f"✅ **AI 5h 额度唤醒完成** ({finished_at:%Y-%m-%d %H:%M:%S})"

    lines = [header]
    if not status_only:
        lines.append(f"触发耗时: jitter {jitter_sec}s · 完成 {finished_at:%H:%M:%S}")
    lines.append("")

    notes: list[str] = []
    for res in results:
        label = res.provider
        if res.provider == "Codex" and codex_profile:
            label = f"Codex ({codex_profile})"
        elif res.provider == "Antigravity":
            label = "Antigravity (Pool 1)"

        quota_str = ""
        if res.quota and res.quota.reset_at is not None:
            quota_str = res.quota.format_summary()
        elif res.quota and res.quota.error:
            quota_str = f"{_format_reset_fallback(finished_at)} (API: {res.quota.error})"
        elif not status_only and res.status not in ("fail", "skipped"):
            quota_str = _format_reset_fallback(finished_at)

        if status_only:
            lines.append(f"- **{label}**: {quota_str or '未知'}")
            continue

        if res.status == "ok":
            icon = "✓"
            state = f"Tier 1 `{res.model}` ({res.elapsed_sec:.1f}s)"
        elif res.status == "degraded":
            icon = "⚠"
            state = f"降级 Tier {res.tier} `{res.model}` ({res.elapsed_sec:.1f}s)"
        elif res.status == "skipped":
            icon = "⏭"
            state = f"跳过 ({res.detail})"
        else:
            icon = "✗"
            state = f"FAIL `{res.model}` ({res.elapsed_sec:.1f}s)"

        extras = []
        if res.usage is not None:
            extras.append(res.usage.format_short())
        if quota_str and res.status != "skipped":
            extras.append(quota_str)
        note = concise_followup_note(res.followup_note)
        if note:
            extras.append(note)
        suffix = f" · {' · '.join(extras)}" if extras else ""
        lines.append(f"- {icon} **{label}**: {state}{suffix}")

        if res.detail and res.status in ("degraded", "fail"):
            notes.append(f"- **{label}**: {res.detail}")

    if notes:
        lines.append("")
        lines.append("**异常/降级详情:**")
        lines.extend(notes)
        if has_degraded or has_fail:
            lines.append("请检查 CLI 登录态或配额状态，必要时更新配置。")

    return "\n".join(lines)


SenderFn = Callable[[list[str], dict[str, str], int], tuple[int, str]]
WebhookFn = Callable[[str, str, str], tuple[bool, str]]


def _default_send_cmd(argv: list[str], env: dict[str, str], timeout_sec: int) -> tuple[int, str]:
    prepared = prepare_command_argv(argv)
    try:
        proc = subprocess.run(
            prepared,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_sec,
        )
        return proc.returncode, proc.stdout or ""
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, str(exc)


def _default_webhook_post(url: str, message: str, http_proxy: str = "") -> tuple[bool, str]:
    payload = json.dumps(
        {
            "msg_type": "text",
            "content": {"text": message},
            "text": message,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "agents-quota-ping/0.2"},
        method="POST",
    )
    handlers: list[urllib.request.BaseHandler] = []
    if http_proxy:
        handlers.append(urllib.request.ProxyHandler({"http": http_proxy, "https": http_proxy}))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=15) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except (urllib.error.URLError, OSError) as exc:
        return False, str(exc)


def send_notifications(
    message: str,
    cfg: AppConfig,
    base_env: dict[str, str] | None = None,
    sender: SenderFn = _default_send_cmd,
    webhook_fn: WebhookFn = _default_webhook_post,
    log_fn: Callable[[str], None] = lambda _msg: None,
) -> bool:
    """Deliver notification via configured Feishu CLI target and/or webhook URL."""
    delivered = False
    configured_any = False

    target_flag = None
    target_val = None
    if cfg.feishu_user_id:
        target_flag, target_val = "--user-id", cfg.feishu_user_id
    elif cfg.feishu_chat_id:
        target_flag, target_val = "--chat-id", cfg.feishu_chat_id

    if target_flag and target_val:
        configured_any = True
        env = feishu_env(cfg, base_env)
        for identity in ("bot", "user"):
            argv = [
                cfg.lark_bin,
                "im",
                "+messages-send",
                "--as",
                identity,
                target_flag,
                target_val,
                "--markdown",
                message,
            ]
            rc, output = sender(argv, env, 30)
            if rc == 0:
                log_fn(f"✓ Feishu notification sent (--as {identity})")
                delivered = True
                break
            log_fn(f"✗ Feishu send (--as {identity}) failed: {extract_error_summary(output)}")

    if cfg.webhook_url:
        configured_any = True
        ok, info = webhook_fn(cfg.webhook_url, message, cfg.http_proxy)
        if ok:
            log_fn(f"✓ Webhook notification sent ({info})")
            delivered = True
        else:
            log_fn(f"✗ Webhook notification failed: {info}")

    if not configured_any:
        log_fn("ℹ No Feishu/webhook target configured; skipping remote notification.")
        return True

    return delivered
