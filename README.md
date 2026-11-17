# agents-quota-ping

> **Align 5-hour rolling quota windows for Claude Code, OpenAI Codex, and Google Antigravity (`agy`) with your workday — using the cheapest models, minimal context, live API reset-time verification, and cross-platform notifications.**

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Dependencies: Zero](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg)](#installation)
[![Platforms: Linux | macOS | Windows](https://img.shields.io/badge/platforms-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey.svg)](#cross-platform-scheduling)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

---

## Why `agents-quota-ping`?

Subscription plans for **Claude Code**, **Codex**, and **Antigravity (`agy`)** typically meter usage on a **rolling 5-hour window** starting from your **first message**. If you start coding at 10:00 AM, your window resets at 15:00 — right in the middle of afternoon work — leaving morning hours unused.

Sending a tiny scheduled ping before work aligns three clean windows across the day (for example **`07:00 → 12:02 → 17:04`**, covering `07:00–22:04`).

However, running naive commands like `claude -p "OK"` or `codex exec "OK"` in a cron job has four hidden traps that this tool solves out of the box:

1. **Flagship default models & reasoning waste**: Your interactive defaults (e.g., Fable/Opus `xhigh`, `gpt-6.1-sol` `max`, `Gemini 4 Hydrogen XHigh`), MCP servers, hooks, plugins, and `AGENTS.md`/`CLAUDE.md` get loaded on every naive ping — costing **10×–80× more quota** than needed.
2. **Midday window collision**: If a `07:00` ping triggers after a random 60s delay (`07:01`), a `12:00` ping with a 30s delay (`12:00:30`) lands *inside* the old morning window instead of opening the afternoon window. Progressive triggers (`07:00`, `12:02`, `17:04`) + short jitter prevent boundary collisions.
3. **Antigravity separate quota buckets**: `agy` separates `Gemini Flash + Hydrogen` (Pool 1), `Argon` (Pool 2), and `Claude/GPT-OSS` (Pool 3). Pinging `gemini-3.8-flash-low` warms Pool 1 at Flash cost without burning Argon or third-party pools.
4. **Silent auth/model breakage in headless cron**: Non-interactive schedulers lack `nvm`/Homebrew `PATH` and proxy variables, and models eventually retire.

---

## Key Features

- **Cheapest Model + Isolated Context per CLI**
  - **Claude Code**: `--model haiku --effort low --tools "" --strict-mcp-config --disable-slash-commands --safe-mode --setting-sources user --system-prompt "Reply OK only." --no-session-persistence --output-format json` (keeps OAuth intact, unlike `--bare`, while skipping plugins, hooks, MCP, and default system prompt).
  - **Codex CLI**: `codex exec -m gpt-6-luna -c model_reasoning_effort="low" -c model_verbosity="low" --ephemeral --ignore-user-config --ignore-rules --skip-git-repo-check --sandbox read-only --json -C <empty_dir>` (skips `config.toml` high-effort defaults, MCP, and hooks).
  - **Antigravity (`agy`)**: `--model gemini-3.8-flash-low --effort low --disable-slash-commands --mode plan --settings '{"allowNonWorkspaceAccess":false,"statusLine":{"enabled":false},"title":{"enabled":false}}' --output-format json` in an empty directory (avoids workspace `AGENTS.md` injection; touches Pool 1 only).
- **Live Reset-Time & Utilization API Probes (`--status-only` supported)**
  - **Claude**: `GET https://api.anthropic.com/api/oauth/usage` (`five_hour.resets_at`, `utilization`, `seven_day`)
  - **Codex**: `GET https://chatgpt.com/backend-api/codex/usage` (fallback `wham/usage`; `primary_window.reset_at`, `used_percent`, `secondary_window`)
  - **Antigravity**: `POST https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels` (`quotaInfo.resetTime`, `remainingFraction` for Pool 1)
  - Gracefully falls back to estimated `+5h` if an API endpoint is temporarily unreachable.
- **Exact Per-Ping Token Telemetry**
  - Extracts real `input_tokens`, `cached_tokens`, `output_tokens`, and `total_cost_usd` directly from `--output-format json` / `--json`.
- **Smart 3-Way Failure Handling (No Blind Escalation)**
  - **Transient network/timeout** → retries Tier 1 once; never escalates to expensive models.
  - **Auth expired / quota exhausted (`401`, `429`, `RESOURCE_EXHAUSTED`)** → stops immediately and alerts; never burns other pools.
  - **Retired model / changed CLI flags** → dynamically selects the lightest available replacement from `~/.codex/models_cache.json` or `agy models` (Pool 1 only), or falls back safely while flagging `⚠ DEGRADED` in the notification.
- **Cross-Platform & Zero Dependencies**
  - Pure Python 3.10+ standard library; runs on **Linux**, **macOS**, and **Windows** (`fcntl` / `msvcrt` file lock, `.cmd`/`.bat` shim handling, XDG / `%APPDATA%` paths, macOS Keychain fallback for Claude OAuth).
- **Flexible Notifications**: Feishu (`lark-cli` DM or group chat) and/or any JSON Webhook (Feishu bot, Slack, Discord, custom endpoint).

---

## Measured Footprint per Ping

Measured directly from a live run with `--no-jitter` (empty working directory):

| Provider | Model (Tier 1) | Measured Tokens | Quota Impact | Latency |
|---|---|---|---|---|
| **Claude Code** | `haiku` (`claude-haiku-5-5`) | **713 tok** (`709 in / 4 out`, `~$0.00001`) | Negligible | ~10s |
| **Codex CLI** | `gpt-6-luna` | **29,006 tok** (`29,001 in / 5 out`, built-in tool schemas) | `0%` on 5h meter | ~15s |
| **Antigravity** | `gemini-3.8-flash-low` | **11,235 tok** (`11,234 in / 1 out`) | ~1% Pool 1 Flash/Hydrogen | ~13s |

---

## Installation

### Option 1: Install as a CLI tool (`pipx` / `uv` / `pip`)

```bash
git clone https://github.com/JasonJarvan/agents-quota-ping.git
cd agents-quota-ping

# Recommended (works on Linux, macOS, Windows):
uv tool install .
# or: pipx install .
# or: pip install --user .
```

### Option 2: Direct symlink from repo (Linux / macOS)

```bash
git clone https://github.com/JasonJarvan/agents-quota-ping.git ~/Codes/Infra/agents-quota-ping
mkdir -p ~/.local/bin
ln -sfn ~/Codes/Infra/agents-quota-ping/bin/quota-ping ~/.local/bin/quota-ping
```

Make sure all three CLIs (`claude`, `codex`, `agy`) are already authenticated interactively before scheduling headless runs.

---

## Configuration (Optional)

Copy [`config.example.json`](config.example.json) to your OS config path:

- **Linux / macOS**: `~/.config/agents-quota-ping/config.json`
- **Windows**: `%APPDATA%\agents-quota-ping\config.json`

```json
{
  "proxy": {
    "http_proxy": "http://127.0.0.1:7890",
    "all_proxy": "socks5h://127.0.0.1:7891",
    "no_proxy": "localhost,127.0.0.1,::1"
  },
  "claude": {
    "bin": "",
    "guard_bin": "",
    "clear_proxy_env": false
  },
  "codex": {
    "bin": ""
  },
  "agy": {
    "bin": ""
  },
  "notify": {
    "lark_bin": "",
    "feishu_user_id": "ou_your_open_id",
    "feishu_chat_id": "",
    "webhook_url": ""
  }
}
```

Environment variable overrides are also supported: `QUOTA_PING_CONFIG`, `QUOTA_PING_STATE_DIR`, `QUOTA_PING_HTTP_PROXY`, `QUOTA_PING_ALL_PROXY`, `QUOTA_PING_WEBHOOK_URL`, `QUOTA_PING_FEISHU_USER_ID`, `QUOTA_PING_FEISHU_CHAT_ID`, `QUOTA_PING_CLAUDE_BIN`, `QUOTA_PING_CODEX_BIN`, `QUOTA_PING_AGY_BIN`, `QUOTA_PING_LARK_BIN`.

---

## Usage

```bash
# Inspect resolved commands without calling models
quota-ping --dry-run

# Check real reset times & remaining % across all 3 APIs (0 tokens spent)
quota-ping --status-only
quota-ping --status-only --json

# Run a real ping right now without startup jitter
quota-ping --no-jitter

# Ping only selected providers
quota-ping --only claude,codex --no-jitter
```

Example output / notification:

```markdown
✅ **AI 5h 额度唤醒完成** (2026-11-17 07:00:42)
触发耗时: jitter 14s · 完成 07:00:42

- ✓ **Claude**: Tier 1 `haiku` (10.1s) · 713 tok (in 709/out 4), ~$0.00001 · 实测重置 12:00:00 · 5h已用1% · 7d已用12%
- ✓ **Codex**: Tier 1 `gpt-6-luna` (14.7s) · 29006 tok (in 29001/out 5) · 实测重置 12:00:38 · 5h已用0% · 7d已用43%
- ✓ **Antigravity (Pool 1)**: Tier 1 `gemini-3.8-flash-low` (13.1s) · 11235 tok (in 11234/out 1) · 实测重置 12:00:40 · 5h已用1%
```

---

## Cross-Platform Scheduling

### Linux (`cron` or `systemd --user`)

**Option A — `crontab -e`**:

```cron
0 7 * * * ~/.local/bin/quota-ping >> ~/.local/state/agents-quota-ping/cron.log 2>&1
2 12 * * * ~/.local/bin/quota-ping >> ~/.local/state/agents-quota-ping/cron.log 2>&1
4 17 * * * ~/.local/bin/quota-ping >> ~/.local/state/agents-quota-ping/cron.log 2>&1
```

**Option B — `systemd --user` timer** (handles missed wakeups via `Persistent=true`):

```bash
mkdir -p ~/.config/systemd/user
cp contrib/agents-quota-ping.service contrib/agents-quota-ping.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agents-quota-ping.timer
```

### macOS (`launchd`)

`launchd` is preferred over `cron` on macOS and automatically runs missed triggers when the Mac wakes from sleep:

```bash
mkdir -p ~/Library/LaunchAgents ~/.local/state/agents-quota-ping
sed "s|__HOME__|$HOME|g" contrib/com.agents-quota-ping.plist > ~/Library/LaunchAgents/com.agents-quota-ping.plist
launchctl unload ~/Library/LaunchAgents/com.agents-quota-ping.plist 2>/dev/null || true
launchctl load -w ~/Library/LaunchAgents/com.agents-quota-ping.plist
```

> **macOS note**: Claude Code stores OAuth tokens in the macOS Keychain (`Claude Code-credentials`) by default rather than `~/.claude/.credentials.json`; `quota-ping` automatically falls back to `security find-generic-password` on Darwin when running in a user LaunchAgent session.

### Windows (Task Scheduler)

Install via `uv tool install .` or `pipx install .` (which generates `quota-ping.exe` and automatically handles `.cmd`/`.bat` npm shims like `claude.cmd` and `codex.cmd`), then register the scheduled task in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\contrib\windows-task-scheduler.ps1
```

The provided [`contrib/windows-task-scheduler.ps1`](contrib/windows-task-scheduler.ps1) configures daily `07:00`, `12:02`, and `17:04` triggers with `-StartWhenAvailable` and battery-friendly flags.

> **Windows tip**: If your CLIs live inside **WSL2**, run the Linux `cron` or `systemd` setup inside WSL2 instead of native Windows Task Scheduler.

---

## Development & Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

---

## License

[MIT](LICENSE)
