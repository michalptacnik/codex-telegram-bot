#!/usr/bin/env bash
set -euo pipefail

PROJECT_TITLE="Lean Agent Runtime"

log() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

jq_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"
  printf '%s' "$s"
}

require_gh() {
  command -v gh >/dev/null 2>&1 || die "gh is required. Install GitHub CLI first."
}

preflight_auth() {
  local auth_out project_err
  if ! auth_out="$(gh auth status 2>&1)"; then
    printf '%s\n' "$auth_out" >&2
    die "gh auth status failed. Run: gh auth login -h github.com"
  fi

  if ! gh api user --jq '.login' >/dev/null 2>&1; then
    printf '%s\n' "$auth_out" >&2
    die "GitHub auth is not valid. Run: gh auth login -h github.com"
  fi

  if ! project_err="$(gh project list --owner @me --limit 1 2>&1 >/dev/null)"; then
    if printf '%s\n' "$project_err" | grep -qi 'missing required scopes'; then
      printf '%s\n' "$project_err" >&2
      die "Missing project scope. Use a token with read:project (or run: gh auth refresh -s read:project)"
    fi
    printf '%s\n' "$project_err" >&2
    die "Unable to access GitHub Projects API."
  fi
}

detect_repo() {
  local remote
  remote="$(git config --get remote.origin.url || true)"
  [[ -n "$remote" ]] || die "No git remote.origin.url found."

  if [[ "$remote" =~ github\.com[:/]([^/]+)/([^/.]+)(\.git)?$ ]]; then
    OWNER="${BASH_REMATCH[1]}"
    REPO="${BASH_REMATCH[2]}"
  else
    die "Could not parse owner/repo from remote: $remote"
  fi
}

ensure_label() {
  local name="$1"
  local color="$2"
  local description="$3"
  local escaped
  escaped="$(jq_escape "$name")"

  if gh label list -R "$OWNER/$REPO" --limit 200 --json name --jq ".[] | select(.name == \"$escaped\") | .name" | head -n1 | grep -qx "$name"; then
    log "Label exists: $name"
    return
  fi

  gh label create "$name" -R "$OWNER/$REPO" --color "$color" --description "$description" >/dev/null
  log "Created label: $name"
}

ensure_milestone() {
  local title="$1"
  local escaped
  escaped="$(jq_escape "$title")"
  local number
  number="$(gh api "repos/$OWNER/$REPO/milestones?state=all&per_page=100" --paginate --jq ".[] | select(.title == \"$escaped\") | .number" | head -n1 || true)"

  if [[ -n "$number" ]]; then
    log "Milestone exists: $title (#$number)"
    printf '%s\n' "$number"
    return
  fi

  gh api -X POST "repos/$OWNER/$REPO/milestones" -f "title=$title" >/dev/null
  number="$(gh api "repos/$OWNER/$REPO/milestones?state=all&per_page=100" --paginate --jq ".[] | select(.title == \"$escaped\") | .number" | head -n1 || true)"
  [[ -n "$number" ]] || die "Failed to create milestone: $title"
  log "Created milestone: $title (#$number)"
  printf '%s\n' "$number"
}

ensure_project() {
  PROJECT_OWNER="@me"
  PROJECT_OWNER_LOGIN="$(gh api user --jq '.login')"

  local escaped
  escaped="$(jq_escape "$PROJECT_TITLE")"
  PROJECT_NUMBER="$(gh project list --owner "$PROJECT_OWNER" --limit 100 --format json --jq ".[] | select(.title == \"$escaped\") | .number" | head -n1 || true)"

  if [[ -z "$PROJECT_NUMBER" ]]; then
    gh project create --owner "$PROJECT_OWNER" --title "$PROJECT_TITLE" >/dev/null
    PROJECT_NUMBER="$(gh project list --owner "$PROJECT_OWNER" --limit 100 --format json --jq ".[] | select(.title == \"$escaped\") | .number" | head -n1 || true)"
    [[ -n "$PROJECT_NUMBER" ]] || die "Failed to create project: $PROJECT_TITLE"
    log "Created project: $PROJECT_TITLE (#$PROJECT_NUMBER)"
  else
    log "Project exists: $PROJECT_TITLE (#$PROJECT_NUMBER)"
  fi

  PROJECT_ID="$(gh project list --owner "$PROJECT_OWNER" --limit 100 --format json --jq ".[] | select(.number == $PROJECT_NUMBER) | .id" | head -n1 || true)"
  PROJECT_URL="$(gh project list --owner "$PROJECT_OWNER" --limit 100 --format json --jq ".[] | select(.number == $PROJECT_NUMBER) | .url" | head -n1 || true)"
  [[ -n "$PROJECT_ID" ]] || die "Could not resolve project ID."

  gh project link "$PROJECT_NUMBER" --owner "$PROJECT_OWNER" --repo "$OWNER/$REPO" >/dev/null 2>&1 || true
}

