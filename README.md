# agents-quota-ping

> **Align 5-hour rolling quota windows for Claude Code, OpenAI Codex, and Google Antigravity (`agy`) with your workday — using the cheapest models, minimal context, live API reset-time verification, parallel execution, and optional notifications.**

[![CI](https://github.com/JasonJarvan/agents-quota-ping/actions/workflows/ci.yml/badge.svg)](https://github.com/JasonJarvan/agents-quota-ping/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Dependencies: Zero](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg)](#installation)
[![Platforms: Linux | macOS | Windows](https://img.shields.io/badge/platforms-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey.svg)](#cross-platform-scheduling)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

---

## Why `agents-quota-ping`?

Subscription plans for **Claude Code**, **Codex**, and **Antigravity (`agy`)** meter usage on a **rolling 5-hour window** starting from your **first message**. If you start coding at 10:00 AM, your window resets at 15:00 — right in the middle of afternoon work — leaving morning hours unused.

Sending a tiny scheduled ping before work aligns three clean windows across the day (for example **`07:00 → 12:02 → 17:04`**, covering `07:00–22:04`).

Running naive commands like `claude -p "OK"` or `codex exec "OK"` in a cron job has four hidden traps that this tool solves out of the box:

1. **Flagship default models & reasoning waste**: Your interactive defaults (e.g., Fable/Opus `xhigh`, `gpt-6.1-sol` `max`, `Gemini 4 Hydrogen XHigh`), MCP servers, hooks, plugins, and `AGENTS.md`/`CLAUDE.md` get loaded on every naive ping — costing **10×–80× more quota** than needed.
2. **Midday window collision**: If a `07:00` ping triggers after a random 60s delay (`07:01`), a `12:00` ping with a 30s delay (`12:00:30`) lands *inside* the old morning window instead of opening the afternoon window. Progressive triggers (`07:00`, `12:02`, `17:04`) + short jitter prevent boundary collisions.
3. **Antigravity separate quota buckets**: `agy` separates its **Gemini pool** (`Flash` / `Hydrogen` / `Pro`) from its **Claude / GPT-OSS pool** (`gpt-oss-120b-medium`, `claude-sonnet-5-5-*`, `claude-opus-5-5-*`), with independent 5-hour reset timers. `quota-ping` warms **both** pools independently (`agy` via `gemini-3.8-flash-low` and `agy-claude` via `gpt-oss-120b-medium`, auto-falling back to `claude-sonnet-5-5-low` once `gpt-oss-120b-medium` sunsets).
4. **Silent auth/model breakage in headless cron**: Non-interactive schedulers lack `nvm`/Homebrew `PATH` and proxy variables, and models eventually retire.

---

## Key Features

- **Cheapest Model + Isolated Context per CLI** (per-invocation flags only — never modifies your global `settings.json` / `config.toml`)
  - **Claude Code**: `--model haiku --effort low --tools "" --strict-mcp-config --disable-slash-commands --safe-mode --setting-sources user --system-prompt "Reply OK only." --no-session-persistence --output-format json` (keeps OAuth intact, unlike `--bare`, while skipping plugins, hooks, MCP, and default system prompt).
  - **Codex CLI**: `codex exec -m gpt-6-luna -c model_reasoning_effort="low" -c model_verbosity="low" --ephemeral --ignore-user-config --ignore-rules --skip-git-repo-check --sandbox read-only --disable apps --disable shell_tool --disable view_image --disable skill_search --disable computer_use --disable browser_use --disable sleep_tool --json -C <empty_dir>` (skips `config.toml` high-effort defaults, MCP, hooks, and non-essential built-in tool schemas — **44% smaller** than default `codex exec`).
  - **Antigravity (`agy` — Pool 1 Gemini)**: `--model gemini-3.8-flash-low --effort low --disable-slash-commands --mode plan --settings '{"allowNonWorkspaceAccess":false,"statusLine":{"enabled":false},"title":{"enabled":false}}' --output-format json` in an empty directory (avoids workspace `AGENTS.md` injection; touches Pool 1 only).
  - **Antigravity (`agy-claude` — Pool 2 Claude/GPT-OSS)**: `--model gpt-oss-120b-medium --disable-slash-commands --mode plan --settings ... --output-format json` (~0.0025% quota per ping, ~400× cheaper than `claude-sonnet-5-5-low`). Automatically discovers Pool 2 fallbacks (`claude-sonnet-5-5-low` first, never Pool 1) once `gpt-oss-120b-medium` retires.
- **Pre-Flight Quota Check (0-Token Smart Skip)**
  - Before invoking any LLM, probes the live quota APIs (~0.5s, 0 tokens):
    - **Exhausted (`0%` remaining) or multi-day cooldown** → skips automatically (avoids wasted calls and prevents hitting Antigravity overage credits at 0%).
    - **Active 5h window already running (`used > 0%`)** → skips automatically unless `--force` or `--no-skip-active` is passed (never skips when `used == 0%`, because unused windows only report a floating placeholder reset time until the first real message!).
- **Live Reset-Time & Utilization API Probes (`--status-only` supported)**
  - **Claude**: `GET https://api.anthropic.com/api/oauth/usage` (`five_hour.resets_at`, `utilization`, `seven_day`)
  - **Codex**: `GET https://chatgpt.com/backend-api/codex/usage` (fallback `wham/usage`; `primary_window.reset_at`, `used_percent`, `secondary_window`)
  - **Antigravity**: `POST https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels` (`quotaInfo.resetTime`, `remainingFraction` for Pool 1)
  - Gracefully falls back to estimated `+5h` if an API endpoint is temporarily unreachable.
- **Parallel Execution & Exact Token Telemetry**
  - Runs all selected providers concurrently (`ThreadPoolExecutor`, finishing in ~15s instead of ~40s).
  - Extracts real uncached `input_tokens`, `cached_tokens`, `output_tokens`, and `total_cost_usd` directly from JSON/JSONL outputs (server-side prompt prefix caching can still apply even on `--ephemeral` single-shot requests).
- **Smart 3-Way Failure Handling (No Blind Escalation)**
  - **Transient network/timeout** → retries pre-flight quota probes (`5s → 10s → 20s`) and Tier 1 pings (`10s → 20s → 40s`, up to 3 retries) with exponential backoff; never escalates to expensive models and never bypasses configured proxies/guards.
  - **Auth expired / quota exhausted (`401`, `429`, `RESOURCE_EXHAUSTED`)** → stops immediately and alerts; never burns other pools.
  - **Retired model / changed CLI flags** → dynamically selects the lightest available replacement from `~/.codex/models_cache.json` or `agy models` (Pool 1 only), or falls back safely while flagging `⚠ DEGRADED`.
- **Cross-Platform & Zero Dependencies**
  - Pure Python 3.10+ standard library; runs on **Linux**, **macOS**, and **Windows** (`fcntl` / `msvcrt` file lock, `.cmd`/`.bat` shim handling, XDG / `%APPDATA%` paths, macOS Keychain fallback for Claude OAuth).
- **Optional Notifications (Quiet on All-Skipped)**
  - Notifications are **100% optional** — works standalone with stdout/log/`--json`, or notifies via generic **Webhook** (Slack, Discord, Feishu custom bot, etc.) or **Feishu (`lark-cli`)** DM/group chat.
  - When **every** provider in a run is skipped (all windows already active, exhausted, or in cooldown), remote notifications are suppressed automatically (follow-up timers still register and log locally).
- **Independent OS follow-up**
  - After a run, registers a per-provider one-shot at `reset_at` plus 15–45 seconds when that reset is within 5.5 hours and still before the local cutoff (default 18:30). Linux uses `systemd-run --user`, macOS `launchd`, Windows Task Scheduler. The process does not sleep until the reset. `--no-followup` disables it.

---

## Measured Footprint per Ping

Measured directly from live runs on an empty working directory:

| Provider | Model (Tier 1) | Measured Tokens | Quota Impact | Latency |
|---|---|---|---|---|
| **Claude Code** | `haiku` (`claude-haiku-5-5`) | **713 tok** (`709 in / 4 out`, `~$0.00001`) | Negligible | ~10s |
| **Codex CLI** | `gpt-6-luna` (non-essential tools disabled) | **16,147 tok** (`16,147 in / 5 out`, down 44% from `29,001`) | `0%` on 5h meter | ~11s |
| **Antigravity (Pool 1)** | `gemini-3.8-flash-low` | **11,235 tok** (`11,234 in / 1 out`) | ~1% Pool 1 Flash/Hydrogen | ~13s |
| **Antigravity (Claude/GPT)** | `gpt-oss-120b-medium` (fallback `claude-sonnet-5-5-low`) | **6,879 tok** (`3,884 in + 2,944 cache / 51 out`) | **0.0025%** Pool 2 (~400× cheaper than Sonnet 5.5 Low at 1.01%) | ~13s |

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

By default `quota-ping` works out of the box with no config file (logs to OS state dir, stdout/JSON only). To add a proxy or optional notifications, copy [`config.example.json`](config.example.json) to your OS config path:

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
  "schedule": {
    "followup_enabled": true,
    "followup_cutoff": "18:30",
    "followup_jitter_min": 15,
    "followup_jitter_max": 45
  },
  "notify": {
    "lark_bin": "",
    "feishu_user_id": "",
    "feishu_chat_id": "",
    "webhook_url": ""
  }
}
```

Environment variable overrides: `QUOTA_PING_CONFIG`, `QUOTA_PING_STATE_DIR`, `QUOTA_PING_HTTP_PROXY`, `QUOTA_PING_ALL_PROXY`, `QUOTA_PING_WEBHOOK_URL`, `QUOTA_PING_FEISHU_USER_ID`, `QUOTA_PING_FEISHU_CHAT_ID`, `QUOTA_PING_CLAUDE_BIN`, `QUOTA_PING_CODEX_BIN`, `QUOTA_PING_AGY_BIN`, `QUOTA_PING_LARK_BIN`, `QUOTA_PING_FOLLOWUP_ENABLED`, `QUOTA_PING_FOLLOWUP_CUTOFF`.

---

## Usage

```bash
# Inspect resolved commands without calling models
quota-ping --dry-run

# Check real reset times & remaining % across all 3 APIs (0 tokens spent)
quota-ping --status-only
quota-ping --status-only --json

# Run a ping right now (smart-skips already-active/exhausted windows)
quota-ping --no-jitter

# Force pinging even if a 5h window is already active
quota-ping --no-jitter --force

# Ping only selected providers
quota-ping --only claude,codex --no-jitter

# Skip the per-provider one-shot, or move the same-day cutoff
quota-ping --no-followup
quota-ping --followup-cutoff 19:00
```

Example report:

```markdown
✅ **AI 5h 额度唤醒完成** (2026-11-17 07:00:18)
触发耗时: jitter 14s · 完成 07:00:18

- ✓ **Claude**: Tier 1 `haiku` (10.1s) · 713 tok (in 709/out 4), ~$0.00001 · 实测重置 12:00:00 · 5h已用1% · 7d已用12%
- ✓ **Codex**: Tier 1 `gpt-6-luna` (11.2s) · 16152 tok (in 16147/out 5) · 实测重置 12:00:16 · 5h已用0% · 7d已用43%
- ✓ **Antigravity (Pool 1)**: Tier 1 `gemini-3.8-flash-low` (13.1s) · 11235 tok (in 11234/out 1) · 实测重置 12:00:18 · 5h已用1%
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

### Independent one-shot follow-up

The fixed clocks (`07:00`, `12:02`, `17:04`) stay in place. After each real run, `quota-ping` can also register a **per-provider OS one-shot** for shortly after the measured `reset_at`. The operating system fires that run (`systemd-run --user`, `launchd`, or Task Scheduler). This process does **not** sleep until the reset.

A follow-up is registered only when:

- The attempt was not a hard failure, and `reset_at` is known.
- `0 < reset_at - now ≤ 5h30m`. Longer cooldowns stay on the next `07:00` run (`>5.5h 不预约`).
- `reset_at` plus a random **15–45 second** jitter is still **today** and not later than the cutoff (**18:30** local time by default). Later than that is left to the next morning (`>18:30 不预约`).
- A pre-flight skip still schedules when the window is exhausted (`remaining == 0`) or already active (`used > 0`) inside that horizon. A skip with no percent data does not.

Each provider gets its own timer (`--only <provider> --no-jitter`), so `--only claude` never blocks Codex or Antigravity. Registering again **replaces** that provider's previous timer. `last-ping-<provider>.json` records the UTC time of an `ok` or `degraded` ping and skips another ping within **15 minutes** unless `--force`, so a follow-up sitting next to a fixed cron does not double-ping while Codex `used_percent` is still rounding to `0%`.

macOS `launchd` is minute-granular. The agent is loaded at the start of the target minute with `--delay-seconds` for the remainder, so the ping itself is not early.

```bash
quota-ping --no-followup
quota-ping --followup-cutoff 19:00
```

`schedule` in `config.json`: `followup_enabled` (default `true`), `followup_cutoff` (default `"18:30"`), `followup_jitter_min` / `followup_jitter_max` (seconds, default `15` and `45`). Overrides: `QUOTA_PING_FOLLOWUP_ENABLED`, `QUOTA_PING_FOLLOWUP_CUTOFF`.

---

## Development & Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

---

## License

[MIT](LICENSE)
