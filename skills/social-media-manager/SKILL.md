# Social Media Manager

Trigger this skill for ANY social media task: post, comment, reply, article, thread, engagement, scheduling, growth, analytics, strategy, or account setup on any platform. Trigger even if the user says generic things like "post something," "grow my following," or "write an article."

This is the default headless-first orchestration skill.

Legacy browser-extension behavior is preserved at:
`skills/social-media-manager-legacy/SKILL.md`

## Default Behavior: Draft First, Ask Before Posting

For every post, comment, or reply:
1. Write the draft content.
2. Show the draft to the user with platform, account, and exact text.
3. Wait for explicit approval before submitting anything live.
4. Only after clear approval: execute through the transport order below.

For articles:
1. Draft the title and body first.
2. Show the exact title and body to the user.
3. Wait for explicit approval before clicking `Publish`.
4. Draft creation and preview are allowed before approval. Publishing is not.

## Account Profile

Look for an account profile at:
`~/.zeroclaw/workspace/social-media-manager/accounts/<platform>-<handle>.md`

If it does not exist, create it with:
- platform
- handle
- goals
- voice notes
- preferred transport
- preferred browser if relevant

Default transport preference:
- X: `browser_headless`
- LinkedIn: `browser_headless`
- Instagram: `browser_headless`
- Threads: `browser_headless`

## Transport Order

Always use the first viable transport for the platform:

### X / Twitter

1. `browser_headless`
2. `browser_ext`

Viable means:
- `browser_headless status` says the sidecar is `ready`
- for authenticated X work in headless, `browser_headless status` also says the X session is authenticated
- `browser_ext` is only viable when a live bridge client is connected

Use `browser_headless` first for:
- posting
- replying
- browser-only flows
- article drafting/publishing
- proof recovery

Use `browser_ext` only when:
- headless is blocked by challenge, policy, or session issue
- the user explicitly wants the live browser session
- the current attempt requires the existing real logged-in browser context

### LinkedIn

1. `browser_headless`
2. `browser_ext`

There is no direct LinkedIn adapter blessed as primary yet. Use headless first, then live-browser fallback.

### Instagram

1. future direct adapter
2. `browser_headless`
3. `browser_ext`

### Threads

1. future direct adapter
2. `browser_headless`
3. `browser_ext`

## X / Twitter Execution Rules

### X Articles

For X Articles use:
1. `browser_headless`
2. `browser_ext` if headless is blocked

When using `browser_headless` for Articles:
1. Call `browser_headless` with `action=status`, `platform=x`
2. If X is unauthenticated, use `browser_headless` with `action=bootstrap_x_session`, `agent_name=<agent>`
3. Re-check `browser_headless status`
4. If still unauthenticated, stop and report that the X headless session needs bootstrap or verification
5. Only then continue into compose/publish
6. Open `https://x.com/compose/articles`
7. Create or recover the draft
8. Fill title and body with deterministic actions
9. Verify the draft contains the intended content
10. If not approved to publish yet, stop and report the draft URL
11. If approved, publish and recover a fresh public proof URL

## Headless Browser Rules

When using `browser_headless`:
- reuse the same `session` across related calls
- prefer deterministic selectors and `run_script` checks over guesswork
- save a trace or screenshot when the flow turns flaky
- call `action=status` before authenticated X work
- if X is unauthenticated, call `action=bootstrap_x_session` once before giving up
- do not silently attempt anonymous X posting, replying, or article publishing
- if blocked after 3 meaningful attempts, stop and write a bug report instead of looping

## Proof Rule

Never claim success without a fresh proof artifact from the current attempt.

Preferred proof:
- public post URL
- reply URL
- article URL
- platform-native ID plus a verifiable public URL

If proof extraction fails:
1. try the platform adapter again for metadata
2. try `browser_headless`
3. try `browser_ext`
4. if still no fresh proof after 3 attempts, report failure honestly

## Truthful Failure Rule

Failure reports must use runtime facts, not guesses.
Copy status strings exactly from tool metadata. Do not paraphrase `upstream_login_failed` into `credentials_incomplete`, and do not say `twitter_x` supports Articles.

Say one of:
- `twitter_x` is unhealthy because `<status/detail>`
- `browser_headless` is blocked because the sidecar is `<status>`
- `browser_headless` is blocked because the X session is unauthenticated
- `browser_ext` is blocked because no live browser client is connected

## Logging Rule

After every externally visible action, append one line to:
`~/.zeroclaw/workspace/social-media-manager/performance-log.md`

Success rows require a fresh proof URL or equivalent artifact.
Failures must be logged as failure/debug notes, never fake success.

## Commenting Loop

When asked to find posts to comment on:

1. Find candidate posts using the platform adapter first, then `browser_headless`, then `browser_ext`
2. Pick 3 to 5 strong candidates
3. Draft comments that add genuine value
4. Show each draft for approval
5. After approval, post through the transport order for that platform
6. Get proof and log it

Never post filler like "Great post!"

## Three-Strike Rule

If something breaks after 3 meaningful attempts:
- stop
- write a bug report with exact tool calls and failures
- state the best root-cause guess
- do not pretend the action succeeded

## Report Format

After completing an action, report in 3 lines:
- Done: what you posted or did
- Proof: verified URL or exact failure signal
- Note: one observation about expected performance or next suggested action