get_field_id() {
  local field_name="$1"
  local escaped
  escaped="$(jq_escape "$field_name")"
  gh project field-list "$PROJECT_NUMBER" --owner "$PROJECT_OWNER" --format json --jq ".[] | select(.name == \"$escaped\") | .id" | head -n1 || true
}

get_option_id() {
  local field_name="$1"
  local option_name="$2"
  local f_escaped o_escaped
  f_escaped="$(jq_escape "$field_name")"
  o_escaped="$(jq_escape "$option_name")"
  gh project field-list "$PROJECT_NUMBER" --owner "$PROJECT_OWNER" --format json --jq ".[] | select(.name == \"$f_escaped\") | .options[]? | select(.name == \"$o_escaped\") | .id" | head -n1 || true
}

ensure_single_select_field() {
  local field_name="$1"
  local options_csv="$2"
  local field_id
  field_id="$(get_field_id "$field_name")"

  if [[ -z "$field_id" ]]; then
    gh project field-create "$PROJECT_NUMBER" --owner "$PROJECT_OWNER" --name "$field_name" --data-type "SINGLE_SELECT" --single-select-options "$options_csv" >/dev/null
    log "Created field: $field_name"
    return
  fi

  log "Field exists: $field_name"
  local opt
  IFS=',' read -r -a opts <<<"$options_csv"
  for opt in "${opts[@]}"; do
    if [[ -z "$(get_option_id "$field_name" "$opt")" ]]; then
      warn "Field '$field_name' missing option '$opt'. Add it manually in project settings."
    fi
  done
}

ensure_project_fields() {
  local status_id
  status_id="$(get_field_id "Status")"
  if [[ -z "$status_id" ]]; then
    gh project field-create "$PROJECT_NUMBER" --owner "$PROJECT_OWNER" --name "Status" --data-type "SINGLE_SELECT" --single-select-options "Todo,In Progress,Blocked,Done" >/dev/null
    log "Created field: Status"
  else
    log "Field exists: Status"
    [[ -n "$(get_option_id "Status" "Todo")" ]] || warn "Status option 'Todo' not found."
    [[ -n "$(get_option_id "Status" "In Progress")" ]] || warn "Status option 'In Progress' not found."
    [[ -n "$(get_option_id "Status" "Blocked")" ]] || warn "Status option 'Blocked' not found. Add it manually in project settings."
    [[ -n "$(get_option_id "Status" "Done")" ]] || warn "Status option 'Done' not found."
  fi

  ensure_single_select_field "Epic" "EPIC 1 - Agent Core Foundation,EPIC 2 - Secure Computer Interaction Layer,EPIC 3 - Multi-Provider Architecture,EPIC 4 - Streaming and CLI-like Feedback,EPIC 5 - Lightweight Web Control Center"
  ensure_single_select_field "Priority" "P0,P1,P2"
  ensure_single_select_field "Size" "S,M,L"
  ensure_single_select_field "Provider" "codex-cli,openai-api,deepseek,ollama"
  ensure_single_select_field "Area" "core,tools,providers,transport,security,docs"
}

find_issue_number_by_title() {
  local title="$1"
  local escaped
  escaped="$(jq_escape "$title")"
  gh issue list -R "$OWNER/$REPO" --state all --limit 500 --json number,title --jq ".[] | select(.title == \"$escaped\") | .number" | head -n1 || true
}

ensure_issue() {
  local title="$1"
  local body="$2"
  local labels_csv="$3"
  local milestone_title="${4:-}"
  local issue_number

  issue_number="$(find_issue_number_by_title "$title")"
  if [[ -n "$issue_number" ]]; then
    log "Issue exists: #$issue_number $title"
    printf '%s\n' "$issue_number"
    return
  fi

  local cmd=(gh issue create -R "$OWNER/$REPO" --title "$title" --body "$body")
  local label
  IFS=',' read -r -a labels <<<"$labels_csv"
  for label in "${labels[@]}"; do
    [[ -n "$label" ]] && cmd+=(--label "$label")
  done
  if [[ -n "$milestone_title" ]]; then
    cmd+=(--milestone "$milestone_title")
  fi

  local out url
  out="$("${cmd[@]}")"
  url="$(printf '%s\n' "$out" | tail -n1)"
  issue_number="${url##*/}"
  [[ "$issue_number" =~ ^[0-9]+$ ]] || die "Failed to parse issue number from: $url"
  log "Created issue: #$issue_number $title"
  printf '%s\n' "$issue_number"
}

