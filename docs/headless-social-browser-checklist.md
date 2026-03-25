# Headless Social Browser Implementation Checklist

Branch: `codex/headless-social-browser`

## 1. Shared Headless Browser Foundation

- [ ] Create a dedicated Node sidecar at `sidecars/browser-headless/`
- [ ] Add `package.json` there with `stagehand` and `playwright` dependencies
- [ ] Implement a stdio JSON command server there so Rust can call it without inventing a separate HTTP stack
- [ ] Expose deterministic operations:
  - `open_url`
  - `snapshot`
  - `act`
  - `extract`
  - `type`
  - `click`
  - `wait_for`
  - `run_script`
  - `get_text`
  - `save_trace`
  - `save_screenshot`
- [ ] Store session/auth artifacts in workspace state, not in git
- [ ] Add a sidecar README with install, auth-state, and trace-debug steps

## 2. Rust Tool Integration

- [ ] Add new tool module `src/tools/browser_headless.rs`
- [ ] Add any helper runtime/process wrapper needed for spawning and talking to the sidecar
- [ ] Register the tool in [src/tools/mod.rs](/Users/michalptacnik/Coding/AgentHQ/src/tools/mod.rs)
- [ ] Add config schema for headless browser sidecar settings in [src/config/schema.rs](/Users/michalptacnik/Coding/AgentHQ/src/config/schema.rs)
- [ ] Wire config loading/defaults in the config module
- [ ] Add unit tests for tool schema, config parsing, and command argument validation

## 3. Global Browser Routing

- [ ] Update [src/agent/loop_.rs](/Users/michalptacnik/Coding/AgentHQ/src/agent/loop_.rs) so all agents prefer:
  1. app/platform adapter
  2. `browser_headless`
  3. `browser_ext`
  4. legacy `browser`
- [ ] Update the tool descriptions shown to the model so `browser_headless` is clearly the primary browser path
- [ ] Keep `browser_ext` as the fallback for logged-in live-session recovery and policy/captcha blocks
- [ ] Add tests for tool-priority instructions and social-task routing text

## 4. X Pilot Adapter

- [ ] Add a sidecar wrapper for `twitter-client-mcp`
- [ ] Choose the cleanest integration shape:
  - direct subprocess wrapper from Rust, or
  - MCP-compatible adapter layer if we want to generalize later
- [ ] Expose X operations needed by the social skill:
  - create post
  - create reply/comment
  - search/find candidates
  - fetch proof URL / post metadata
- [ ] Route X social tasks as:
  1. `twitter-client-mcp`
  2. `browser_headless`
  3. `browser_ext`
- [ ] Add proof-first tests so success is never claimed without a real URL/artifact

## 5. Shared Social Adapter Shape

- [ ] Define one internal adapter contract for social platforms
- [ ] Keep it generic enough for X, LinkedIn, Instagram, and Threads
- [ ] Proposed operations:
  - `prepare_session`
  - `create_post`
  - `create_comment`
  - `create_reply`
  - `create_article`
  - `find_candidates`
  - `get_proof`
  - `report_failure_mode`
- [ ] Do not hardwire X-specific assumptions into the shared interface

## 6. LinkedIn Structure

- [ ] Do not ship a direct LinkedIn publishing adapter yet unless a stronger OSS option is validated during implementation
- [ ] Build the routing slot now so LinkedIn can use:
  1. `browser_headless`
  2. `browser_ext`
- [ ] Keep research references for LinkedIn stabilization:
  - `linkedin-private-api`
  - `linkedin-api` / Voyager wrappers
  - `linkedln-bot` for flow/selectors reference only
- [ ] Add LinkedIn-specific proof rules before claiming the pilot is done

## 7. Skill Layer

- [ ] Create a shared browser skill for all agents that instructs them to prefer `browser_headless`
- [ ] Keep the current social media skill preserved as legacy
- [ ] Create `social-media-manager-headless` as the new headless-first orchestrator
- [ ] Route platform behavior inside that skill as:
  - X: `twitter-client-mcp` -> `browser_headless` -> `browser_ext`
  - LinkedIn: `browser_headless` -> `browser_ext`
  - Instagram: future direct adapter -> `browser_headless` -> `browser_ext`
  - Threads: future direct adapter -> `browser_headless` -> `browser_ext`

## 8. Verification

- [ ] Sidecar smoke test: open page, snapshot, click, type, save trace
- [ ] Rust integration smoke test: sidecar spawn + one successful command round-trip
- [ ] X end-to-end smoke test through the new priority order
- [ ] Fallback test: fail X adapter deliberately and verify automatic fallback to `browser_headless`
- [ ] Fallback test: fail `browser_headless` deliberately and verify automatic fallback to `browser_ext`
- [ ] Keep unrelated untracked files out of the branch

## 9. First Build Order

- [ ] Land `browser_headless` sidecar + Rust tool first
- [ ] Land global routing changes second
- [ ] Land X adapter third
- [ ] Land skill updates fourth
- [ ] Run real end-to-end verification only after all four are in place
