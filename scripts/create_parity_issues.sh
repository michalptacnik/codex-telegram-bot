#!/usr/bin/env bash
set -euo pipefail

OWNER="michalptacnik"
REPO="codex-telegram-bot"
SEED_FILE="docs/issue_seeds/telegram_parity_issues.json"

usage() {
  cat <<USAGE
Usage: create_parity_issues.sh [--owner OWNER] [--repo REPO] [--seed-file FILE]

Requires env var GH_TOKEN with repo issue write access.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --owner) OWNER="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --seed-file) SEED_FILE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ -z "${GH_TOKEN:-}" ]]; then
  echo "GH_TOKEN is required" >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "jq is required" >&2
  exit 1
fi

if [[ ! -f "$SEED_FILE" ]]; then
  echo "Seed file not found: $SEED_FILE" >&2
  exit 1
fi

create_issue() {
  local title="$1"
  local body="$2"
  local labels_csv="$3"

  local existing
  existing=$(curl -sS \
    -H "Authorization: Bearer $GH_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$OWNER/$REPO/issues?state=all&per_page=100" \
    | jq -r --arg t "$title" '.[] | select(.title == $t) | .number' | head -n1)

  if [[ -n "$existing" ]]; then
    echo "skip: #$existing $title"
    return 0
  fi

  local payload
  payload=$(jq -n --arg title "$title" --arg body "$body" --arg labels "$labels_csv" '{title:$title, body:$body, labels:($labels|split(",")|map(select(length>0)))}')

  local response
  response=$(curl -sS -X POST \
    -H "Authorization: Bearer $GH_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$OWNER/$REPO/issues" \
    -d "$payload")

  local num url
  num=$(echo "$response" | jq -r '.number // empty')
  url=$(echo "$response" | jq -r '.html_url // empty')
  if [[ -z "$num" || -z "$url" ]]; then
    echo "failed: $title" >&2
    echo "$response" >&2
    exit 1
  fi
  echo "created: #$num $url"
}

jq -c '.[]' "$SEED_FILE" | while IFS= read -r item; do
  title=$(echo "$item" | jq -r '.title')
  body=$(echo "$item" | jq -r '.body')
  labels_csv=$(echo "$item" | jq -r '.labels | join(",")')
  create_issue "$title" "$body" "$labels_csv"
done