find_project_item_id_by_issue() {
  local issue_number="$1"

  gh project item-list "$PROJECT_NUMBER" --owner "$PROJECT_OWNER" --limit 500 --format json --jq ".items[]? | select(.content.number == $issue_number) | .id" | head -n1 || true
}

add_issue_to_project() {
  local issue_number="$1"
  local issue_url
  issue_url="$(gh issue view "$issue_number" -R "$OWNER/$REPO" --json url --jq '.url')"

  local item_id
  item_id="$(gh project item-add "$PROJECT_NUMBER" --owner "$PROJECT_OWNER" --url "$issue_url" --format json --jq '.id' 2>/dev/null || true)"

  if [[ -z "$item_id" ]]; then
    item_id="$(find_project_item_id_by_issue "$issue_number")"
  fi

  [[ -n "$item_id" ]] || die "Could not resolve project item ID for issue #$issue_number"
  printf '%s\n' "$item_id"
}

set_single_select_field() {
  local item_id="$1"
  local field_name="$2"
  local option_name="$3"

  local field_id option_id
  field_id="$(get_field_id "$field_name")"
  [[ -n "$field_id" ]] || { warn "Field not found: $field_name"; return; }

  option_id="$(get_option_id "$field_name" "$option_name")"
  [[ -n "$option_id" ]] || { warn "Option '$option_name' not found in field '$field_name'"; return; }

  gh project item-edit --id "$item_id" --project-id "$PROJECT_ID" --field-id "$field_id" --single-select-option-id "$option_id" >/dev/null
}

set_item_fields() {
  local item_id="$1"
  local epic="$2"
  local priority="$3"
  local size="$4"
  local area="$5"
  local provider="$6"
  local status="${7:-Todo}"

  set_single_select_field "$item_id" "Status" "$status"
  set_single_select_field "$item_id" "Epic" "$epic"
  set_single_select_field "$item_id" "Priority" "$priority"
  set_single_select_field "$item_id" "Size" "$size"
  set_single_select_field "$item_id" "Area" "$area"
  set_single_select_field "$item_id" "Provider" "$provider"
}

