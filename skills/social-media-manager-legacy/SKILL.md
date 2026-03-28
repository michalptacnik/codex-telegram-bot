# Social Media Manager Legacy

Use this skill only when the headless-first social stack is blocked and the live browser extension path is required.

Primary default skill:
`skills/social-media-manager/SKILL.md`

## Transport Order

For X / Twitter use:
1. `browser_ext`
2. `browser_open` only to open a URL, never to automate

For authenticated social posting on the live browser:
- use `browser_ext`, not legacy `browser`
- use the preferred connected browser for the account when one is recorded
- stop after 3 meaningful failures and report the bug instead of looping

## X Compose Rules

### New post

1. `action=open_url, url=https://x.com/home`
2. `action=click, selector=[data-testid="SideNav_NewTweet_Button"]`
3. If that fails, open `https://x.com/compose/post`
4. Use `action=type, selector=[data-testid="tweetTextarea_0"], text=<post>, replace=true`
5. Verify intended text is present
6. Click the enabled dedicated compose submit control:
   `[data-testid="tweetButton"]`
7. Wait briefly, then recover fresh proof

### Reply / comment

1. Open the target post URL
2. Prefer direct selectors before snapshot
3. Click `selector=[data-testid="reply"]`
4. If X moves to `https://x.com/compose/post`, keep treating it as dedicated compose
5. Use `action=type, selector=[data-testid="tweetTextarea_0"], text=<reply>, replace=true`
6. Never use `fill` for X reply editors
7. Click the enabled submit control:
   - dedicated compose: `[data-testid="tweetButton"]`
   - inline composer: `[data-testid="tweetButtonInline"]`
8. Recover fresh proof from the actual reply text and thread, not the parent post URL

### X Article

1. Open `https://x.com/compose/articles`
2. Click `Write` when needed with `[data-testid="empty_state_button_text"]`
3. Type title with `textarea[placeholder="Add a title"]`
4. Type body with `[data-testid="composer"]`
5. Verify title and body contain the intended content
6. If approved, click the real visible `Publish` control
7. Return a fresh public proof URL only after verification

## Proof Rule

Never claim success without a fresh current-attempt proof URL.

For replies:
- do not accept the parent tweet URL
- search the thread or `/with_replies` for the exact reply text
- return only the reply’s own `/status/` URL

## Logging Rule

Append success only with real proof to:
`~/.zeroclaw/workspace/social-media-manager/performance-log.md`

If posting fails or proof cannot be established, write a failure/debug note instead of a fake success row.
