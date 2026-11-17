"""Failure classification and dynamic cheapest fallback model selection."""

from __future__ import annotations

from enum import Enum
import json
from pathlib import Path
import re


class ErrorKind(str, Enum):
    """Category of a failed ping attempt."""

    MODEL_OR_FLAG = "model_or_flag"
    AUTH_OR_QUOTA = "auth_or_quota"
    TRANSIENT = "transient"
    UNKNOWN = "unknown"


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\r")

_MODEL_OR_FLAG_PATTERNS = (
    r"unknown model",
    r"model[_\s-]*not[_\s-]*found",
    r"model\b.*\bnot found",
    r"model\b.*\bdoes not exist",
    r"model\b.*\bnot available",
    r"model\b.*\bnot supported",
    r"unsupported model",
    r"invalid model",
    r"deprecated",
    r"retired",
    r"unknown option",
    r"unknown flag",
    r"unrecognized option",
    r"unrecognized flag",
    r"unexpected argument",
    r"flag provided but not defined",
    r"unknown argument",
    r"unknown feature",
    r"invalid feature",
    r"error: invalid value .* for '--model'",
)

_AUTH_OR_QUOTA_PATTERNS = (
    r"\b401\b",
    r"\bunauthorized\b",
    r"invalid_jwt",
    r"invalid[_\s-]*token",
    r"token expired",
    r"authentication required",
    r"not logged in",
    r"please log in",
    r"login required",
    r"run /login",
    r"re-authenticate",
    r"claude agent network cage unavailable",
    r"claude network preflight",
    r"\b429\b",
    r"resource_exhausted",
    r"rate[_\s-]*limit",
    r"too many requests",
    r"usage limit",
    r"quota exceeded",
    r"exhausted your capacity",
    r"credit balance is too low",
    r"insufficient_quota",
)

_TRANSIENT_PATTERNS = (
    r"timed out",
    r"\btimeout\b",
    r"connection refused",
    r"connection reset",
    r"unexpected eof",
    r"\beof\b",
    r"\bssl\b",
    r"\btls\b",
    r"econnreset",
    r"etimedout",
    r"econnrefused",
    r"network is unreachable",
    r"temporary failure in name resolution",
    r"could not resolve host",
    r"\b502\b",
    r"\b503\b",
    r"\b504\b",
    r"500 internal server error",
    r"service unavailable",
    r"overloaded",
    r"try again",
)


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences and carriage returns from CLI output."""
    return _ANSI_RE.sub("", text)


def classify_failure(returncode: int | None, output: str, timed_out: bool = False) -> ErrorKind:
    """Classify a failed CLI invocation to decide retry vs fallback vs abort."""
    if timed_out:
        return ErrorKind.TRANSIENT

    normalized = strip_ansi(output).lower()
    if any(re.search(pat, normalized) for pat in _MODEL_OR_FLAG_PATTERNS):
        return ErrorKind.MODEL_OR_FLAG
    if any(re.search(pat, normalized) for pat in _AUTH_OR_QUOTA_PATTERNS):
        return ErrorKind.AUTH_OR_QUOTA
    if any(re.search(pat, normalized) for pat in _TRANSIENT_PATTERNS):
        return ErrorKind.TRANSIENT
    return ErrorKind.UNKNOWN


def extract_error_summary(output: str, max_len: int = 180) -> str:
    """Extract the most informative short line from CLI stderr/stdout."""
    cleaned = strip_ansi(output)
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return "(no output)"

    preferred = [
        line
        for line in lines
        if any(tok in line.lower() for tok in ("error", "fail", "invalid", "unknown", "401", "429", "quota", "expired", "refus"))
    ]
    chosen = preferred[-1] if preferred else lines[-1]
    if len(chosen) > max_len:
        return chosen[: max_len - 1] + "…"
    return chosen


def _codex_model_rank(model: dict[str, object]) -> tuple[int, int, str]:
    slug = str(model.get("slug") or "")
    lower = slug.lower()
    desc = str(model.get("description") or "").lower()
    def_eff = str(model.get("default_reasoning_level") or "").lower()
    priority = model.get("priority")
    prio_num = int(priority) if isinstance(priority, int) else 999

    if any(tok in lower for tok in ("luna", "mini", "nano", "spark", "flash", "lite")):
        tier_rank = 0
    elif "affordable" in desc or "efficient" in desc or "fast" in desc:
        tier_rank = 1
    elif def_eff == "low":
        tier_rank = 2
    elif "astra" in lower:
        tier_rank = 4
    else:
        tier_rank = 3
    return (tier_rank, prio_num, lower)


def pick_codex_fallback_models(cache_path: Path, exclude: set[str] | None = None) -> list[str]:
    """Select cheapest visible fallback models from Codex models_cache.json."""
    excluded = exclude or set()
    if not cache_path.is_file():
        return []
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    raw_models = data.get("models", []) if isinstance(data, dict) else []
    visible = [
        m
        for m in raw_models
        if isinstance(m, dict)
        and m.get("visibility") == "list"
        and m.get("slug")
        and str(m.get("slug")) not in excluded
    ]
    visible.sort(key=_codex_model_rank)
    return [str(m["slug"]) for m in visible]


def _agy_pool1_rank(slug: str) -> tuple[int, str]:
    lower = slug.lower()
    if "flash" in lower:
        if lower.endswith("-low"):
            return (0, lower)
        if lower.endswith("-medium"):
            return (1, lower)
        return (2, lower)
    if "hydrogen" in lower:
        if lower.endswith("-low"):
            return (3, lower)
        if lower.endswith("-medium"):
            return (4, lower)
        return (5, lower)
    return (9, lower)


def pick_agy_pool1_models(models_output: str, exclude: set[str] | None = None) -> list[str]:
    """Parse `agy models` output and return only Pool 1 (Flash/Hydrogen) models."""
    excluded = exclude or set()
    cleaned = strip_ansi(models_output)
    candidates: list[str] = []
    for line in cleaned.splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.match(r"^([a-z0-9][a-z0-9._-]+)\s+", line)
        if not match:
            continue
        slug = match.group(1)
        lower = slug.lower()
        if slug in excluded:
            continue
        if any(bad in lower for bad in ("argon", "orchestrator", "claude", "gpt", "oss")):
            continue
        if "flash" in lower or "hydrogen" in lower:
            candidates.append(slug)
    candidates.sort(key=_agy_pool1_rank)
    return candidates
