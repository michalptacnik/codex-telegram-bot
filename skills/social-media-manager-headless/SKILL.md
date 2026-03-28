# Social Media Manager Headless

Use this skill when the user explicitly wants the headless-first social stack or asks to use the new social automation pipeline.

This skill is the same operational model as:
`skills/social-media-manager/SKILL.md`

## Routing

- X / Twitter: `browser_headless` -> `browser_ext`
- LinkedIn: `browser_headless` -> `browser_ext`
- Instagram: future direct adapter -> `browser_headless` -> `browser_ext`
- Threads: future direct adapter -> `browser_headless` -> `browser_ext`

For X:
- call `browser_headless` with `action=status`, `platform=x` before authenticated X browser work
- when `browser_headless status` returns an authenticated session, reuse that exact `session` value for every later X `browser_headless` action in the task
- never create a fresh arbitrary headless session for authenticated X work after status has already identified the working session
- if headless says X is unauthenticated:
  ask the user for the one-time setup named in the status
  prefer `browser_headless` with `action=import_x_session_from_chrome` when the user is already logged into X in Google Chrome
  use `browser_headless` with `action=bootstrap_x_session_interactive` only if Chrome-session import is unavailable or fails
  after setup, re-check `browser_headless` with `action=status`, `platform=x`
- only use `browser_ext` when direct and headless are truthfully unavailable or blocked

## Ground Rules

- Draft first, ask before posting
- Require fresh proof from the current attempt
- Log success only with real proof
- Use runtime statuses in failure reports, not setup guesses
- When setup is needed, tell the user the exact next action:
  keep Google Chrome signed into X, then let you import the Chrome X session
  or complete the one-time interactive bootstrap if Chrome import fails
- After 3 meaningful failures, stop and report the bug instead of looping

## X Article Flow

When the task is to create or publish an X Article through `browser_headless`:

- use the authenticated X session returned by `browser_headless` `action=status`, `platform=x`
- open `https://x.com/compose/articles`
- if the articles landing page shows `Write`, click `[data-testid="empty_state_button_text"]`
- if already on a draft editor URL like `https://x.com/compose/articles/edit/...`, stay on that draft
- set the title with:
  `selector="textarea[placeholder=\"Add a title\"]"`
- set the body with:
  `selector="[data-testid=\"composer\"]"`
- after typing, verify article state with `snapshot` or simple `run_script` checks, not custom pseudo-selectors
- for publish readiness, do not use selectors like `:has-text(...)` inside `document.querySelector`
- prefer either:
  - `click` on a visible button whose text is `Publish`, or
  - a `run_script` that scans `document.querySelectorAll("button")` and matches `textContent.trim() === "Publish"`
- verify the title is no longer empty and the body editor contains the requested body text before clicking `Publish`
- after clicking `Publish`, wait briefly and verify the resulting URL and visible page state before reporting success
- if the article is published via a post/status wrapper instead of a standalone article URL, return the verified public proof URL you can actually open

## Legacy Fallback

If the headless-first path is blocked by capability, policy, challenge, or session issues, switch to:
`skills/social-media-manager-legacy/SKILL.md`
