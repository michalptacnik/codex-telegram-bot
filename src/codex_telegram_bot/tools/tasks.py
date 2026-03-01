from __future__ import annotations

from codex_telegram_bot.services.thin_memory import ThinMemoryStore
from codex_telegram_bot.tools.base import ToolContext, ToolRequest, ToolResult


class TaskCreateTool:
    name = "task_create"
    description = "Create a task in memory/pages/tasks.md and reflect it in MEMORY_INDEX obligations."

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        title = str(request.args.get("title") or "").strip()
        due = str(request.args.get("due") or "").strip()
        details = str(request.args.get("details") or "").strip()
        tags_raw = request.args.get("tags")
        tags = []
        if isinstance(tags_raw, list):
            tags = [str(x).strip() for x in tags_raw if str(x).strip()]
        elif isinstance(tags_raw, str) and tags_raw.strip():
            tags = [x.strip() for x in tags_raw.split(",") if x.strip()]
        if not title:
            return ToolResult(ok=False, output="title is required.")
        store = ThinMemoryStore(context.workspace_root)
        try:
            task = store.create_task(title=title, due=due, details=details, tags=tags)
        except Exception as exc:
            return ToolResult(ok=False, output=f"Failed to create task: {exc}")
        return ToolResult(
            ok=True,
            output=(
                f"Created task {task.task_id}: {task.title}"
                + (f" due {task.due}" if task.due else "")
            ),
        )


class TaskListTool:
    name = "task_list"
    description = "List tasks from memory/pages/tasks.md."

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        query = str(request.args.get("filter") or "").strip()
        store = ThinMemoryStore(context.workspace_root)
        tasks = store.list_tasks(filter_text=query)
        if not tasks:
            return ToolResult(ok=True, output="No tasks found.")
        lines = []
        for item in tasks:
            status = "done" if item.done else "open"
            lines.append(
                f"- {item.task_id} [{status}] {item.title}"
                + (f" due={item.due}" if item.due else "")
                + (f" tags={','.join(item.tags)}" if item.tags else "")
            )
        return ToolResult(ok=True, output="\n".join(lines))


class TaskDoneTool:
    name = "task_done"
    description = "Mark a task as completed and remove it from MEMORY_INDEX obligations."

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        task_id = str(request.args.get("task_id") or request.args.get("id") or "").strip()
        if not task_id:
            return ToolResult(ok=False, output="task_id is required.")
        store = ThinMemoryStore(context.workspace_root)
        try:
            task = store.mark_task_done(task_id=task_id)
        except Exception as exc:
            return ToolResult(ok=False, output=f"Failed to mark task done: {exc}")
        return ToolResult(ok=True, output=f"Task completed: {task.task_id} {task.title}")
