# codex-telegram-bot

A Telegram bot that forwards user messages to the local `codex` CLI and returns the response back in chat.

Designed for private/self-hosted use, with an optional allowlist to prevent unauthorized usage.

## Features

- Runs as a local polling Telegram bot
- Forwards text prompts to `codex exec` (non-interactive)
- Streams back long output in multiple Telegram messages
- Built-in onboarding flow for token + allowlist
- Safety guardrails:
  - Input length cap
  - Output chunking
  - Secret redaction pattern for `sk-*` tokens
  - Optional user allowlist
- Admin commands:
  - `/ping`
  - `/status`
  - `/reset`
  - `/reinstall`
  - `/purge`
  - `/restart`

## Architecture

`Telegram -> python-telegram-bot handlers -> local codex subprocess -> Telegram reply`

The bot is stateless by default. It executes each incoming message as an independent `codex exec` call.

## Requirements

- Python 3.10+
- `codex` CLI installed and accessible in `PATH`
- Telegram bot token from `@BotFather`

## Install

Dev install:

```bash
pip install -e .
```

Local install:

```bash
pip install .
```

## Run

```bash
codex-telegram-bot
```

## Config

By default, config is stored in:

```
~/.config/codex-telegram-bot/.env
```

You can override it with:

```bash
codex-telegram-bot --config-dir /path/to/config
```

On first run, the bot prompts you for:

- `TELEGRAM_BOT_TOKEN`
- `ALLOWLIST` (comma-separated Telegram user IDs)

If `ALLOWLIST` is blank, the bot will warn and require you to type `YES` to continue.

Environment variables override `.env`:

- `TELEGRAM_BOT_TOKEN` (required)
- `ALLOWLIST` (optional)
- `LOG_LEVEL` (default: INFO)

Print active config summary (never prints token):

```bash
codex-telegram-bot --print-config
```

## Service (systemd)

Recommended (user service):

```bash
./scripts/install_service.sh --user --workdir /path/to/repo
systemctl --user enable --now codex-telegram-bot
```

System-wide:

```bash
sudo ./scripts/install_service.sh --system --workdir /opt/codex-telegram-bot
sudo systemctl enable --now codex-telegram-bot
```

## Docker (optional)

The container expects `codex` to be available. The simplest approach is to mount the host binary into the container.

```bash
docker compose up --build
```

If your `codex` binary is in a different location, update the volume in `docker-compose.yml`.

## Admin Commands

- `/status`: shows Codex version, working directory, allowlist mode
- `/reinstall`: clears stored token and restarts for onboarding
- `/purge`: removes `.env` and restarts
- `/restart`: immediate process restart

## Troubleshooting

- `Error: codex CLI not found.`
  - Ensure `codex` is installed and available in `PATH` for the runtime user.
- Bot does not respond in Telegram
  - Verify token correctness
  - Verify allowlist includes your Telegram user ID
  - Check stderr logs for handler exceptions

## License

MIT
