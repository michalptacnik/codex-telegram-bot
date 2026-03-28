# Agents

## Tanith (Primary)

The default agent. Handles all user requests across all channels.

- **Provider**: Configured via `default_provider` / `default_model`
- **Persona**: See SOUL.md and IDENTITY.md
- **Tools**: All registered tools unless explicitly excluded
- **Skills**: Repo-packaged skills in `skills/` plus installed workspace skills in `~/.zeroclaw/workspace/skills/`
- **Memory**: Persistent — learns preferences and context over time
