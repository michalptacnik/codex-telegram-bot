# Agent HQ Desktop

This folder scaffolds the native macOS desktop shell for Agent HQ.

## Current intent

- Wrap the existing Rust runtime and embedded web UI in a desktop host.
- Keep the runtime logic in the main `agent-hq` crate.
- Let the desktop shell own installable app packaging and native window lifecycle.

## Planned usage

```bash
cd desktop
npm install
npm run tauri:dev
```

For production builds:

```bash
cd desktop
npm run tauri:build
```

The current shell exposes a native window and host metadata. Runtime bootstrap and supervision hooks are intentionally isolated here so they can evolve without leaking desktop concerns into the core crate.

## Updater setup

The desktop app is now wired for Tauri's signed updater flow. To build update-capable releases, provide these environment variables before running `npm run tauri:build`:

```bash
export AGENT_HQ_UPDATER_ENDPOINTS="https://releases.example.com/latest.json"
export AGENT_HQ_UPDATER_PUBKEY="$(cat ~/.tauri/publickey.pem)"
```

- `AGENT_HQ_UPDATER_ENDPOINTS`: comma-separated update feed URLs. These can use Tauri variables like `{{target}}`, `{{arch}}`, and `{{current_version}}`.
- `AGENT_HQ_UPDATER_PUBKEY`: contents of the Tauri updater public key, not a file path.

The app will compile and run without those variables, but the in-app updater will report that updates are not configured.

For signed release automation, you should also enable Tauri updater artifact generation in your release pipeline together with the Tauri signing key. That is intentionally not forced in the default local build config so ordinary desktop builds stay healthy.

For CI/CD, this repo now includes a macOS release workflow at [.github/workflows/desktop-release.yml](/Users/michalptacnik/Coding/AgentHQ/.github/workflows/desktop-release.yml). It expects these GitHub settings:

- Secret `AGENT_HQ_UPDATER_PUBKEY`
- Secret `TAURI_SIGNING_PRIVATE_KEY`
- Secret `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`
- Variable `AGENT_HQ_UPDATER_ENDPOINTS` (optional; defaults to the repo's GitHub Releases `latest.json` URL when omitted)

You can also produce a signed updater build locally with:

```bash
cd desktop
export AGENT_HQ_UPDATER_PUBKEY="$(cat ~/.tauri/publickey.pem)"
export AGENT_HQ_UPDATER_ENDPOINTS="https://github.com/<owner>/<repo>/releases/latest/download/latest.json"
export TAURI_SIGNING_PRIVATE_KEY="$(cat ~/.tauri/privatekey.pem)"
export TAURI_SIGNING_PRIVATE_KEY_PASSWORD="..."
npm run tauri:build:update
```
