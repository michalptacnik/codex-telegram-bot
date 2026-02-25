# Production Deployment Guide

This guide covers deploying `codex-telegram-bot` in production, both single-tenant
(one operator, one Telegram bot) and multi-tenant (multiple isolated user groups).

---

## System Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| OS | Ubuntu 22.04 / Debian 12 | Ubuntu 24.04 |
| CPU | 1 core | 2 cores |
| RAM | 512 MB | 2 GB |
| Disk | 2 GB | 10 GB |
| Python | 3.10 | 3.12 |
| codex CLI | latest | latest |

---

## Environment Variables

Required:

```
TELEGRAM_BOT_TOKEN=<your-bot-token>
EXECUTION_WORKSPACE_ROOT=/var/lib/codex-bot/workspace
```

Strongly recommended for production:

```
# API auth for Control Center
LOCAL_API_KEYS=admintoken:admin:*;readertoken:api:read

# Provider
PROVIDER_BACKEND=codex-cli

# Execution limits
CODEX_EXEC_TIMEOUT_SEC=180
TOOL_LOOP_MAX_STEPS=3

# Alerting
ALERT_WEBHOOK_URL=https://your-webhook/endpoint
ALERT_MIN_SEVERITY=medium

# Session isolation
SESSION_WORKSPACES_ROOT=/var/lib/codex-bot/sessions
WORKSPACE_MAX_DISK_BYTES=104857600
```

Optional:

```
# Fallback provider (echo mode for degraded operation)
PROVIDER_FALLBACK_MODE=echo

# Retention
SESSION_ARCHIVE_AFTER_IDLE_DAYS=30
SESSION_DELETE_AFTER_DAYS=90

# Memory compaction
SESSION_MAX_MESSAGES=200
SESSION_COMPACT_KEEP=40
```

---

## Installation

### From Debian package (recommended)

```bash
# Build package
./scripts/build_deb.sh --output-dir dist

# Install
sudo dpkg -i dist/codex-telegram-bot_*.deb
```

### From source

```bash
git clone <repo>
cd codex-telegram-bot
python -m pip install -e .
```

---

## Process Supervision

### systemd (recommended)

Copy the included unit file:

```bash
sudo cp systemd/codex-telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now codex-telegram-bot
```

Check status:

```bash
sudo systemctl status codex-telegram-bot
journalctl -u codex-telegram-bot -f
```

### Docker

```bash
docker compose up -d
docker compose logs -f
```

---

## Control Center

The Control Center web UI listens on port `8080` by default. To expose it:

```bash
# Run the bot + control center
codex-telegram-bot serve --port 8080
```

Protect with a reverse proxy (nginx example):

```nginx
server {
    listen 443 ssl;
    server_name control.yourdomain.example;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

Always set `LOCAL_API_KEYS` when exposing the Control Center outside localhost.

---

## Data Persistence

State is stored in SQLite at:

```
$STATE_DB_PATH  (default: ~/.codex-telegram-bot/state.db)
```

Backup:

```bash
sqlite3 ~/.codex-telegram-bot/state.db ".backup /backup/state-$(date +%Y%m%d).db"
```

Restore:

```bash
cp /backup/state-20260101.db ~/.codex-telegram-bot/state.db
```

---

## Upgrade Procedure

1. Stop the service: `sudo systemctl stop codex-telegram-bot`
2. Back up state DB (see above)
3. Install new package: `sudo dpkg -i dist/codex-telegram-bot_*.deb`
4. Start the service: `sudo systemctl start codex-telegram-bot`
5. Verify health: `curl http://localhost:8080/health`

For rollback: stop, restore DB backup, reinstall previous package.

See `docs/upgrade_rollback_runbook.md` for the full runbook.

---

## Multi-Tenant Deployment

For multiple isolated user groups:

1. Run one instance per tenant, each with its own:
   - `TELEGRAM_BOT_TOKEN`
   - `EXECUTION_WORKSPACE_ROOT`
   - `SESSION_WORKSPACES_ROOT`
   - `STATE_DB_PATH`
   - Different port for Control Center

2. Use a process supervisor (systemd templates or Docker Compose services) to
   manage each instance independently.

3. Apply OS-level user isolation: run each tenant's bot as a separate system user
   with restricted filesystem permissions.

4. Do not share workspace directories across tenants.

---

## Health Checks

```bash
# Liveness
curl http://localhost:8080/health

# Readiness (first-run checks)
curl -H "x-local-api-key: <token>" http://localhost:8080/api/onboarding/readiness
```

Expected healthy response from `/health`:

```json
{
  "status": "ok",
  "provider_health": {"status": "healthy"},
  "metrics": {"total_runs": ...}
}
```
