# AgentHQ Chrome Bridge Extension

This extension connects your local Chrome session to AgentHQ Control Center so agent tools can open/navigate tabs and run in-tab script actions.

## Install (unpacked)

1. Open `chrome://extensions` in Chrome.
2. Enable **Developer mode**.
3. Click **Load unpacked** and select this folder:
   - `chrome-extension/`
4. Open the extension popup.
5. Set **Control Center URL** (default `http://127.0.0.1:8765`).
6. If backend uses `BROWSER_EXTENSION_TOKEN`, paste it into **Bridge token**.
7. Keep **Bridge enabled** checked.
8. Click **Save** then **Ping now**.

When connected, the popup status shows `active`, the extension icon badge shows `ON`, and Control Center `/chat` shows a connected Chrome client.

## Backend requirements

- Run Control Center (`--control-center`) from this repo.
- Browser bridge API endpoints are served at:
  - `POST /api/browser/extension/register`
  - `POST /api/browser/extension/heartbeat`
  - `GET /api/browser/extension/commands`
  - `POST /api/browser/extension/commands/{command_id}/result`
- Optional auth:
  - set env `BROWSER_EXTENSION_TOKEN=<secret>`
  - extension sends it as header `x-browser-extension-token`
- Optional redispatch tuning:
  - set env `BROWSER_EXTENSION_DISPATCH_LEASE_SEC=<seconds>` (default `30`) to control when unacknowledged commands are re-dispatched.

## Supported command types

- `open_url` -> opens URL in new/existing tab
- `navigate_url` -> navigates current active tab
- `run_script` -> executes JavaScript in active/specified tab via `chrome.scripting`

These are triggered by agent tools:

- `browser_open`
- `browser_navigate`
- `browser_script`
- `browser_status`
