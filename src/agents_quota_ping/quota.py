"""Live quota and reset-time API probes for Claude, Codex, and Antigravity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

from agents_quota_ping.config import AppConfig, prepare_command_argv


@dataclass(frozen=True)
class QuotaSnapshot:
    """Measured quota window metadata returned by a provider API."""

    provider: str
    reset_at: datetime | None = None
    window_label: str = "5h"
    used_percent: int | None = None
    remaining_percent: int | None = None
    secondary_label: str = ""
    secondary_used_percent: int | None = None
    source: str = "api"
    error: str = ""

    def format_summary(self) -> str:
        """Render concise human-readable quota + reset time summary."""
        parts: list[str] = []
        if self.reset_at is not None:
            local_dt = self.reset_at.astimezone()
            parts.append(f"实测重置 {local_dt:%H:%M:%S}")
        if self.used_percent is not None:
            parts.append(f"{self.window_label}已用{self.used_percent}%")
        elif self.remaining_percent is not None:
            parts.append(f"{self.window_label}剩{self.remaining_percent}%")
        if self.secondary_used_percent is not None and self.secondary_label:
            parts.append(f"{self.secondary_label}已用{self.secondary_used_percent}%")
        return " · ".join(parts)


def parse_iso_utc(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp string into an aware datetime."""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def parse_claude_usage_payload(data: dict) -> QuotaSnapshot:
    """Parse response JSON from Anthropic /api/oauth/usage."""
    five = data.get("five_hour") if isinstance(data.get("five_hour"), dict) else {}
    seven = data.get("seven_day") if isinstance(data.get("seven_day"), dict) else {}

    reset_at = parse_iso_utc(five.get("resets_at"))
    util = five.get("utilization")
    used_pct = int(round(float(util))) if isinstance(util, (int, float)) else None
    rem_pct = max(0, 100 - used_pct) if used_pct is not None else None

    w_util = seven.get("utilization")
    w_pct = int(round(float(w_util))) if isinstance(w_util, (int, float)) else None

    return QuotaSnapshot(
        provider="Claude",
        reset_at=reset_at,
        window_label="5h",
        used_percent=used_pct,
        remaining_percent=rem_pct,
        secondary_label="7d" if w_pct is not None else "",
        secondary_used_percent=w_pct,
        source="api",
    )


def _window_label(seconds: object) -> str:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return "5h"
    if 14400 <= seconds <= 21600:
        return "5h"
    days = round(seconds / 86400)
    if days >= 1:
        return f"{days}d"
    hours = round(seconds / 3600)
    return f"{hours}h"


def parse_codex_usage_payload(data: dict) -> QuotaSnapshot:
    """Parse response JSON from ChatGPT Codex backend usage API."""
    rate = data.get("rate_limit") if isinstance(data.get("rate_limit"), dict) else {}
    primary = rate.get("primary_window") if isinstance(rate.get("primary_window"), dict) else {}
    secondary = rate.get("secondary_window") if isinstance(rate.get("secondary_window"), dict) else {}

    reset_epoch = primary.get("reset_at")
    reset_at = (
        datetime.fromtimestamp(float(reset_epoch), tz=timezone.utc)
        if isinstance(reset_epoch, (int, float))
        else None
    )
    used_raw = primary.get("used_percent")
    used_pct = int(round(float(used_raw))) if isinstance(used_raw, (int, float)) else None
    rem_pct = max(0, 100 - used_pct) if used_pct is not None else None

    sec_used_raw = secondary.get("used_percent")
    sec_pct = int(round(float(sec_used_raw))) if isinstance(sec_used_raw, (int, float)) else None
    sec_label = _window_label(secondary.get("limit_window_seconds")) if sec_pct is not None else ""

    return QuotaSnapshot(
        provider="Codex",
        reset_at=reset_at,
        window_label=_window_label(primary.get("limit_window_seconds")),
        used_percent=used_pct,
        remaining_percent=rem_pct,
        secondary_label=sec_label,
        secondary_used_percent=sec_pct,
        source="api",
    )


_AGY_POOL1_PREFERRED_KEYS = (
    "gemini-3.8-flash-tiered",
    "gemini-3.8-flash-low",
    "gemini-4-hydrogen-sonar",
    "gemini-4-hydrogen-low",
    "gemini-3-flash",
    "gemini-2.5-flash",
)

_AGY_POOL2_PREFERRED_KEYS = (
    "gpt-oss-120b-medium",
    "claude-sonnet-5-5-low",
    "claude-sonnet-5-5-medium",
    "claude-sonnet-5-5-high",
    "claude-opus-5-5-low",
    "claude-opus-5-5-medium",
    "claude-opus-5-5-high",
)


