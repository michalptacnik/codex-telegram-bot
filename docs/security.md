# Security Hardening Guide

This document covers the threat model, hardening checklist, and known limitations
for `codex-telegram-bot`.

---

## Threat Model

### Assets

| Asset | Sensitivity |
|-------|-------------|
| Telegram bot token | Critical — allows impersonating the bot |
| OPENAI/Anthropic API keys | High — billing impact |
| Workspace files | High — agent has read/write access |
| Control Center API | Medium — exposes run history, can queue prompts |
| SQLite state DB | Medium — session and run history |

### Trust Boundaries

1. **Telegram ↔ Bot**: Messages arrive from Telegram servers. User identity is
   validated by Telegram. The bot trusts `user_id` and `chat_id` from PTB.

2. **Bot ↔ codex CLI**: The bot spawns `codex exec` as a subprocess. The CLI
   inherits the bot process's environment and workspace root.

3. **Bot ↔ Control Center**: The Control Center runs as part of the same process.
   API endpoints are optionally protected by `LOCAL_API_KEYS`.

4. **Workspace isolation**: Each session gets an isolated sub-directory under
   `SESSION_WORKSPACES_ROOT`. The `WorkspaceManager` enforces disk quotas.

### Threat Categories

#### T1: Unauthorized Control Center access (SSRF / direct access)
- **Risk**: Attacker accesses `/api/*` endpoints or UI pages to read run history
  or queue prompts.
- **Mitigation**: Set `LOCAL_API_KEYS` to protect all `/api/*` endpoints.  Set
  `CONTROL_CENTER_UI_SECRET` to require a login secret for all HTML UI pages
  (`/`, `/runs`, `/sessions`, etc.).  Use a TLS-terminating reverse proxy when
  exposing the Control Center outside localhost.

#### T2: Secret leakage via prompts
- **Risk**: User sends a prompt containing API keys or credentials; agent echoes them.
- **Mitigation**: `AccessController.scan_for_secrets()` scans outbound content.
  Redaction patterns are applied by `util.redact()` before logging.

#### T3: Workspace escape
- **Risk**: Agent writes to files outside its allocated session workspace.
- **Mitigation**: `WorkspaceManager` enforces per-session directories. Policy
  profiles (`strict`/`balanced`/`trusted`) control which shell operations are
  allowed. Use `strict` profile for untrusted users.

#### T4: Privilege escalation via tool approval
- **Risk**: Attacker approves high-risk tool actions via Control Center API.
- **Mitigation**: POST endpoints for approval require `api:write` scope when
  `LOCAL_API_KEYS` is configured. Pending approvals have a configurable TTL
  (`APPROVAL_TTL_SEC`, default 900 s).

#### T5: Spend abuse
- **Risk**: Malicious user triggers many expensive runs.
- **Mitigation**: `AccessController` enforces per-user daily spend ceilings
  (`spend_limit_usd`, default $10/day). Role-based action gating prevents
  `viewer`-role users from queuing prompts.

---

## Hardening Checklist

### Network

- [ ] Control Center not exposed to the internet without TLS
- [ ] `LOCAL_API_KEYS` set with strong random tokens (≥ 32 chars)
- [ ] Reverse proxy enforces HTTPS with HSTS
- [ ] Consider firewall rule: Control Center port accessible only from trusted IPs

### Secrets management

- [ ] `TELEGRAM_BOT_TOKEN` stored in environment/secrets manager, not in code
- [ ] API keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) not committed to source
- [ ] `.env` file excluded from version control (`.gitignore`)
- [ ] Secret scanning patterns active (verify via `/api/onboarding/readiness`)

### Workspace isolation

- [ ] `EXECUTION_WORKSPACE_ROOT` is a dedicated directory, not `/` or home
- [ ] `SESSION_WORKSPACES_ROOT` is separate from source code
- [ ] `WORKSPACE_MAX_DISK_BYTES` set to appropriate limit
- [ ] Bot process runs as a non-root, non-sudo user

### Access control

- [ ] Default policy profile is `balanced` or `strict` for new agents
- [ ] Admin roles assigned only to trusted operators
- [ ] `approve_tool` action requires at least `user` role
- [ ] `switch_provider` and `manage_agents` restricted to `admin` role

### Dependency hygiene

- [ ] `pip audit` or `safety check` run before deployment
- [ ] Dependencies pinned in production
- [ ] Codex CLI version pinned and verified

---

## Known Limitations

1. **No end-to-end encryption for Telegram messages.** Telegram messages are
   plaintext on Telegram's servers. Use Telegram Secret Chats for sensitive
   communications (the bot cannot access those).

2. **Codex CLI runs with bot process permissions.** If the bot has broad filesystem
   access, so does the agent. Restrict workspace root and use policy profiles.

3. **Control Center UI auth is optional.** Set `CONTROL_CENTER_UI_SECRET` to require
   a login secret for all HTML pages (`/`, `/runs`, etc.).  When not set (local/dev
   mode), UI pages are served without auth — use network-level controls (firewall,
   VPN) to restrict access to the Control Center port.  The `/api/*` endpoints are
   protected when `LOCAL_API_KEYS` is configured.

4. **SQLite is not encrypted at rest.** Use full-disk encryption (LUKS/BitLocker)
   if the state DB contains sensitive data.

5. **Approval TTL is not a security boundary.** A pending approval that expires is
   auto-denied, but approval IDs are not cryptographically signed. The approval
   flow relies on the Control Center being accessible only to authorized operators.

---

## Reporting Vulnerabilities

Open an issue tagged `security` in the project repository.  For sensitive
disclosures, contact the maintainer directly via the contact information in
`README.md`.
