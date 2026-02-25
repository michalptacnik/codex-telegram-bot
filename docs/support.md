# Support and Troubleshooting

This guide covers common setup failures, diagnostic steps, and how to report bugs.

---

## Collecting Logs

```bash
# systemd
journalctl -u codex-telegram-bot --since "1 hour ago" > /tmp/bot-logs.txt

# Docker
docker compose logs --tail=500 codex-telegram-bot > /tmp/bot-logs.txt

# Direct run
codex-telegram-bot serve 2>&1 | tee /tmp/bot-logs.txt
```

Attach logs when reporting issues.

---

## Common Setup Failures

### Bot is not responding to Telegram messages

1. Check `TELEGRAM_BOT_TOKEN` is set and valid:
   ```bash
   curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe"
   ```
   Expected: `{"ok": true, "result": {"username": "..."}}`

2. Verify the bot process is running:
   ```bash
   systemctl status codex-telegram-bot
   ```

3. Check for port conflicts or firewall rules blocking outbound HTTPS.

### `codex CLI not found` errors

The bot requires the `codex` CLI in `PATH`. Verify:

```bash
which codex
codex --version
```

If not installed, follow the [codex CLI installation guide](https://github.com/openai/codex).

Use the readiness check endpoint to confirm:

```bash
curl http://localhost:8080/api/onboarding/readiness
```

### Control Center not loading

1. Verify the service is running: `curl http://localhost:8080/health`
2. Check port binding: `ss -tlnp | grep 8080`
3. If behind a reverse proxy, check proxy config and TLS certificates.

### `Error: codex exited with code N`

- Code 1 or 2: Check that `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY`) is set.
- Code 124: Timeout. Increase `CODEX_EXEC_TIMEOUT_SEC`.
- Other: Check `codex exec` manually with the same prompt.

### `SpendLimitExceeded` errors

The default per-user daily spend limit is $10. Increase via `UserProfile.spend_limit_usd`
or use the `AccessController` API to set per-user limits. This requires an admin-role
profile to modify programmatically.

### Workspace permission errors

The bot needs write access to `EXECUTION_WORKSPACE_ROOT` and `SESSION_WORKSPACES_ROOT`.

```bash
ls -la $EXECUTION_WORKSPACE_ROOT
chown -R bot-user:bot-user $EXECUTION_WORKSPACE_ROOT
```

### Database locked errors

Another process is holding the SQLite lock. Only run one instance per `STATE_DB_PATH`.
To check:

```bash
fuser ~/.codex-telegram-bot/state.db
```

---

## FAQ

**Q: Can I use the bot with multiple Telegram users?**
A: Yes. Each `(chat_id, user_id)` pair gets its own session. Use RBAC roles
(`ROLE_VIEWER`/`ROLE_USER`/`ROLE_ADMIN`) to control what each user can do.

**Q: How do I reset the onboarding wizard?**
A: Delete `~/.codex-telegram-bot/onboarding.json` and restart the service.

**Q: How do I clear all sessions and run history?**
A: Stop the service, delete `~/.codex-telegram-bot/state.db`, and restart.

**Q: The parity report shows all gates failing. What does this mean?**
A: The parity harness runs the Telegram agent against a benchmark suite. Gates
failing in CI usually means the codex CLI is unavailable. Use `--offline-telegram`
for CI runs. For production parity failures, check the codex CLI version and
API key configuration.

**Q: How do I disable tool approval prompts?**
A: Set the agent's `policy_profile` to `trusted`. Only do this for highly trusted
users; it disables interactive approval for high-risk tool calls.

---

## Reporting Bugs

1. Check existing issues in the project repository.
2. Collect logs (see above).
3. Include:
   - OS and Python version
   - Bot version (`codex-telegram-bot --version`)
   - Relevant log lines (redact API keys and tokens)
   - Steps to reproduce
4. Open a new issue at the project repository.

For security vulnerabilities, see `docs/security.md`.
