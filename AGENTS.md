# Agents

## Tanith (Primary)

The default agent. Handles all user requests across all channels.

- **Provider**: Configured via `default_provider` / `default_model`
- **Persona**: See SOUL.md and IDENTITY.md
- **Tools**: All registered tools unless explicitly excluded
- **Skills**: All installed skills in workspace `skills/` directory
- **Memory**: Persistent — learns preferences and context over time
