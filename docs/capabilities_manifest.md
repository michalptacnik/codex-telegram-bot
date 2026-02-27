# Capabilities Manifest Spec

## Files
- `CAPABILITIES.md` (human-readable)
- `AGENTS_INDEX.json` (machine-readable)

## JSON Shape
```json
{
  "workspace_root": "string",
  "repo_root": "string",
  "cwd": "string",
  "is_git_repo": true,
  "tools": ["tool_name"],
  "disabled_tools": {"tool_name": "reason"},
  "binaries": {"git": true, "sqlite3": false},
  "permissions": {
    "workspace_write": true,
    "outside_workspace_write": false
  },
  "guidance": "If you need a tool not in the list, ask or degrade gracefully."
}
```

## Runtime Rules
- Manifest is written when session runtime snapshot is refreshed.
- The same content is injected into system prompt context.
- Agents must not invent tools outside `tools`.
- If required capability is missing, agent should ask or degrade gracefully.
