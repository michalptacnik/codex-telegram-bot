# Browser Operator

Use this skill whenever the user asks AgentHQ to do browser work: browse a site, click through a web flow, fill forms, work inside a logged-in app, inspect a page, publish in a CMS, or "just use the browser." Trigger aggressively even when the user does not say "browser skill."

## Mission

Execute browser tasks reliably, headless-first, and prove the result came from the current attempt.

## Tool Order

For browser work, prefer tools in this order:

1. `browser_headless`
2. `browser_ext`
3. legacy `browser`
4. `browser_open` only to open a URL, never to automate

Use `browser_ext` before `browser_headless` only when the user explicitly wants the live browser session or the task depends on the user's existing logged-in browser state and the headless path is blocked.

## Fixed Workflow

1. State the target outcome, the proof you will require, and the estimated tool turns.
2. If the estimate is greater than `12`, ask for permission before acting.
3. Start with the smallest proof-oriented action that can confirm the browser state.
4. Reuse the same `session` value across `browser_headless` calls in one task.
5. If a flow becomes flaky, save a trace or screenshot before changing strategy.
6. If you reach the estimate without fresh proof, stop, explain why, give a revised estimate, and ask whether to continue.

## Headless Recovery Ladder

Prefer the first successful step in this order:

1. `browser_headless` with `open_url`
2. `browser_headless` with `snapshot`
3. `browser_headless` with deterministic `click`, `type`, `get_text`, `run_script`, `wait_for`
4. `browser_headless` with `save_screenshot` or `save_trace`
5. `browser_ext` to recover in the live browser
6. legacy `browser` only if the headless and extension paths are both unavailable or clearly blocked

Always reacquire a fresh page map after navigation, reloads, or failed clicks.

## Fresh-Proof Rule

Historical context, old chat turns, memory recalls, and previous successes are never proof for the current attempt.

For every externally visible action, require a fresh artifact from this attempt:

- Prefer native identifiers such as URL, ID, receipt, order number, post URL, comment URL, CMS URL, or message URL.
- Use `browser_headless save_screenshot` or `save_trace` when the site offers no stronger identifier or when debugging is required.
- If the user says "again," require a second fresh artifact. Never reuse an earlier post, draft, screenshot, or trace as proof of a new attempt.

## Social And Webmail Rules

- For social posting, comments, replies, and publishing, use the proven platform path first.
- For authenticated X browser work, use `browser_headless action=status` first and `bootstrap_x_session` when the saved X session is missing or expired.
- For X/Twitter, use authenticated `browser_headless` first, then `browser_ext`.
- For browser webmail, only use this skill when the `mail` tool is unavailable, verification fails, or the user explicitly wants browser/webmail handling.
- When a button or flow changes, stay in the same class of solution: headless DOM recovery first, then trace/screenshot, then live browser fallback.

## Response Style

Keep action updates short and factual.

When complete, report:

- what you did
- the fresh proof artifact
- any caveat or fallback used
