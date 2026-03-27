# OpenAI Codex Compliance Notes

Last verified: **March 27, 2026**.

## What OpenAI officially supports

These statements come from official OpenAI docs:

- OpenAI documents Codex authentication at <https://developers.openai.com/codex/auth>.
- The page says Codex supports two sign-in methods when using OpenAI models:
  - Sign in with ChatGPT for subscription access
  - Sign in with an API key for usage-based access
- The same page says ChatGPT sign-in applies when using the Codex app, CLI, or IDE extension.
- OpenAI also documents the Codex SDK at <https://developers.openai.com/codex/sdk> and says it can be used to integrate Codex into your own application.
- The Codex CLI page at <https://developers.openai.com/codex/cli> says the CLI prompts the user to authenticate with a ChatGPT account or an API key.

## Compliance stance in Agent HQ

To minimize policy risk, Agent HQ treats ChatGPT-based Codex access as compliant only when the login happens through OpenAI's official Codex client surface.

That means:

- Allowed: user logs in with `codex login`, and Agent HQ invokes the official `codex` CLI.
- Allowed: user signs in with an API key through the official `codex` CLI.
- Not allowed by default in Agent HQ: direct ChatGPT OAuth flows implemented by Agent HQ itself.
- Not allowed by default in Agent HQ: direct calls to undocumented ChatGPT backend endpoints.

## Resulting product rules

- `openai-codex` in Agent HQ now requires a valid official `codex` CLI session.
- Agent HQ checks `codex login status` before attempting a request.
- Agent HQ uses the official `codex exec` interface for inference.
- Agent HQ defaults the Codex CLI bridge to `read-only` sandbox mode. Override with `ZEROCLAW_CODEX_SANDBOX` only if you intentionally want a broader Codex sandbox.

## Setup

```bash
codex login
codex login status
```

Then configure Agent HQ:

```toml
default_provider = "openai-codex"
default_model = "gpt-5-codex"
```

Optional environment variables:

```bash
export ZEROCLAW_CODEX_CLI_PATH="/custom/path/to/codex"
export ZEROCLAW_CODEX_SANDBOX="read-only"
```