def _snapshot_from_agy_quota_info(provider: str, chosen: dict) -> QuotaSnapshot:
    reset_at = parse_iso_utc(chosen.get("resetTime"))
    rem_frac = chosen.get("remainingFraction")
    rem_pct = int(round(float(rem_frac) * 100)) if isinstance(rem_frac, (int, float)) else None
    used_pct = max(0, 100 - rem_pct) if rem_pct is not None else None
    return QuotaSnapshot(
        provider=provider,
        reset_at=reset_at,
        window_label="5h",
        used_percent=used_pct,
        remaining_percent=rem_pct,
        source="api",
    )


def parse_agy_models_payload(data: dict) -> QuotaSnapshot:
    """Parse Pool 1 (Flash/Hydrogen) quotaInfo from Antigravity fetchAvailableModels."""
    models = data.get("models") if isinstance(data.get("models"), dict) else {}
    chosen: dict | None = None

    for key in _AGY_POOL1_PREFERRED_KEYS:
        candidate = models.get(key)
        if isinstance(candidate, dict) and isinstance(candidate.get("quotaInfo"), dict):
            if candidate["quotaInfo"].get("resetTime"):
                chosen = candidate["quotaInfo"]
                break

    if chosen is None:
        for key, candidate in models.items():
            lower = str(key).lower()
            if any(bad in lower for bad in ("argon", "orchestrator", "claude", "gpt", "oss")):
                continue
            if ("flash" in lower or "hydrogen" in lower) and isinstance(candidate, dict):
                qi = candidate.get("quotaInfo")
                if isinstance(qi, dict) and qi.get("resetTime"):
                    chosen = qi
                    break

    if chosen is None:
        return QuotaSnapshot(provider="Antigravity", source="api", error="Pool 1 quotaInfo missing")

    return _snapshot_from_agy_quota_info("Antigravity", chosen)


def parse_agy_pool2_models_payload(data: dict) -> QuotaSnapshot:
    """Parse Pool 2 (Claude/GPT-OSS) quotaInfo from Antigravity fetchAvailableModels."""
    provider = "Antigravity (Claude/GPT)"
    models = data.get("models") if isinstance(data.get("models"), dict) else {}
    chosen: dict | None = None

    for key in _AGY_POOL2_PREFERRED_KEYS:
        candidate = models.get(key)
        if isinstance(candidate, dict) and isinstance(candidate.get("quotaInfo"), dict):
            if candidate["quotaInfo"].get("resetTime"):
                chosen = candidate["quotaInfo"]
                break

    if chosen is None:
        for key, candidate in models.items():
            lower = str(key).lower()
            if any(bad in lower for bad in ("gemini", "flash", "hydrogen", "argon", "neon", "carbon", "orchestrator")):
                continue
            if any(tok in lower for tok in ("claude", "gpt", "oss")) and isinstance(candidate, dict):
                qi = candidate.get("quotaInfo")
                if isinstance(qi, dict) and qi.get("resetTime"):
                    chosen = qi
                    break

    if chosen is None:
        return QuotaSnapshot(provider=provider, source="api", error="Pool 2 (Claude/GPT) quotaInfo missing")

    return _snapshot_from_agy_quota_info(provider, chosen)


def _open_url_json(
    url: str,
    headers: dict[str, str],
    http_proxy: str = "",
    method: str = "GET",
    body: bytes | None = None,
    timeout_sec: int = 15,
) -> dict:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    handlers: list[urllib.request.BaseHandler] = []
    if http_proxy:
        handlers.append(urllib.request.ProxyHandler({"http": http_proxy, "https": http_proxy}))
    opener = urllib.request.build_opener(*handlers)
    with opener.open(req, timeout=timeout_sec) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


def _read_claude_token(claude_dir: Path) -> str:
    cred_path = claude_dir / ".credentials.json"
    if cred_path.is_file():
        data = json.loads(cred_path.read_text(encoding="utf-8"))
        tok = (data.get("claudeAiOauth") or {}).get("accessToken")
        if tok:
            return str(tok)

    if sys.platform == "darwin":
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout.strip())
            tok = (data.get("claudeAiOauth") or {}).get("accessToken")
            if tok:
                return str(tok)
    return ""


_CLAUDE_GUARDED_FETCH_CODE = r"""
import json, os, sys, urllib.request
from pathlib import Path
cred = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
tok = (cred.get("claudeAiOauth") or {}).get("accessToken") or ""
if not tok:
    raise SystemExit("missing claude oauth token")
req = urllib.request.Request(
    "https://api.anthropic.com/api/oauth/usage",
    headers={
        "Authorization": f"Bearer {tok}",
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": "agents-quota-ping/0.2",
    },
)
with urllib.request.urlopen(req, timeout=15) as r:
    sys.stdout.write(r.read().decode("utf-8", "replace"))
"""


