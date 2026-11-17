"""Cross-platform configuration, binary discovery, and environment builders."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import sys


def default_config_dir(home: Path | None = None, platform: str | None = None) -> Path:
    """Return OS-appropriate user config directory for agents-quota-ping."""
    plat = sys.platform if platform is None else platform
    base_home = Path.home() if home is None else home
    if plat == "win32":
        appdata = os.environ.get("APPDATA")
        root = Path(appdata) if appdata else base_home / "AppData" / "Roaming"
        return root / "agents-quota-ping"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    root = Path(xdg) if xdg else base_home / ".config"
    return root / "agents-quota-ping"


def default_state_dir(home: Path | None = None, platform: str | None = None) -> Path:
    """Return OS-appropriate runtime state/log directory for agents-quota-ping."""
    env_override = os.environ.get("QUOTA_PING_STATE_DIR")
    if env_override:
        return Path(env_override)
    plat = sys.platform if platform is None else platform
    base_home = Path.home() if home is None else home
    if plat == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA")
        root = Path(local_appdata) if local_appdata else base_home / "AppData" / "Local"
        return root / "agents-quota-ping"
    xdg = os.environ.get("XDG_STATE_HOME")
    root = Path(xdg) if xdg else base_home / ".local" / "state"
    return root / "agents-quota-ping"


def default_config_file() -> Path:
    """Return path to optional user config JSON file."""
    override = os.environ.get("QUOTA_PING_CONFIG")
    if override:
        return Path(override)
    return default_config_dir() / "config.json"


def _candidate_bin_dirs(home: Path | None = None, platform: str | None = None) -> list[Path]:
    plat = sys.platform if platform is None else platform
    base_home = Path.home() if home is None else home
    dirs: list[Path] = []
    if plat == "win32":
        appdata = Path(os.environ.get("APPDATA", str(base_home / "AppData" / "Roaming")))
        local_appdata = Path(os.environ.get("LOCALAPPDATA", str(base_home / "AppData" / "Local")))
        dirs.extend(
            [
                base_home / ".local" / "bin",
                appdata / "npm",
                local_appdata / "Programs" / "Antigravity",
                local_appdata / "Volta" / "bin",
            ]
        )
    else:
        dirs.extend(
            [
                base_home / ".local" / "bin",
                base_home / "bin",
                Path("/opt/homebrew/bin"),
                Path("/usr/local/bin"),
                Path("/usr/bin"),
                Path("/bin"),
            ]
        )
        nvm_versions = base_home / ".nvm" / "versions" / "node"
        if nvm_versions.is_dir():
            version_bins = sorted(
                (p / "bin" for p in nvm_versions.iterdir() if (p / "bin").is_dir()),
                reverse=True,
            )
            dirs.extend(version_bins)
    return dirs


def build_search_path(existing_path: str | None = None, home: Path | None = None, platform: str | None = None) -> str:
    """Construct an extended PATH that still works under minimal cron/launchd/Task Scheduler shells."""
    current = os.environ.get("PATH", "") if existing_path is None else existing_path
    seen: set[str] = set()
    ordered: list[str] = []
    for entry in [*[str(d) for d in _candidate_bin_dirs(home=home, platform=platform)], *current.split(os.pathsep)]:
        if not entry or entry in seen:
            continue
        seen.add(entry)
        ordered.append(entry)
    return os.pathsep.join(ordered)


def resolve_binary(name: str, configured: str = "", env_var: str = "", search_path: str | None = None) -> str:
    """Resolve an executable using explicit config, env override, or extended PATH lookup."""
    if env_var:
        env_val = os.environ.get(env_var, "").strip()
        if env_val:
            return env_val
    if configured.strip():
        return configured.strip()
    path_str = build_search_path(search_path)
    found = shutil.which(name, path=path_str)
    return found or name


def prepare_command_argv(argv: list[str], platform: str | None = None) -> list[str]:
    """Wrap Windows .cmd/.bat shims via cmd.exe /c without enabling shell=True."""
    plat = sys.platform if platform is None else platform
    if plat == "win32" and argv and argv[0].lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", *argv]
    return argv


@dataclass(frozen=True)
class AppConfig:
    """Resolved runtime configuration combining defaults, JSON config, and env vars.

    ``lock_file`` is the legacy global lock path. Live runs lock
    ``state_dir / run-<provider>.lock`` instead, so one provider's follow-up
    never blocks the others. Follow-up policy is ``followup_enabled`` /
    ``followup_cutoff`` plus a seconds-based jitter range.
    """

    state_dir: Path
    empty_workdir: Path
    lock_file: Path
    log_file: Path
    codex_home: Path
    claude_dir: Path
    agy_data_dir: Path
    claude_bin: str
    codex_bin: str
    agy_bin: str
    lark_bin: str
    claude_guard_bin: str = ""
    claude_clear_proxy_env: bool = False
    http_proxy: str = ""
    all_proxy: str = ""
    no_proxy: str = "localhost,127.0.0.1,::1"
    feishu_user_id: str = ""
    feishu_chat_id: str = ""
    webhook_url: str = ""
    search_path: str = field(default_factory=build_search_path)
    # One-shot follow-up after a measured reset. Jitter bounds are seconds.
    followup_enabled: bool = True
    followup_cutoff: str = "18:30"
    followup_jitter_min: int = 15
    followup_jitter_max: int = 45


def _coerce_bool(value: object, default: bool) -> bool:
    """Parse a JSON or env boolean without treating the string ``"false"`` as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _coerce_int(value: object, default: int) -> int:
    """Parse an integer config value, falling back when the input is unusable."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _read_json_dict(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_config(config_path: Path | None = None) -> AppConfig:
    """Load configuration from JSON file + environment overrides."""
    cfg_path = default_config_file() if config_path is None else config_path
    raw = _read_json_dict(cfg_path)

    proxy_raw = raw.get("proxy") if isinstance(raw.get("proxy"), dict) else {}
    claude_raw = raw.get("claude") if isinstance(raw.get("claude"), dict) else {}
    codex_raw = raw.get("codex") if isinstance(raw.get("codex"), dict) else {}
    agy_raw = raw.get("agy") if isinstance(raw.get("agy"), dict) else {}
    notify_raw = raw.get("notify") if isinstance(raw.get("notify"), dict) else {}
    schedule_raw = raw.get("schedule") if isinstance(raw.get("schedule"), dict) else {}

    state_dir = default_state_dir()
    home = Path.home()
    codex_home = Path(os.environ.get("CODEX_HOME", str(home / ".codex")))
    claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(home / ".claude")))
    agy_data_dir = Path(os.environ.get("ANTIGRAVITY_APP_DATA_DIR", str(home / ".gemini" / "antigravity-cli")))

    search_path = build_search_path()

    http_proxy = os.environ.get("QUOTA_PING_HTTP_PROXY", str(proxy_raw.get("http_proxy", ""))).strip()
    all_proxy = os.environ.get("QUOTA_PING_ALL_PROXY", str(proxy_raw.get("all_proxy", ""))).strip()
    no_proxy = os.environ.get(
        "QUOTA_PING_NO_PROXY",
        str(proxy_raw.get("no_proxy", "localhost,127.0.0.1,::1")),
    ).strip()

    claude_guard = os.environ.get("QUOTA_PING_CLAUDE_GUARD_BIN", str(claude_raw.get("guard_bin", ""))).strip()
    clear_proxy_env = bool(claude_raw.get("clear_proxy_env", False))

    followup_enabled = _coerce_bool(schedule_raw.get("followup_enabled", True), True)
    followup_env = os.environ.get("QUOTA_PING_FOLLOWUP_ENABLED", "").strip()
    if followup_env:
        followup_enabled = _coerce_bool(followup_env, followup_enabled)
    followup_cutoff = str(schedule_raw.get("followup_cutoff", "18:30") or "18:30").strip() or "18:30"
    cutoff_env = os.environ.get("QUOTA_PING_FOLLOWUP_CUTOFF", "").strip()
    if cutoff_env:
        followup_cutoff = cutoff_env
    followup_jitter_min = _coerce_int(schedule_raw.get("followup_jitter_min", 15), 15)
    followup_jitter_max = _coerce_int(schedule_raw.get("followup_jitter_max", 45), 45)

    return AppConfig(
        state_dir=state_dir,
        empty_workdir=state_dir / "empty",
        lock_file=state_dir / "run.lock",
        log_file=state_dir / "quota-ping.log",
        codex_home=codex_home,
        claude_dir=claude_dir,
        agy_data_dir=agy_data_dir,
        claude_bin=resolve_binary("claude", str(claude_raw.get("bin", "")), "QUOTA_PING_CLAUDE_BIN", search_path),
        codex_bin=resolve_binary("codex", str(codex_raw.get("bin", "")), "QUOTA_PING_CODEX_BIN", search_path),
        agy_bin=resolve_binary("agy", str(agy_raw.get("bin", "")), "QUOTA_PING_AGY_BIN", search_path),
        lark_bin=resolve_binary("lark-cli", str(notify_raw.get("lark_bin", "")), "QUOTA_PING_LARK_BIN", search_path),
        claude_guard_bin=claude_guard,
        claude_clear_proxy_env=clear_proxy_env,
        http_proxy=http_proxy,
        all_proxy=all_proxy,
        no_proxy=no_proxy,
        feishu_user_id=os.environ.get("QUOTA_PING_FEISHU_USER_ID", str(notify_raw.get("feishu_user_id", ""))).strip(),
        feishu_chat_id=os.environ.get("QUOTA_PING_FEISHU_CHAT_ID", str(notify_raw.get("feishu_chat_id", ""))).strip(),
        webhook_url=os.environ.get("QUOTA_PING_WEBHOOK_URL", str(notify_raw.get("webhook_url", ""))).strip(),
        search_path=search_path,
        followup_enabled=followup_enabled,
        followup_cutoff=followup_cutoff,
        followup_jitter_min=followup_jitter_min,
        followup_jitter_max=followup_jitter_max,
    )


_PROXY_ENV_KEYS = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "no_proxy",
    "NO_PROXY",
)


def base_cron_env(cfg: AppConfig, base: dict[str, str] | None = None) -> dict[str, str]:
    """Build deterministic environment with extended PATH for scheduled jobs."""
    env = dict(os.environ if base is None else base)
    env["PATH"] = cfg.search_path
    return env


def proxy_env(cfg: AppConfig, base: dict[str, str] | None = None) -> dict[str, str]:
    """Build environment applying configured HTTP/SOCKS proxies when set."""
    env = base_cron_env(cfg, base)
    if cfg.http_proxy:
        env["http_proxy"] = cfg.http_proxy
        env["https_proxy"] = cfg.http_proxy
        env["HTTP_PROXY"] = cfg.http_proxy
        env["HTTPS_PROXY"] = cfg.http_proxy
    if cfg.all_proxy:
        env["all_proxy"] = cfg.all_proxy
        env["ALL_PROXY"] = cfg.all_proxy
    if cfg.no_proxy:
        env["no_proxy"] = cfg.no_proxy
        env["NO_PROXY"] = cfg.no_proxy
    return env


def claude_env(cfg: AppConfig, base: dict[str, str] | None = None) -> dict[str, str]:
    """Build environment for Claude Code, optionally clearing inherited proxy vars for wrapper guards."""
    if not cfg.claude_clear_proxy_env:
        return proxy_env(cfg, base)
    env = base_cron_env(cfg, base)
    for key in _PROXY_ENV_KEYS:
        env.pop(key, None)
    return env


def feishu_env(cfg: AppConfig, base: dict[str, str] | None = None) -> dict[str, str]:
    """Build environment for lark-cli bypassing local proxies."""
    env = base_cron_env(cfg, base)
    for key in _PROXY_ENV_KEYS:
        env.pop(key, None)
    env["LARK_CLI_NO_PROXY"] = "1"
    return env
