# codex-telegram-bot

A Telegram bot that forwards user messages to the local `codex` CLI and returns the response back in chat.

It is designed for private/self-hosted use, with optional allowlist controls to prevent unauthorized usage.

## Features

- Runs as a local polling Telegram bot
- Forwards text prompts to `codex exec`
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

## Quick Start

```bash
git clone git@github.com:<your-username>/codex-telegram-bot.git
cd codex-telegram-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

On first run, the bot will prompt you for:

- `TELEGRAM_BOT_TOKEN`
- `ALLOWLIST` (comma-separated Telegram user IDs)

It writes values into `.env` in the project directory.

## Configuration

You can configure using environment variables or `.env` file.

- `TELEGRAM_BOT_TOKEN` (required)
- `ALLOWLIST` (optional): comma-separated numeric Telegram user IDs

If `ALLOWLIST` is empty, anyone who can message the bot can use it.

## Security Notes

- Prefer setting `ALLOWLIST` to your own Telegram user ID(s).
- Keep `.env` private and never commit it.
- This bot executes local CLI calls; run it only on machines you trust.
- Consider running it under a restricted Unix user.

## Operational Notes

- Max incoming prompt length: `6000` chars
- Reply chunk size: `3800` chars per Telegram message
- Codex timeout: `60s`

## Admin Commands

- `/status`: shows Codex version, working directory, allowlist mode
- `/reinstall`: clears stored token and restarts for onboarding
- `/purge`: removes `.env` and restarts
- `/restart`: immediate process restart

## Run as a Service (systemd example)

```ini
[Unit]
Description=Codex Telegram Bot
After=network.target

[Service]
Type=simple
User=<linux-user>
WorkingDirectory=/opt/codex-telegram-bot
ExecStart=/opt/codex-telegram-bot/.venv/bin/python /opt/codex-telegram-bot/bot.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

## Troubleshooting

- `Error: codex CLI not found.`
  - Ensure `codex` is installed and available in `PATH` for the runtime user.
- Bot does not respond in Telegram
  - Verify token correctness
  - Verify allowlist includes your Telegram user ID
  - Check stderr logs for handler exceptions

## License

MIT
