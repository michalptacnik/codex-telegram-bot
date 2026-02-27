import asyncio
import logging
import os
import shlex
import json
import re
from typing import Optional, List
from types import SimpleNamespace

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.constants import ChatAction
    from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters
except Exception:  # pragma: no cover - exercised only in minimal test environments.
    class Update:  # type: ignore[override]
        pass

    class InlineKeyboardButton:  # type: ignore[override]
        def __init__(self, text: str, callback_data: str = ""):
            self.text = text
            self.callback_data = callback_data

    class InlineKeyboardMarkup:  # type: ignore[override]
        def __init__(self, keyboard):
            self.keyboard = keyboard

    class ChatAction:  # type: ignore[override]
        TYPING = "typing"

    class _FilterStub:
        def __and__(self, _other):
            return self

        def __invert__(self):
            return self

    class _BuilderStub:
        def token(self, _token: str):
            return self

        def build(self):
            raise RuntimeError("python-telegram-bot is required to build application runtime.")

    class ApplicationBuilder:  # type: ignore[override]
        def token(self, token: str):
            return _BuilderStub().token(token)

    class CommandHandler:  # type: ignore[override]
        def __init__(self, *_args, **_kwargs):
            pass

    class MessageHandler:  # type: ignore[override]
        def __init__(self, *_args, **_kwargs):
            pass

    class CallbackQueryHandler:  # type: ignore[override]
        def __init__(self, *_args, **_kwargs):
            pass

    class ContextTypes:  # type: ignore[override]
        DEFAULT_TYPE = object

    filters = SimpleNamespace(TEXT=_FilterStub(), COMMAND=_FilterStub())

from codex_telegram_bot.agent_core.agent import Agent
from codex_telegram_bot.app_container import build_agent_service
from codex_telegram_bot.execution.policy import ExecutionPolicyEngine
from codex_telegram_bot.services.agent_service import AgentService
from codex_telegram_bot.services.message_updater import MessageUpdater
from .util import chunk_text

logger = logging.getLogger(__name__)

MAX_INPUT_CHARS = 6000
MAX_OUTPUT_CHARS = 3800
EPHEMERAL_STATUS_TTL_SEC = 12
STATUS_HEARTBEAT_SEC = 15
STATUS_HEARTBEAT_PUSH_SEC = 45
USER_WINDOW_SEC = 60
MAX_USER_COMMANDS_PER_WINDOW = 20
COMMAND_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")
TOOL_LEAK_RE = re.compile(
    r"(?is)(^\s*!(exec|tool|loop)\b|step\s*\d+\s*:.*\{cmd\s*:|\|\s*timeout\s*=|```.*!(exec|tool|loop).*```)"
)
_COMMAND_HANDLERS = [
    ("ping", "handle_ping"),
    ("new", "handle_new"),
    ("resume", "handle_resume"),
    ("branch", "handle_branch"),
    ("pending", "handle_pending"),
    ("approve", "handle_approve"),
    ("deny", "handle_deny"),
    ("reset", "handle_reset"),
    ("status", "handle_status"),
    ("help", "handle_help"),
    ("workspace", "handle_workspace"),
    ("skills", "handle_skills"),
    ("email", "handle_email"),
    ("gh", "handle_gh"),
    ("email_check", "handle_email_check"),
    ("contact", "handle_contact"),
    ("template", "handle_template"),
    ("email_template", "handle_email_template"),
    ("reinstall", "handle_reinstall"),
    ("purge", "handle_purge"),
    ("restart", "handle_restart"),
    ("sessions", "handle_sessions"),
    ("tail", "handle_tail"),
    ("kill", "handle_kill"),
    ("interrupt", "handle_interrupt"),
    ("continue", "handle_continue"),
]


def is_allowed(user_id: int, allowlist: Optional[List[int]]) -> bool:
    if allowlist is None:
        return True
    return user_id in allowlist


