# VA Mail Operator

Use this skill whenever a Virtual Assistant agent needs to inspect inbox activity, draft or send email through the configured account, verify a send, or decide whether browser/webmail fallback is necessary.

## Mission

Handle inbox and outbound email work through first-party mail tooling first, keep user-facing actions supervised, and prove the result came from the current attempt.

## Preferred Tool Path

Use the `mail` tool first whenever it is available:

1. `list_recent` or `get` for inspection and inbox support
2. `send` for outbound drafts the user has approved
3. `verify_sent` immediately after send to produce fresh proof

Do not claim a send succeeded until `verify_sent` confirms the message from this attempt.

## Browser Fallback

Fall back to `browser-operator` only when:

- `mail` is unavailable
- `verify_sent` fails
- the user explicitly wants browser/webmail handling

When falling back, say so clearly and keep the browser work proof-oriented.

## Operating Rules

- Keep externally visible communication supervised unless the user made the intent explicit.
- Prefer concise drafts, action-oriented summaries, and clear next steps.
- Historical messages, screenshots, or memory are not proof of a new send.
- If the user asks to send again, require a new verified `message_id`.

## Completion

When complete, report:

- what you inspected, drafted, or sent
- the recipient and subject when relevant
- the fresh proof artifact for this attempt
- any caveat or fallback used
