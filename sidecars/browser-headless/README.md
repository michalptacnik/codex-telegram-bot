# Browser Headless Sidecar

This sidecar provides the `browser_headless` tool surface for AgentHQ.

It is built around:

- Playwright for deterministic browser control, persistent profiles, screenshots, and traces
- Stagehand as the planned high-level browser reasoning layer behind the same stdio protocol

## Install

```bash
cd sidecars/browser-headless
npm install
```

## Run directly

```bash
node server.mjs
```

The Rust `browser_headless` tool spawns this process automatically via stdio.

## State

By default the sidecar stores persistent profiles, screenshots, and traces under:

`$ZEROCLAW_HEADLESS_STATE_DIR`

If that variable is not set, it falls back to:

`<repo>/sidecars/browser-headless/.state`

## Current status

- Playwright-backed deterministic actions are implemented now
- Stagehand dependency is wired in for the same sidecar surface, but advanced `act` / `extract` flows are intentionally deferred until provider/env plumbing lands in Rust config
