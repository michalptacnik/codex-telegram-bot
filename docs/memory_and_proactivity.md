# Memory, Proactivity, Marketplace, and Attachments Design

This document defines implementation guardrails for:

- Thin persistent memory index + on-demand markdown pages.
- Proactive heartbeat and task surfacing.
- Skill marketplace search/install/enable flow.
- Telegram inbound/outbound attachments as first-class artifacts.

## 1. Thin Memory Index

### Objective

Always inject a tiny memory summary into prompts without bloating context.

### Layout

Per session workspace (or configured state root):

- `memory/MEMORY_INDEX.md` (always loaded, capped)
- `memory/daily/YYYY-MM-DD.md` (append-only daily log)
- `memory/pages/**/*.md` (curated detail pages)

### Format

`MEMORY_INDEX.md` is stable and machine-parseable:

```md
# MEMORY_INDEX v1
## Identity
- preferred_name: ...
- timezone: ...

## Active Projects (max 10)
- P001 | Name | memory/pages/projects/name.md

## Obligations (max 20)
- O001 | Task | due: YYYY-MM-DD | ref: memory/pages/tasks.md#anchor

## Preferences (max 15)
- key: value

## Pointers (max 50)
- P001 -> memory/pages/projects/name.md
- DYYYY-MM-DD -> memory/daily/YYYY-MM-DD.md
```

### Invariants

- Always-loaded index must stay under strict budget (`MEMORY_INDEX_MAX_CHARS`, default 8000).
- Heavy pages and daily logs are never injected by default.
- Detailed memory is loaded only through explicit tool calls (`memory_pointer_open`, etc.).
- Pointer targets are confined to the session `memory/` subtree.

## 2. Memory Pointer Resolution

### Objective

Resolve IDs to files and anchors with path-safe reads.

### Rules

- Pointer IDs are declared only in the `Pointers` section.
- Resolution supports optional markdown anchor fragments.
- Reads are bounded by max chars and normalized via workspace root checks.
- Path traversal (`..`, absolute escapes) is rejected.

## 3. Proactive Heartbeat

### Objective

Run periodic, cheap checks and proactively message the user when useful.

### File

- `memory/HEARTBEAT.md` with sections for daily/weekly/monitors/waiting/quiet-hours.

### Delivery Path

1. Scheduler tick (30-60 minutes).
2. Select heartbeat-enabled sessions.
3. Enforce quiet hours in user timezone.
4. Enforce spend ceilings before sending.
5. Run low-cost probe:
   - `NO_ACTION`
   - `ACTION message`
   - `ACTION mission` (optional)
6. Deliver through proactive messenger transport (`send_message` path).

### Invariants

- Heartbeat is opt-in per user/session.
- Quiet hours are enforced before any proactive send.
- Proactive actions obey access control and spend ceilings.
- Every proactive action is auditable (run/event trail).

## 4. Skill Marketplace Model

### Objective

Support discover/install/enable from remote catalogs with safe defaults.

### Source Model

- Configured catalog sources (GitHub repo indexes, optional URL indexes).
- SQLite cache for quick search and bounded refresh cadence.

### Install Model

- Instruction-only skills first (SKILL.md + assets).
- Install targets:
  - workspace packs
  - global packs
- Precedence remains `workspace > global > bundled`.

### Verification

- Compute/store SHA-256 of SKILL.md and assets on install.
- Re-verify hashes on enable.
- Optional trusted publisher signatures shown as verification metadata.

### Progressive Disclosure

- Routing uses compact metadata only.
- Full SKILL.md content loads only when selected/activated.
- Prompt contract forbids dumping all skills by default.

## 5. Telegram Attachments

### Objective

Treat files as session-scoped artifacts tied to messages and usable by tools.

### Inbound Model

- Accept document/photo/audio/video.
- Download via Telegram API.
- Store under session workspace (for example: `attachments/<message-id>/...`).
- Sanitize filename and enforce limits.
- Record DB rows linking message and attachment.
- Inject compact receipt text into the turn context (filename/mime/size/hash/path).

### Outbound Model

- `send_file` tool validates workspace-bound path and existence.
- Sends via Telegram (`sendDocument`/`sendPhoto`/`sendVideo`/`sendAudio`).
- Records outbound message + attachment rows for traceability.

### Invariants

- Paths are normalized under allowed roots.
- File size and attachment-count caps are enforced.
- No auto-open/auto-exec of uploaded content.
- Attachment download APIs enforce auth and session ownership checks.
