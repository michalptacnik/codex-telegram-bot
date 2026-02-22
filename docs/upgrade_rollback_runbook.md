# Upgrade and Rollback Runbook

Operational procedure for safe upgrades and controlled rollbacks.

## Scope

- Runtime: `codex-telegram-bot` systemd service.
- State: SQLite database at `<config-dir>/state.db`.
- Installer flow: `scripts/bootstrap_ubuntu.sh`.

## Preconditions

1. Confirm you have shell access with rights to stop/start the service.
2. Identify your config directory:
   - user mode: `~/.config/codex-telegram-bot`
   - system mode: deployment-specific, often `/etc/codex-telegram-bot` or service override
3. Ensure enough free disk for at least one full `state.db` copy.

## Data Migration Safety Checks

`bootstrap_ubuntu.sh` now performs a preflight migration check before venv install/service wiring:

1. Detect existing `<config-dir>/state.db`.
2. Run `PRAGMA integrity_check`.
3. Abort if integrity check is not `ok`.
4. Create backup:
   - `<config-dir>/backups/state.<UTC_TIMESTAMP>.pre-migration.db`
   - `<config-dir>/backups/state.<UTC_TIMESTAMP>.pre-migration.db.sha256`

To bypass (not recommended except emergency break-glass):

```bash
./scripts/bootstrap_ubuntu.sh ... --skip-migration-check
```

## Upgrade Procedure

User service example:

```bash
cd /path/to/codex-telegram-bot
git fetch origin
git checkout <target-ref>
./scripts/bootstrap_ubuntu.sh --user --workdir "$(pwd)" --skip-apt
systemctl --user restart codex-telegram-bot
systemctl --user status codex-telegram-bot --no-pager
```

System service example:

```bash
cd /opt/codex-telegram-bot
git fetch origin
git checkout <target-ref>
sudo ./scripts/bootstrap_ubuntu.sh --system --workdir "$(pwd)" --skip-apt
sudo systemctl restart codex-telegram-bot
sudo systemctl status codex-telegram-bot --no-pager
```

Post-upgrade checks:

1. `GET /health` returns `status=ok`.
2. `GET /api/reliability` returns expected snapshot fields.
3. Telegram `/status` responds with active version/session info.

## Rollback Procedure

1. Stop service.
2. Checkout previous known-good ref.
3. Re-run bootstrap for that ref.
4. Restore pre-migration DB backup if needed.
5. Start service and verify health.

User service rollback:

```bash
cd /path/to/codex-telegram-bot
systemctl --user stop codex-telegram-bot
git checkout <previous-known-good-ref>
./scripts/bootstrap_ubuntu.sh --user --workdir "$(pwd)" --skip-apt --no-enable
cp ~/.config/codex-telegram-bot/backups/state.<TIMESTAMP>.pre-migration.db ~/.config/codex-telegram-bot/state.db
systemctl --user start codex-telegram-bot
systemctl --user status codex-telegram-bot --no-pager
```

System service rollback:

```bash
cd /opt/codex-telegram-bot
sudo systemctl stop codex-telegram-bot
git checkout <previous-known-good-ref>
sudo ./scripts/bootstrap_ubuntu.sh --system --workdir "$(pwd)" --skip-apt --no-enable
sudo cp <config-dir>/backups/state.<TIMESTAMP>.pre-migration.db <config-dir>/state.db
sudo systemctl start codex-telegram-bot
sudo systemctl status codex-telegram-bot --no-pager
```

## Validation Evidence (2026-02-22)

Milestone upgrade path dry-run tested:

- source commit: `46cad9e`
- target commit: `95a5897`
- command style:
  - `bootstrap_ubuntu.sh --skip-apt --no-enable --dry-run`
- observed:
  - source -> target includes migration preflight integrity + backup commands
  - target -> source command sequence executes for rollback re-install path

Notes:

- Dry-run validates procedure and command sequence without mutating service state.
- For production release, run the same sequence without `--dry-run` in a staging host first.