def probe_claude_quota(cfg: AppConfig) -> QuotaSnapshot:
    """Query Claude OAuth usage API for live 5-hour reset timestamp and utilization."""
    cred_path = cfg.claude_dir / ".credentials.json"
    try:
        if cfg.claude_guard_bin and Path(cfg.claude_guard_bin).is_file() and cred_path.is_file():
            argv = prepare_command_argv(
                [cfg.claude_guard_bin, sys.executable, "-c", _CLAUDE_GUARDED_FETCH_CODE, str(cred_path)]
            )
            env = {"HOME": str(Path.home()), "PATH": cfg.search_path}
            proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=25)
            if proc.returncode != 0:
                msg = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
                return QuotaSnapshot(provider="Claude", error=msg[:120])
            data = json.loads(proc.stdout)
            return parse_claude_usage_payload(data)

        token = _read_claude_token(cfg.claude_dir)
        if not token:
            return QuotaSnapshot(provider="Claude", error="no oauth credentials")
        proxy = "" if cfg.claude_clear_proxy_env else cfg.http_proxy
        data = _open_url_json(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "Accept": "application/json",
                "User-Agent": "agents-quota-ping/0.2",
            },
            http_proxy=proxy,
        )
        return parse_claude_usage_payload(data)
    except Exception as exc:  # noqa: BLE001 - best-effort telemetry probe
        return QuotaSnapshot(provider="Claude", error=str(exc)[:120])


def probe_codex_quota(cfg: AppConfig) -> QuotaSnapshot:
    """Query ChatGPT Codex usage API for live 5-hour reset timestamp and usage."""
    auth_path = cfg.codex_home / "auth.json"
    if not auth_path.is_file():
        return QuotaSnapshot(provider="Codex", error="missing codex auth.json")
    try:
        tokens = json.loads(auth_path.read_text(encoding="utf-8")).get("tokens") or {}
        access_token = tokens.get("access_token")
        account_id = tokens.get("account_id")
        if not access_token or not account_id:
            return QuotaSnapshot(provider="Codex", error="missing access_token/account_id")
        headers = {
            "Authorization": f"Bearer {access_token}",
            "ChatGPT-Account-Id": str(account_id),
            "Accept": "application/json",
            "User-Agent": "agents-quota-ping/0.2",
        }
        last_err = ""
        for url in (
            "https://chatgpt.com/backend-api/codex/usage",
            "https://chatgpt.com/backend-api/wham/usage",
        ):
            try:
                data = _open_url_json(url, headers=headers, http_proxy=cfg.http_proxy)
                return parse_codex_usage_payload(data)
            except urllib.error.HTTPError as exc:
                last_err = f"HTTP {exc.code}"
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
        return QuotaSnapshot(provider="Codex", error=last_err[:120])
    except Exception as exc:  # noqa: BLE001
        return QuotaSnapshot(provider="Codex", error=str(exc)[:120])


def _fetch_agy_models_payload(cfg: AppConfig) -> tuple[dict | None, str]:
    token_path = cfg.agy_data_dir / "antigravity-oauth-token"
    if not token_path.is_file():
        return None, "missing antigravity-oauth-token"
    try:
        token_data = json.loads(token_path.read_text(encoding="utf-8"))
        token_obj = token_data.get("token") if isinstance(token_data.get("token"), dict) else {}
        access_token = token_obj.get("access_token")
        if not access_token:
            return None, "missing access_token"

        proj_file = cfg.agy_data_dir / "cache" / "default_project_id.txt"
        proj = proj_file.read_text(encoding="utf-8").strip() if proj_file.is_file() else ""
        payload = json.dumps({"project": proj} if proj else {}).encode("utf-8")

        data = _open_url_json(
            "https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "User-Agent": "antigravity/1.5.9 linux/amd64",
            },
            http_proxy=cfg.http_proxy,
            method="POST",
            body=payload,
        )
        return data, ""
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)[:120]


def probe_agy_quota(cfg: AppConfig) -> QuotaSnapshot:
    """Query Antigravity Cloud Code API for Pool 1 live resetTime and remainingFraction."""
    data, err = _fetch_agy_models_payload(cfg)
    if err or data is None:
        return QuotaSnapshot(provider="Antigravity", error=err or "empty response")
    return parse_agy_models_payload(data)


def probe_agy_claude_quota(cfg: AppConfig) -> QuotaSnapshot:
    """Query Antigravity Cloud Code API for Pool 2 (Claude/GPT) live resetTime and remainingFraction."""
    data, err = _fetch_agy_models_payload(cfg)
    if err or data is None:
        return QuotaSnapshot(provider="Antigravity (Claude/GPT)", error=err or "empty response")
    return parse_agy_pool2_models_payload(data)


def probe_quota(provider_key: str, cfg: AppConfig) -> QuotaSnapshot:
    """Probe live quota window API for `claude`, `codex`, `agy`, or `agy-claude`."""
    if provider_key == "claude":
        return probe_claude_quota(cfg)
    if provider_key == "codex":
        return probe_codex_quota(cfg)
    if provider_key == "agy":
        return probe_agy_quota(cfg)
    if provider_key == "agy-claude":
        return probe_agy_claude_quota(cfg)
    raise ValueError(f"Unknown provider key: {provider_key}")