def _extract_exec_argv(prompt: str) -> List[List[str]]:
    argv_list: List[List[str]] = []
    for raw_line in (prompt or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("!exec "):
            continue
        cmd = line[len("!exec ") :].strip()
        if not cmd:
            continue
        try:
            argv = shlex.split(cmd)
        except ValueError:
            continue
        if argv:
            argv_list.append(argv)
    return argv_list


def _strip_flag(parts: List[str], flag: str) -> tuple[List[str], bool]:
    out: List[str] = []
    found = False
    for p in parts:
        if p == flag:
            found = True
            continue
        out.append(p)
    return out, found


def _parse_email_command_spec(args: List[str]) -> tuple[Optional[dict], str]:
    cleaned, dry_run = _strip_flag([str(a or "").strip() for a in args], "--dry-run")
    raw = " ".join([x for x in cleaned if x]).strip()
    if not raw:
        return None, "Usage: /email [--dry-run] to@example.com | Subject | Body"
    parts = [p.strip() for p in raw.split("|", 2)]
    if len(parts) != 3:
        return None, "Usage: /email [--dry-run] to@example.com | Subject | Body"
    to_addr, subject, body = parts
    if "@" not in to_addr or "." not in to_addr.split("@")[-1]:
        return None, "Error: invalid recipient email address."
    if not subject or not body:
        return None, "Error: subject and body are required."
    payload = {
        "name": "send_email_smtp",
        "args": {
            "to": to_addr,
            "subject": subject,
            "body": body,
            "dry_run": bool(dry_run),
        },
    }
    return payload, ""


def _parse_gh_command_spec(args: List[str]) -> tuple[Optional[dict], str]:
    cleaned, dry_run = _strip_flag([str(a or "").strip() for a in args], "--dry-run")
    tokens = [t for t in cleaned if t]
    if len(tokens) < 1:
        return None, (
            "Usage: /gh [--dry-run] comment <owner/repo> <issue> <body...>\n"
            "or: /gh [--dry-run] create <owner/repo> <title> | <body>\n"
            "or: /gh [--dry-run] close <owner/repo> <issue> [completed|not_planned]"
        )
    op = tokens[0].lower()
    if op == "comment":
        if len(tokens) < 4:
            return None, "Usage: /gh comment <owner/repo> <issue> <body...>"
        repo = tokens[1]
        try:
            issue = int(tokens[2])
        except ValueError:
            return None, "Error: issue must be an integer."
        body = " ".join(tokens[3:]).strip()
        if not body:
            return None, "Error: comment body is required."
        return {
            "name": "github_comment",
            "args": {"repo": repo, "issue": issue, "body": body, "dry_run": bool(dry_run)},
        }, ""
    if op == "create":
        if len(tokens) < 3:
            return None, "Usage: /gh create <owner/repo> <title> | <body>"
        repo = tokens[1]
        tail = " ".join(tokens[2:]).strip()
        parts = [p.strip() for p in tail.split("|", 1)]
        title = parts[0] if parts else ""
        body = parts[1] if len(parts) > 1 else ""
        if not title:
            return None, "Error: issue title is required."
        return {
            "name": "github_create_issue",
            "args": {"repo": repo, "title": title, "body": body, "dry_run": bool(dry_run)},
        }, ""
    if op == "close":
        if len(tokens) < 3:
            return None, "Usage: /gh close <owner/repo> <issue> [completed|not_planned]"
        repo = tokens[1]
        try:
            issue = int(tokens[2])
        except ValueError:
            return None, "Error: issue must be an integer."
        reason = (tokens[3].strip().lower() if len(tokens) > 3 else "completed")
        if reason not in {"completed", "not_planned"}:
            reason = "completed"
        return {
            "name": "github_close_issue",
            "args": {"repo": repo, "issue": issue, "reason": reason, "dry_run": bool(dry_run)},
        }, ""
    return None, "Error: unknown /gh operation. Use comment, create, or close."


def _parse_email_check_spec(args: List[str]) -> tuple[Optional[dict], str]:
    email = str((args[0] if args else "") or "").strip()
    if not email:
        return None, "Usage: /email_check <email>"
    return {"name": "email_validate", "args": {"email": email}}, ""


def _parse_contact_spec(args: List[str]) -> tuple[Optional[dict], str]:
    tokens = [str(a or "").strip() for a in args if str(a or "").strip()]
    if not tokens:
        return None, "Usage: /contact add <email> [name...] | list | remove <email>"
    op = tokens[0].lower()
    if op == "list":
        return {"name": "contact_list", "args": {}}, ""
    if op == "add":
        if len(tokens) < 2:
            return None, "Usage: /contact add <email> [name...]"
        return {
            "name": "contact_upsert",
            "args": {"email": tokens[1], "name": " ".join(tokens[2:]).strip()},
        }, ""
    if op == "remove":
        if len(tokens) < 2:
            return None, "Usage: /contact remove <email>"
        return {"name": "contact_remove", "args": {"email": tokens[1]}}, ""
    return None, "Error: unknown /contact operation. Use add, list, or remove."


def _parse_template_spec(args: List[str]) -> tuple[Optional[dict], str]:
    raw = " ".join([str(a or "").strip() for a in args if str(a or "").strip()]).strip()
    if not raw:
        return None, "Usage: /template save <id> | <subject> | <body> | list | show <id> | delete <id>"
    if raw.lower() == "list":
        return {"name": "template_list", "args": {}}, ""
    tokens = raw.split()
    op = tokens[0].lower() if tokens else ""
    if op == "show":
        if len(tokens) < 2:
            return None, "Usage: /template show <id>"
        return {"name": "template_get", "args": {"template_id": tokens[1]}}, ""
    if op == "delete":
        if len(tokens) < 2:
            return None, "Usage: /template delete <id>"
        return {"name": "template_delete", "args": {"template_id": tokens[1]}}, ""
    if op == "save":
        body = raw[len("save"):].strip()
        parts = [p.strip() for p in body.split("|", 2)]
        if len(parts) != 3:
            return None, "Usage: /template save <id> | <subject> | <body>"
        return {
            "name": "template_upsert",
            "args": {"template_id": parts[0], "subject": parts[1], "body": parts[2]},
        }, ""
    return None, "Error: unknown /template operation."


def _parse_email_template_spec(args: List[str]) -> tuple[Optional[dict], str]:
    tokens = [str(a or "").strip() for a in args if str(a or "").strip()]
    if len(tokens) < 2:
        return None, "Usage: /email_template [--dry-run] <template_id> <to_email>"
    tokens, dry_run = _strip_flag(tokens, "--dry-run")
    if len(tokens) < 2:
        return None, "Usage: /email_template [--dry-run] <template_id> <to_email>"
    return {
        "name": "send_email_template",
        "args": {"template_id": tokens[0], "to": tokens[1], "dry_run": bool(dry_run)},
    }, ""


def _prompt_has_high_risk_tool_actions(prompt: str) -> bool:
    engine = ExecutionPolicyEngine()
    for argv in _extract_exec_argv(prompt):
        decision = engine.evaluate(argv=argv, policy_profile="trusted")
        if decision.risk_tier == "high":
            return True
    return False


def _resolve_pending_by_prefix(pending: List[dict], prefix: str) -> Optional[dict]:
    value = (prefix or "").strip().lower()
    if not value:
        return None
    return next((p for p in pending if p["approval_id"].startswith(value)), None)


def _humanize_approval_execution_output(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return "(no output)"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for line in lines:
        if line.lower().startswith("email sent to "):
            return f"Done. {line}"
    if lines and lines[0].startswith("[tool:"):
        rc_line = next((ln for ln in lines if ln.startswith("[tool:") and "rc=" in ln), "")
        if rc_line and "rc=0" not in rc_line:
            return "I ran the approved action, but it failed. Check /status for details."
        return "Done. I ran the approved action."
    return text


def _allow_user_command(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    now = asyncio.get_running_loop().time()
    limiter = context.application.bot_data.setdefault("user_command_limiter", {})
    key = int(user_id or 0)
    stamps = [t for t in limiter.get(key, []) if now - float(t) <= USER_WINDOW_SEC]
    if len(stamps) >= MAX_USER_COMMANDS_PER_WINDOW:
        limiter[key] = stamps
        return False
    stamps.append(now)
    limiter[key] = stamps
    return True


def _is_valid_command_name(name: str) -> bool:
    return bool(COMMAND_NAME_RE.match((name or "").strip()))


def _looks_like_tool_leak(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    # Match tool directives anywhere in the outgoing message.
    if re.search(r"(?is)![a-z_][a-z0-9_-]*(?:\s|$)", raw):
        return True
    if re.search(r"(?is)!(exec|tool|loop)\s", raw):
        return True
    if re.search(r"(?is)\b(exec|tool|loop)\s*\{", raw):
        return True
    if TOOL_LEAK_RE.search(raw):
        return True
    if re.search(r"(?is)step\s*\d+\s*:.*\{cmd\s*:", raw) and re.search(r"(?is)\|\s*timeout\s*=", raw):
        return True
    if re.search(r"(?is)```.*?(?:!exec|!tool|!loop|\{cmd\s*:).*?```", raw):
        return True
    return bool(re.match(r"(?is)^\s*\{.*\"(name|tool|args)\"\s*:", raw))


def _sanitize_command_name(name: str) -> str:
    sanitized = re.sub(r"[^a-z0-9_]", "_", (name or "").strip().lower())
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    return sanitized[:32]


def _validate_command_registry(command_specs: List[tuple[str, object]]) -> None:
    for command_name, _ in command_specs:
        if not _is_valid_command_name(command_name):
            raise RuntimeError(
                f"Invalid Telegram command '{command_name}'. Use lowercase letters, digits, and underscore only."
            )


def _build_command_registry() -> List[tuple[str, object]]:
    command_specs: List[tuple[str, object]] = []
    used_names = set()
    for raw_command_name, handler_name in _COMMAND_HANDLERS:
        handler = globals().get(handler_name)
        if not callable(handler):
            logger.error("Skipping Telegram command '%s': handler '%s' is missing.", raw_command_name, handler_name)
            continue
        command_name = _sanitize_command_name(raw_command_name)
        if not _is_valid_command_name(command_name):
            logger.error("Skipping Telegram command '%s': sanitized value '%s' is invalid.", raw_command_name, command_name)
            continue
        if command_name in used_names:
            logger.error("Skipping Telegram command '%s': duplicate command '%s'.", raw_command_name, command_name)
            continue
        if command_name != raw_command_name:
            logger.warning("Normalized Telegram command '%s' to '%s'.", raw_command_name, command_name)
        used_names.add(command_name)
        command_specs.append((command_name, handler))
    if not command_specs:
        raise RuntimeError("No Telegram commands were registered. Check command registry configuration.")
    return command_specs


async def _delete_message_later(bot, chat_id: int, message_id: int, delay_sec: int = EPHEMERAL_STATUS_TTL_SEC) -> None:
    await asyncio.sleep(max(1, int(delay_sec)))
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def _send_approval_options(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    approval_id: str,
    command_preview: str,
) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else 0
    msg = (
        "Approval required: allow this high-risk action?\n"
        f"`{command_preview[:180]}`\n"
        "1) Allow once\n"
        "2) Deny\n"
        "3) Show pending list"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("1) Allow once", callback_data=f"approval:allow:{approval_id}"),
                InlineKeyboardButton("2) Deny", callback_data=f"approval:deny:{approval_id}"),
            ],
            [InlineKeyboardButton("3) Show pending", callback_data="approval:pending")],
        ]
    )
    sent = await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=keyboard)
    asyncio.create_task(
        _delete_message_later(
            bot=context.bot,
            chat_id=chat_id,
            message_id=sent.message_id,
            delay_sec=60,
        )
    )


async def handle_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await update.message.reply_text("✅")
    except Exception as exc:
        logger.exception("Ping handler error: %s", exc)


async def handle_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.effective_chat:
            return
        user_id = update.message.from_user.id if update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        if not _allow_user_command(context, user_id):
            await update.message.reply_text("Rate limit: too many commands. Please wait a minute.")
            return
        agent = context.bot_data.get("agent")
        session = agent.reset_session(chat_id=update.effective_chat.id, user_id=user_id)
        await update.message.reply_text(f"New session started: `{session.session_id[:8]}`", parse_mode="Markdown")
    except Exception as exc:
        logger.exception("Reset handler error: %s", exc)


async def handle_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_reset(update, context)


async def handle_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.effective_chat:
            return
        user_id = update.message.from_user.id if update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        agent_service = context.bot_data.get("agent_service")
        target_prefix = ""
        if context.args:
            target_prefix = (context.args[0] or "").strip().lower()
        session = agent_service.get_or_create_session(chat_id=update.effective_chat.id, user_id=user_id)
        if target_prefix:
            candidates = agent_service.list_sessions_for_chat_user(
                chat_id=update.effective_chat.id,
                user_id=user_id,
                limit=100,
            )
            exact = next((s for s in candidates if s.session_id.startswith(target_prefix)), None)
            if exact:
                activated = agent_service.activate_session(
                    chat_id=update.effective_chat.id,
                    user_id=user_id,
                    session_id=exact.session_id,
                )
                if activated:
                    session = activated
        history = agent_service.list_session_messages(session.session_id, limit=6)
        turns = len([m for m in history if m.role in {"user", "assistant"}])
        await update.message.reply_text(
            f"Active session: `{session.session_id[:8]}`\nRecent turns: {turns}",
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.exception("Resume handler error: %s", exc)


async def handle_branch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.effective_chat:
            return
        user_id = update.message.from_user.id if update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        agent_service = context.bot_data.get("agent_service")
        source = agent_service.get_or_create_session(chat_id=update.effective_chat.id, user_id=user_id)
        branched = agent_service.create_branch_session(
            chat_id=update.effective_chat.id,
            user_id=user_id,
            from_session_id=source.session_id,
            copy_messages=12,
        )
        if not branched:
            await update.message.reply_text("Could not create branch session.")
            return
        await update.message.reply_text(
            f"Branched `{source.session_id[:8]}` -> `{branched.session_id[:8]}`",
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.exception("Branch handler error: %s", exc)


async def handle_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.effective_chat:
            return
        user_id = update.message.from_user.id if update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        agent_service = context.bot_data.get("agent_service")
        pending = agent_service.list_pending_tool_approvals(
            chat_id=update.effective_chat.id,
            user_id=user_id,
            limit=10,
        )
        if not pending:
            await update.message.reply_text("No pending approvals.")
            return
        lines = ["Pending approvals:"]
        for item in pending:
            cmd = " ".join(item.get("argv", []))
            lines.append(f"- `{item['approval_id'][:8]}` risk={item['risk_tier']} cmd={cmd[:80]}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        logger.exception("Pending handler error: %s", exc)


async def handle_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.effective_chat:
            return
        user_id = update.message.from_user.id if update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        if not _allow_user_command(context, user_id):
            await update.message.reply_text("Rate limit: too many commands. Please wait a minute.")
            return
        if not context.args:
            await update.message.reply_text("Usage: /approve <approval_id_prefix>")
            return
        prefix = (context.args[0] or "").strip().lower()
        agent_service = context.bot_data.get("agent_service")
        pending = agent_service.list_pending_tool_approvals(
            chat_id=update.effective_chat.id,
            user_id=user_id,
            limit=50,
        )
        match = _resolve_pending_by_prefix(pending, prefix)
        if not match:
            await update.message.reply_text("Approval not found.")
            return
        out = await agent_service.approve_tool_action(
            approval_id=match["approval_id"],
            chat_id=update.effective_chat.id,
            user_id=user_id,
        )
        for chunk in chunk_text(_humanize_approval_execution_output(out), MAX_OUTPUT_CHARS):
            await update.message.reply_text(chunk)
    except Exception as exc:
        logger.exception("Approve handler error: %s", exc)


async def handle_deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.effective_chat:
            return
        user_id = update.message.from_user.id if update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        if not _allow_user_command(context, user_id):
            await update.message.reply_text("Rate limit: too many commands. Please wait a minute.")
            return
        if not context.args:
            await update.message.reply_text("Usage: /deny <approval_id_prefix>")
            return
        prefix = (context.args[0] or "").strip().lower()
        agent_service = context.bot_data.get("agent_service")
        pending = agent_service.list_pending_tool_approvals(
            chat_id=update.effective_chat.id,
            user_id=user_id,
            limit=50,
        )
        match = _resolve_pending_by_prefix(pending, prefix)
        if not match:
            await update.message.reply_text("Approval not found.")
            return
        out = agent_service.deny_tool_action(
            approval_id=match["approval_id"],
            chat_id=update.effective_chat.id,
            user_id=user_id,
        )
        await update.message.reply_text(out)
    except Exception as exc:
        logger.exception("Deny handler error: %s", exc)


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.effective_chat:
            return
        agent_service = context.bot_data.get("agent_service")
        version = await agent_service.provider_version()
        cwd = os.getcwd()
        allowlist_active = "yes" if context.bot_data.get("allowlist") else "no"
        user_id = update.message.from_user.id if update.message.from_user else 0
        session = agent_service.get_or_create_session(chat_id=update.effective_chat.id, user_id=user_id)
        chat_id = update.effective_chat.id
        active_jobs = context.application.bot_data.setdefault("active_jobs", {})
        run_state = context.application.bot_data.setdefault("run_state", {})
        state = run_state.get(chat_id, {})
        pending = agent_service.list_pending_tool_approvals(chat_id=chat_id, user_id=user_id, limit=200)
        diagnostics = agent_service.session_context_diagnostics(session.session_id)
        active_job = active_jobs.get(chat_id, "")
        active_step = state.get("active_step", "-")
        total_steps = state.get("steps_total", "-")
        active_agent = agent_service.get_agent(session.current_agent_id) if hasattr(session, "current_agent_id") else None
        provider_label = (
            (active_agent.provider if active_agent else "") or agent_service.default_provider_name()
        )
        elapsed = ""
        started_at = state.get("started_at")
        if started_at:
            elapsed_sec = max(0, int(asyncio.get_running_loop().time() - float(started_at)))
            elapsed = f"{elapsed_sec}s"
        else:
            elapsed = "-"
        msg = (
            f"Provider: {provider_label}\n"
            f"Provider version/model: {version}\n"
            f"CWD: {cwd}\n"
            f"Allowlist active: {allowlist_active}\n"
            f"Session: {session.session_id[:8]}\n"
            f"Active job: {(active_job[:8] if active_job else '-')}\n"
            f"Step: {active_step}/{total_steps}\n"
            f"Pending approvals: {len(pending)}\n"
            f"Elapsed: {elapsed}\n"
            f"Context: prompt={diagnostics.get('prompt_chars', 0)} chars, "
            f"retrieval={diagnostics.get('retrieval_confidence', 'n/a')}"
        )
        await update.message.reply_text(msg)
    except Exception as exc:
        logger.exception("Status handler error: %s", exc)


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.effective_chat:
            return
        user_id = update.message.from_user.id if update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        agent_service = context.bot_data.get("agent_service")
        session = agent_service.get_or_create_session(chat_id=update.effective_chat.id, user_id=user_id)
        profile = "trusted"
        session_obj = agent_service.get_session(session.session_id)
        if session_obj:
            agent = agent_service.get_agent(session_obj.current_agent_id)
            if agent:
                profile = agent.policy_profile
        text = (
            "Commands:\n"
            "/new, /resume [id], /branch, /status, /workspace, /skills, /pending, /approve <id>, /deny <id>, /interrupt, /continue, /sessions, /tail [id], /kill [id], /email, /gh, /email_check, /contact, /template, /email_template\n"
            "\n"
            "Examples:\n"
            "- `!exec /bin/ls -la`\n"
            "- `!loop {\"steps\":[{\"kind\":\"exec\",\"command\":\"/bin/echo hi\"}],\"final_prompt\":\"summarize\"}`\n"
            "- `/email me@example.com | Subject | Body`\n"
            "- `/gh comment owner/repo 123 looks good`\n"
            "- `/contact add me@example.com Michal`\n"
            "- `/template save welcome | Welcome | Hello from template.`\n"
            "- `/email_template welcome me@example.com`\n"
            "\n"
            f"Active policy profile: `{profile}`\n"
            "High-risk actions require approval and are auditable."
        )
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as exc:
        logger.exception("Help handler error: %s", exc)


async def handle_workspace(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.effective_chat:
            return
        user_id = update.message.from_user.id if update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        agent_service = context.bot_data.get("agent_service")
        session = agent_service.get_or_create_session(chat_id=update.effective_chat.id, user_id=user_id)
        ws = agent_service.session_workspace(session.session_id)
        await update.message.reply_text(
            f"Session: `{session.session_id[:8]}`\nWorkspace: `{ws}`",
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.exception("Workspace handler error: %s", exc)


async def handle_skills(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.effective_chat:
            return
        user_id = update.message.from_user.id if update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        agent_service = context.bot_data.get("agent_service")
        items = agent_service.list_skills()
        if not items:
            await update.message.reply_text("No skills available.")
            return
        lines = ["Skills:"]
        for s in items:
            status = "enabled" if s.get("enabled") else "disabled"
            lines.append(f"- `{s.get('skill_id')}` {status} tools={','.join(s.get('tools') or [])}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        logger.exception("Skills handler error: %s", exc)


async def handle_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.effective_chat:
            return
        user_id = update.message.from_user.id if update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        if not _allow_user_command(context, user_id):
            await update.message.reply_text("Rate limit: too many commands. Please wait a minute.")
            return
        spec, err = _parse_email_command_spec(context.args or [])
        if not spec:
            await update.message.reply_text(err or "Invalid /email command.")
            return
        text = "!tool " + json.dumps(spec, ensure_ascii=True) + "\n\nConfirm email send result briefly."
        await _process_prompt(update=update, context=context, text=text, user_id=user_id)
    except Exception as exc:
        logger.exception("Email handler error: %s", exc)


async def handle_gh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.effective_chat:
            return
        user_id = update.message.from_user.id if update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        if not _allow_user_command(context, user_id):
            await update.message.reply_text("Rate limit: too many commands. Please wait a minute.")
            return
        spec, err = _parse_gh_command_spec(context.args or [])
        if not spec:
            await update.message.reply_text(err or "Invalid /gh command.")
            return
        text = "!tool " + json.dumps(spec, ensure_ascii=True) + "\n\nSummarize the GitHub action result briefly."
        await _process_prompt(update=update, context=context, text=text, user_id=user_id)
    except Exception as exc:
        logger.exception("GH handler error: %s", exc)


async def handle_email_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.effective_chat:
            return
        user_id = update.message.from_user.id if update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        spec, err = _parse_email_check_spec(context.args or [])
        if not spec:
            await update.message.reply_text(err or "Invalid /email_check command.")
            return
        text = "!tool " + json.dumps(spec, ensure_ascii=True)
        await _process_prompt(update=update, context=context, text=text, user_id=user_id)
    except Exception as exc:
        logger.exception("Email-check handler error: %s", exc)


async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.effective_chat:
            return
        user_id = update.message.from_user.id if update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        spec, err = _parse_contact_spec(context.args or [])
        if not spec:
            await update.message.reply_text(err or "Invalid /contact command.")
            return
        text = "!tool " + json.dumps(spec, ensure_ascii=True)
        await _process_prompt(update=update, context=context, text=text, user_id=user_id)
    except Exception as exc:
        logger.exception("Contact handler error: %s", exc)


async def handle_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.effective_chat:
            return
        user_id = update.message.from_user.id if update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        spec, err = _parse_template_spec(context.args or [])
        if not spec:
            await update.message.reply_text(err or "Invalid /template command.")
            return
        text = "!tool " + json.dumps(spec, ensure_ascii=True)
        await _process_prompt(update=update, context=context, text=text, user_id=user_id)
    except Exception as exc:
        logger.exception("Template handler error: %s", exc)


async def handle_email_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.effective_chat:
            return
        user_id = update.message.from_user.id if update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        spec, err = _parse_email_template_spec(context.args or [])
        if not spec:
            await update.message.reply_text(err or "Invalid /email_template command.")
            return
        text = "!tool " + json.dumps(spec, ensure_ascii=True)
        await _process_prompt(update=update, context=context, text=text, user_id=user_id)
    except Exception as exc:
        logger.exception("Email-template handler error: %s", exc)


async def handle_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.effective_chat:
            return
        user_id = update.message.from_user.id if update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        agent_service = context.bot_data.get("agent_service")
        manager = getattr(agent_service, "process_manager", None)
        if manager is None:
            await update.message.reply_text("Process sessions are unavailable in this runtime.")
            return
        rows = manager.list_sessions(chat_id=update.effective_chat.id, user_id=user_id, limit=20)
        if not rows:
            await update.message.reply_text("No process sessions.")
            return
        lines = ["Process sessions:"]
        for row in rows[:20]:
            sid = str(row.get("process_session_id") or "")[:16]
            cmd = " ".join(list(row.get("argv") or []))[:80]
            lines.append(f"- `{sid}` {row.get('status')} pty={bool(row.get('pty_enabled'))} {cmd}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        logger.exception("Sessions handler error: %s", exc)


async def handle_tail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.effective_chat:
            return
        user_id = update.message.from_user.id if update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        agent_service = context.bot_data.get("agent_service")
        manager = getattr(agent_service, "process_manager", None)
        if manager is None:
            await update.message.reply_text("Process sessions are unavailable in this runtime.")
            return
        session_id = str((context.args[0] if context.args else "") or "").strip()
        if not session_id:
            active = manager.get_active_session(chat_id=update.effective_chat.id, user_id=user_id)
            session_id = str((active or {}).get("process_session_id") or "")
        if not session_id:
            await update.message.reply_text("No active process session.")
            return
        polled = manager.poll_session(process_session_id=session_id, cursor=None)
        if not polled.get("ok"):
            await update.message.reply_text(str(polled.get("error") or "Failed to poll session."))
            return
        output = str(polled.get("output") or "(no new output)")
        msg = f"session={session_id} status={polled.get('status')} cursor_next={polled.get('cursor_next')}\n{output}"
        for chunk in chunk_text(msg, MAX_OUTPUT_CHARS):
            await update.message.reply_text(chunk)
    except Exception as exc:
        logger.exception("Tail handler error: %s", exc)


async def handle_kill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.effective_chat:
            return
        user_id = update.message.from_user.id if update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        agent_service = context.bot_data.get("agent_service")
        manager = getattr(agent_service, "process_manager", None)
        if manager is None:
            await update.message.reply_text("Process sessions are unavailable in this runtime.")
            return
        session_id = str((context.args[0] if context.args else "") or "").strip()
        if not session_id:
            active = manager.get_active_session(chat_id=update.effective_chat.id, user_id=user_id)
            session_id = str((active or {}).get("process_session_id") or "")
        if not session_id:
            await update.message.reply_text("No active process session to kill.")
            return
        result = manager.terminate_session(process_session_id=session_id, mode="kill")
        if result.get("ok"):
            await update.message.reply_text(
                f"Killed session `{session_id[:16]}` status={result.get('status')} exit_code={result.get('exit_code')}",
                parse_mode="Markdown",
            )
            return
        await update.message.reply_text(str(result.get("error") or "Failed to terminate session."))
    except Exception as exc:
        logger.exception("Kill handler error: %s", exc)


async def handle_interrupt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.effective_chat:
            return
        user_id = update.message.from_user.id if update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        chat_id = update.effective_chat.id
        manager = getattr(agent_service, "process_manager", None)
        if manager is not None:
            active = manager.get_active_session(chat_id=chat_id, user_id=user_id)
            if active:
                sid = str(active.get("process_session_id") or "")
                outcome = manager.terminate_session(process_session_id=sid, mode="interrupt")
                if outcome.get("ok"):
                    await update.message.reply_text(
                        f"Interrupted session `{sid[:16]}` status={outcome.get('status')}",
                        parse_mode="Markdown",
                    )
                    return
        active_jobs = context.application.bot_data.setdefault("active_jobs", {})
        active_tasks = context.application.bot_data.setdefault("active_tasks", {})
        cancelled_job = False
        cancelled_task = False
        job_id = active_jobs.get(chat_id)
        if job_id:
            cancelled_job = agent_service.cancel_job(job_id)
        task = active_tasks.get(chat_id)
        if task and not task.done():
            task.cancel()
            cancelled_task = True
        if cancelled_job or cancelled_task:
            await update.message.reply_text("Interrupted active run.")
        else:
            await update.message.reply_text("No active run to interrupt.")
    except Exception as exc:
        logger.exception("Interrupt handler error: %s", exc)


async def handle_continue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.effective_chat:
            return
        user_id = update.message.from_user.id if update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        agent_service = context.bot_data.get("agent_service")
        chat_id = update.effective_chat.id
        manager = getattr(agent_service, "process_manager", None)
        if manager is not None:
            active = manager.get_active_session(chat_id=chat_id, user_id=user_id)
            if active:
                sid = str(active.get("process_session_id") or "")
                polled = manager.poll_session(process_session_id=sid, cursor=None)
                if polled.get("ok"):
                    out = str(polled.get("output") or "(no new output)")
                    msg = f"session={sid} status={polled.get('status')} cursor_next={polled.get('cursor_next')}\n{out}"
                    for chunk in chunk_text(msg, MAX_OUTPUT_CHARS):
                        await update.message.reply_text(chunk)
                    return
        pending_continue = context.application.bot_data.setdefault("pending_continue_prompts", {})
        force_continue = bool(
            context.args and (context.args[0] or "").strip().lower() in {"yes", "force", "confirm"}
        )
        session = agent_service.get_or_create_session(chat_id=chat_id, user_id=user_id)
        last = agent_service.get_last_user_prompt(session.session_id)
        if not last:
            await update.message.reply_text("No previous prompt to continue from.")
            return
        followup = pending_continue.get(chat_id) or (
            f"{last}\n\nContinue from the last response and proceed with the next best step."
        )
        if not force_continue and _prompt_has_high_risk_tool_actions(last):
            pending_continue[chat_id] = followup
            await update.message.reply_text(
                "Continue blocked: last prompt includes high-risk tool actions.\n"
                "Run /continue yes to confirm replay."
            )
            return
        pending_continue.pop(chat_id, None)
        await _process_prompt(update=update, context=context, text=followup, user_id=user_id)
    except Exception as exc:
        logger.exception("Continue handler error: %s", exc)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.message.text:
            return
        user_id = update.message.from_user.id if update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        if not _allow_user_command(context, user_id):
            await update.message.reply_text("Rate limit: too many commands. Please wait a minute.")
            return

        text = update.message.text
        if len(text) > MAX_INPUT_CHARS:
            await update.message.reply_text("Input too long.")
            return

        await _process_prompt(update=update, context=context, text=text, user_id=user_id)
    except asyncio.CancelledError:
        try:
            if update and update.message:
                await update.message.reply_text("Run cancelled.")
        except Exception:
            pass
    except Exception as exc:
        logger.exception("Message handler error: %s", exc)


async def handle_reinstall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.message.from_user.id if update.message and update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        context.application.bot_data.get("reinstall_callback")()
        await update.message.reply_text("Reinstall scheduled. Restarting now.")
        await asyncio.sleep(0.5)
        context.application.bot_data.get("restart_callback")()
    except Exception as exc:
        logger.exception("Reinstall handler error: %s", exc)


async def handle_purge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.message.from_user.id if update.message and update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        context.application.bot_data.get("purge_callback")()
        await update.message.reply_text("Purged .env. Restarting now.")
        await asyncio.sleep(0.5)
        context.application.bot_data.get("restart_callback")()
    except Exception as exc:
        logger.exception("Purge handler error: %s", exc)


async def handle_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_id = update.message.from_user.id if update.message and update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        await update.message.reply_text("Restarting now.")
        await asyncio.sleep(0.5)
        context.application.bot_data.get("restart_callback")()
    except Exception as exc:
        logger.exception("Restart handler error: %s", exc)


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        logger.error("Telegram error: %s", context.error)
    except Exception:
        pass


async def handle_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        query = update.callback_query
        if not query or not update.effective_chat:
            return
        await query.answer()
        user_id = query.from_user.id if query.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        if not _allow_user_command(context, user_id):
            await query.edit_message_text("Rate limit: too many actions. Please wait a minute.")
            return
        data = (query.data or "").strip()
        if not data.startswith("approval:"):
            return
        agent_service = context.bot_data.get("agent_service")
        chat_id = update.effective_chat.id
        pending = agent_service.list_pending_tool_approvals(chat_id=chat_id, user_id=user_id, limit=50)

        if data == "approval:pending":
            if not pending:
                await query.edit_message_text("No pending approvals.")
                return
            lines = ["Pending approvals:"]
            for item in pending[:10]:
                cmd = " ".join(item.get("argv", []))
                lines.append(f"- `{item['approval_id'][:8]}` risk={item['risk_tier']} cmd={cmd[:80]}")
            await query.edit_message_text("\n".join(lines), parse_mode="Markdown")
            return

        parts = data.split(":", 2)
        if len(parts) != 3:
            return
        op, approval_id = parts[1], parts[2]
        match = _resolve_pending_by_prefix(pending, approval_id)
        if not match:
            await query.edit_message_text("Approval not found (already handled or expired).")
            return
        if op == "allow":
            out = await agent_service.approve_tool_action(
                approval_id=match["approval_id"],
                chat_id=chat_id,
                user_id=user_id,
            )
            await query.edit_message_text(f"Approved `{match['approval_id'][:8]}`", parse_mode="Markdown")
            for chunk in chunk_text(_humanize_approval_execution_output(out), MAX_OUTPUT_CHARS):
                await context.bot.send_message(chat_id=chat_id, text=chunk)
            return
        if op == "deny":
            out = agent_service.deny_tool_action(
                approval_id=match["approval_id"],
                chat_id=chat_id,
                user_id=user_id,
            )
            await query.edit_message_text(f"Denied `{match['approval_id'][:8]}`: {out}", parse_mode="Markdown")
            return
    except Exception as exc:
        logger.exception("Approval callback handler error: %s", exc)


async def _process_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, user_id: int) -> None:
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    agent = context.bot_data.get("agent")
    agent_service = context.bot_data.get("agent_service")
    active_jobs = context.application.bot_data.setdefault("active_jobs", {})
    active_tasks = context.application.bot_data.setdefault("active_tasks", {})
    run_state = context.application.bot_data.setdefault("run_state", {})
    active_tasks[update.effective_chat.id] = asyncio.current_task()
    run_state[update.effective_chat.id] = {
        "active_step": 0,
        "steps_total": 0,
        "started_at": asyncio.get_running_loop().time(),
    }
    status_msg = await update.message.reply_text("On it — preparing execution...")
    message_updater = context.application.bot_data.setdefault("message_updater", MessageUpdater())

    async def set_status(text_value: str) -> None:
        await message_updater.update(
            bot=context.bot,
            chat_id=update.effective_chat.id,
            message_id=status_msg.message_id,
            text=text_value[:3900],
            fallback_send=False,
        )

    async def progress(update_payload: dict) -> None:
        event = update_payload.get("event", "")
        if event == "loop.started":
            run_state[update.effective_chat.id]["steps_total"] = int(update_payload.get("steps_total", 0) or 0)
            await set_status(f"Working on it: started {update_payload.get('steps_total', 0)} step(s).")
        elif event == "loop.autoplan.started":
            await set_status("Planning the next concrete actions...")
        elif event == "loop.autoplan.ready":
            run_state[update.effective_chat.id]["steps_total"] = int(update_payload.get("steps_total", 0) or 0)
            await set_status(f"Plan ready: {update_payload.get('steps_total', 0)} executable step(s).")
        elif event == "loop.autoplan.none":
            await set_status("No tool steps needed; drafting the answer.")
        elif event == "skills.activated":
            skills = ", ".join([str(x) for x in list(update_payload.get("skills") or [])][:4])
            await set_status(f"Using skill(s): {skills or 'n/a'}")
        elif event == "skills.deactivated":
            skills = ", ".join([str(x) for x in list(update_payload.get("skills") or [])][:4])
            await set_status(f"Finished with skill(s): {skills or 'n/a'}")
        elif event == "loop.step.started":
            run_state[update.effective_chat.id]["active_step"] = int(update_payload.get("step", 0) or 0)
            await set_status(f"Step {update_payload.get('step')}: {update_payload.get('command', '')[:120]}")
        elif event == "loop.step.awaiting_approval":
            approval_id = str(update_payload.get("approval_id", ""))
            await set_status(f"Paused: waiting for approval `{approval_id[:8]}`.")
            agent_service_local = context.bot_data.get("agent_service")
            pending = agent_service_local.list_pending_tool_approvals(
                chat_id=update.effective_chat.id,
                user_id=user_id,
                limit=20,
            )
            match = _resolve_pending_by_prefix(pending, approval_id)
            if match:
                cmd = " ".join(match.get("argv", []))
                await _send_approval_options(
                    update=update,
                    context=context,
                    approval_id=match["approval_id"],
                    command_preview=cmd,
                )
        elif event == "model.job.queued":
            active_jobs[update.effective_chat.id] = update_payload.get("job_id", "")
            await set_status(f"Model job queued `{str(update_payload.get('job_id', ''))[:8]}`.")
        elif event == "model.job.heartbeat":
            phase = str(update_payload.get("phase", "") or "processing")
            elapsed = int(update_payload.get("elapsed_sec", 0) or 0)
            jid = str(update_payload.get("job_id", "") or "")
            await set_status(
                f"Still working: {phase} ({elapsed}s), job `{jid[:8]}`."
            )
        elif event == "loop.finished":
            await set_status("Finishing up and preparing the response.")

    async def heartbeat() -> None:
        chat_id = update.effective_chat.id
        loop = asyncio.get_running_loop()
        last_push = 0.0
        while True:
            await asyncio.sleep(STATUS_HEARTBEAT_SEC)
            state = run_state.get(chat_id, {})
            if not state:
                return
            now = loop.time()
            elapsed = int(max(0, now - float(state.get("started_at", 0.0))))
            active_step = int(state.get("active_step", 0) or 0)
            total_steps = int(state.get("steps_total", 0) or 0)
            job_id = str(active_jobs.get(chat_id, "") or "")
            if total_steps > 0:
                msg = f"Still working: step {active_step}/{total_steps} ({elapsed}s)."
            elif job_id:
                msg = f"Still working: model job `{job_id[:8]}` ({elapsed}s)."
            else:
                msg = f"Still processing ({elapsed}s)."
            await set_status(msg)
            if now - last_push >= STATUS_HEARTBEAT_PUSH_SEC:
                try:
                    sent = await context.bot.send_message(chat_id=chat_id, text=msg)
                    asyncio.create_task(
                        _delete_message_later(
                            bot=context.bot,
                            chat_id=chat_id,
                            message_id=sent.message_id,
                            delay_sec=EPHEMERAL_STATUS_TTL_SEC,
                        )
                    )
                except Exception:
                    pass
                last_push = now

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        response = await agent.handle_message(
            chat_id=update.effective_chat.id,
            user_id=user_id,
            text=text,
            agent_id="default",
            progress_callback=progress,
        )
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        active_jobs.pop(update.effective_chat.id, None)
        active_tasks.pop(update.effective_chat.id, None)
        run_state.pop(update.effective_chat.id, None)
    output = response.output
    output = output.strip() if output else ""
    if not output:
        output = "(no output)"
    if _looks_like_tool_leak(output):
        try:
            turn = await agent_service.run_turn(
                prompt=output,
                chat_id=update.effective_chat.id,
                user_id=user_id,
                session_id=response.session_id,
                agent_id="default",
                progress_callback=progress,
            )
            output = (turn.text or "").strip() or "(no output)"
        except Exception:
            logger.exception("Output firewall reroute failed")
            output = "I need one clarification to continue: should I run the next tool step now?"

    await set_status("Done — sending response.")
    await message_updater.flush(chat_id=update.effective_chat.id, message_id=status_msg.message_id)
    asyncio.create_task(
        _delete_message_later(
            bot=context.bot,
            chat_id=update.effective_chat.id,
            message_id=status_msg.message_id,
            delay_sec=EPHEMERAL_STATUS_TTL_SEC,
        )
    )
    for chunk in chunk_text(output, MAX_OUTPUT_CHARS):
        await update.message.reply_text(chunk)


def build_application(
    token: str,
    allowlist: Optional[List[int]],
    callbacks: dict,
    agent_service: Optional[AgentService] = None,
    agent: Optional[Agent] = None,
):
    if agent_service is None:
        agent_service = build_agent_service()
    if agent is None:
        agent = Agent(agent_service=agent_service)
    app = ApplicationBuilder().token(token).build()
    app.bot_data["allowlist"] = allowlist
    app.bot_data["agent_service"] = agent_service
    app.bot_data["agent"] = agent
    app.bot_data["message_updater"] = MessageUpdater()
    app.bot_data.update(callbacks)
    command_specs = _build_command_registry()
    for command_name, handler in command_specs:
        app.add_handler(CommandHandler(command_name, handler))
    app.add_handler(CallbackQueryHandler(handle_approval_callback, pattern=r"^approval:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(handle_error)
    return app