issue_1_1_body() {
  local parent="$1"
  cat <<EOF
Parent Epic: #$parent

## Summary
Create the initial \`agent_core/\` package and route Telegram messages through a dedicated Agent entrypoint.

## Acceptance Criteria
- Telegram transport calls \`Agent.handle_message()\`.
- No provider logic remains in Telegram transport layer.
- Memory cap default is 20 turns.

## Implementation notes
- Add \`agent_core/agent.py\`, \`agent_core/memory.py\`, \`agent_core/router.py\`.
- Keep transport thin and focused on Telegram I/O only.
- Preserve current behavior while introducing boundaries.
EOF
}

issue_1_2_body() {
  local parent="$1"
  cat <<EOF
Parent Epic: #$parent

## Summary
Add provider abstraction so Agent Core is implementation-agnostic and supports the current codex-cli provider.

## Acceptance Criteria
- \`Provider\` interface exists with \`generate(messages, stream=False)\`.
- \`CodexCLIProvider\` implements the interface and current behavior still works.
- Provider is configurable without transport code changes.

## Implementation notes
- Add provider base interface and codex-cli implementation module.
- Move provider-specific details out of agent orchestration.
- Keep streaming flag in interface for upcoming transport streaming work.
EOF
}

issue_1_3_body() {
  local parent="$1"
  cat <<EOF
Parent Epic: #$parent

## Summary
Replace unbounded session history with bounded rolling memory.

## Acceptance Criteria
- Default cap is 20 turns and configurable.
- \`/reset\` clears memory.
- Session state does not grow without bound.

## Implementation notes
- Implement rolling buffer semantics in memory module.
- Expose cap through config/env without invasive wiring.
- Keep reset behavior explicit and testable.
EOF
}

issue_1_4_body() {
  local parent="$1"
  cat <<EOF
Parent Epic: #$parent

## Summary
Introduce lean markdown capability registry with selective summarized injection only.

## Acceptance Criteria
- \`capabilities/system.md\`, \`capabilities/git.md\`, \`capabilities/files.md\` exist.
- Capabilities are loaded dynamically.
- Injection is minimal, selective, and summary-first.
- Full capability files are never auto-injected.

## Implementation notes
- Keep capability markdown small and focused.
- Build relevance gate to include capability summaries only when useful.
- Keep injection deterministic and auditable.
EOF
}

issue_1_5_body() {
  local parent="$1"
  cat <<EOF
Parent Epic: #$parent

## Summary
Add explicit tool registry with strict validation and no arbitrary shell execution.

## Acceptance Criteria
- \`tools/base.py\`, \`tools/files.py\`, \`tools/git.py\` exist.
- \`ReadFileTool\`, \`WriteFileTool\`, \`GitStatusTool\` are implemented.
- Tools use explicit registration.
- No arbitrary shell execution path exists.
- Inputs are validated before execution.

## Implementation notes
- Define strict tool interface and registration mechanism.
- Validate paths and arguments against workspace constraints.
- Keep initial toolset minimal and deterministic.
EOF
}

main() {
  require_gh
  preflight_auth
  detect_repo

  log "Repository detected: $OWNER/$REPO"

  ensure_label "epic" "5319E7" "Epic tracking item"
  ensure_label "area:core" "0E8A16" "Core architecture"
  ensure_label "area:tools" "1D76DB" "Tool registry and implementations"
  ensure_label "area:providers" "0052CC" "Provider abstraction and adapters"
  ensure_label "area:transport" "FBCA04" "Telegram/Web transport layer"
  ensure_label "area:security" "B60205" "Security and safe execution"
  ensure_label "area:docs" "6F42C1" "Documentation"
  ensure_label "priority:P0" "D93F0B" "Highest priority"
  ensure_label "priority:P1" "FBCA04" "Medium priority"
  ensure_label "priority:P2" "0E8A16" "Lower priority"
  ensure_label "size:S" "C2E0C6" "Small scope"
  ensure_label "size:M" "F9D0C4" "Medium scope"
  ensure_label "size:L" "D4C5F9" "Large scope"
  ensure_label "provider:codex-cli" "0366D6" "Codex CLI provider"
  ensure_label "provider:openai-api" "1B7FBD" "OpenAI API provider"
  ensure_label "provider:deepseek" "0B7285" "DeepSeek provider"
  ensure_label "provider:ollama" "005CC5" "Ollama provider"
  ensure_label "type:epic" "5319E7" "Epic issue"
  ensure_label "type:task" "0E8A16" "Task issue"
  ensure_label "type:bug" "D73A4A" "Bug issue"
  ensure_label "type:doc" "0075CA" "Documentation issue"

  ensure_milestone "v0.2 Agent Core" >/dev/null
  ensure_milestone "v0.3 Secure Computer Interaction" >/dev/null
  ensure_milestone "v0.5 Multi-Provider Beta" >/dev/null
  ensure_milestone "v1.0 Stable Ubuntu-first Release" >/dev/null

  ensure_project
  ensure_project_fields

  local epic1 epic2 epic3 epic4 epic5

  epic1="$(ensure_issue "EPIC 1: Agent Core Foundation" "Goal: Introduce proper separation of concerns and remove direct codex coupling. Definition of Done: Telegram no longer calls provider directly, provider abstraction exists, bounded memory exists, tool registry exists, lean markdown capability system exists, and behavior remains stable." "type:epic,epic,area:core,priority:P0,size:L,provider:codex-cli" "v0.2 Agent Core")"
  epic2="$(ensure_issue "EPIC 2: Secure Computer Interaction Layer" "Goal: Make local computer interaction safe and reliable with safe subprocess controls, workspace isolation, stable git operations, and SSH environment detection summaries." "type:epic,epic,area:security,priority:P0,size:L,provider:codex-cli" "v0.3 Secure Computer Interaction")"
  epic3="$(ensure_issue "EPIC 3: Multi-Provider Architecture" "Goal: Add support beyond codex-cli with provider adapters and runtime provider switching." "type:epic,epic,area:providers,priority:P1,size:L,provider:openai-api" "v0.5 Multi-Provider Beta")"
  epic4="$(ensure_issue "EPIC 4: Streaming and CLI-like Feedback" "Goal: Make Telegram feel responsive like CLI with streaming, edits, and throttled progress updates." "type:epic,epic,area:transport,priority:P1,size:M,provider:codex-cli" "v0.5 Multi-Provider Beta")"
  epic5="$(ensure_issue "EPIC 5: Lightweight Web Control Center" "Goal: Add a minimal web control center for sessions, providers, memory, and tool usage without runtime bloat." "type:epic,epic,area:transport,priority:P2,size:L,provider:codex-cli" "v1.0 Stable Ubuntu-first Release")"

  local item_id
  item_id="$(add_issue_to_project "$epic1")"; set_item_fields "$item_id" "EPIC 1 - Agent Core Foundation" "P0" "L" "core" "codex-cli" "Todo"
  item_id="$(add_issue_to_project "$epic2")"; set_item_fields "$item_id" "EPIC 2 - Secure Computer Interaction Layer" "P0" "L" "security" "codex-cli" "Todo"
  item_id="$(add_issue_to_project "$epic3")"; set_item_fields "$item_id" "EPIC 3 - Multi-Provider Architecture" "P1" "L" "providers" "openai-api" "Todo"
  item_id="$(add_issue_to_project "$epic4")"; set_item_fields "$item_id" "EPIC 4 - Streaming and CLI-like Feedback" "P1" "M" "transport" "codex-cli" "Todo"
  item_id="$(add_issue_to_project "$epic5")"; set_item_fields "$item_id" "EPIC 5 - Lightweight Web Control Center" "P2" "L" "transport" "codex-cli" "Todo"

  local i11 i12 i13 i14 i15
  i11="$(ensure_issue "1.1 Create agent_core module" "$(issue_1_1_body "$epic1")" "type:task,area:core,priority:P0,size:M,provider:codex-cli" "v0.2 Agent Core")"
  i12="$(ensure_issue "1.2 Provider abstraction + codex-cli provider" "$(issue_1_2_body "$epic1")" "type:task,area:providers,priority:P0,size:M,provider:codex-cli" "v0.2 Agent Core")"
  i13="$(ensure_issue "1.3 Bounded memory" "$(issue_1_3_body "$epic1")" "type:task,area:core,priority:P0,size:S,provider:codex-cli" "v0.2 Agent Core")"
  i14="$(ensure_issue "1.4 Markdown capability registry (lean)" "$(issue_1_4_body "$epic1")" "type:task,area:core,priority:P1,size:M,provider:codex-cli" "v0.2 Agent Core")"
  i15="$(ensure_issue "1.5 Explicit tool registry" "$(issue_1_5_body "$epic1")" "type:task,area:tools,priority:P0,size:M,provider:codex-cli" "v0.2 Agent Core")"

  item_id="$(add_issue_to_project "$i11")"; set_item_fields "$item_id" "EPIC 1 - Agent Core Foundation" "P0" "M" "core" "codex-cli" "Todo"
  item_id="$(add_issue_to_project "$i12")"; set_item_fields "$item_id" "EPIC 1 - Agent Core Foundation" "P0" "M" "providers" "codex-cli" "Todo"
  item_id="$(add_issue_to_project "$i13")"; set_item_fields "$item_id" "EPIC 1 - Agent Core Foundation" "P0" "S" "core" "codex-cli" "Todo"
  item_id="$(add_issue_to_project "$i14")"; set_item_fields "$item_id" "EPIC 1 - Agent Core Foundation" "P1" "M" "core" "codex-cli" "Todo"
  item_id="$(add_issue_to_project "$i15")"; set_item_fields "$item_id" "EPIC 1 - Agent Core Foundation" "P0" "M" "tools" "codex-cli" "Todo"

  cat <<EOF

[DONE] Bootstrap complete.
Project: $PROJECT_TITLE
Owner:   $PROJECT_OWNER_LOGIN
Repo:    $OWNER/$REPO
URL:     $PROJECT_URL

Manual project view setup (one-time, in GitHub UI):
1. Create view "Kanban by Status" with layout Board, grouped by Status.
2. Create view "Table grouped by Epic" with layout Table, grouped by Epic.
3. Create view "Backlog" filtered to Status=Todo.

Helpful diagnostics:
- Check auth and scopes: gh auth status
- List project fields: gh project field-list $PROJECT_NUMBER --owner $PROJECT_OWNER
- List project items: gh project item-list $PROJECT_NUMBER --owner $PROJECT_OWNER --limit 200
EOF
}

OWNER=""
REPO=""
PROJECT_OWNER=""
PROJECT_OWNER_LOGIN=""
PROJECT_NUMBER=""
PROJECT_ID=""
PROJECT_URL=""

main "$@"
