import asyncio
import logging
import os
import shlex
from typing import Optional, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters

from codex_telegram_bot.agent_core.agent import Agent
from codex_telegram_bot.app_container import build_agent_service
from codex_telegram_bot.execution.policy import ExecutionPolicyEngine
from codex_telegram_bot.services.agent_service import AgentService
from .util import chunk_text

logger = logging.getLogger(__name__)

MAX_INPUT_CHARS = 6000
MAX_OUTPUT_CHARS = 3800
EPHEMERAL_STATUS_TTL_SEC = 12
USER_WINDOW_SEC = 60
MAX_USER_COMMANDS_PER_WINDOW = 20


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
        "Approval required: allow Codex to run this command?\n"
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
        for chunk in chunk_text(out or "(no output)", MAX_OUTPUT_CHARS):
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
        elapsed = ""
        started_at = state.get("started_at")
        if started_at:
            elapsed_sec = max(0, int(asyncio.get_running_loop().time() - float(started_at)))
            elapsed = f"{elapsed_sec}s"
        else:
            elapsed = "-"
        msg = (
            f"Codex version: {version}\n"
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
            "/new, /resume [id], /branch, /status, /workspace, /pending, /approve <id>, /deny <id>, /interrupt, /continue\n"
            "\n"
            "Examples:\n"
            "- `!exec /bin/ls -la`\n"
            "- `!loop {\"steps\":[{\"kind\":\"exec\",\"command\":\"/bin/echo hi\"}],\"final_prompt\":\"summarize\"}`\n"
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


async def handle_interrupt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.message or not update.effective_chat:
            return
        user_id = update.message.from_user.id if update.message.from_user else 0
        allowlist = context.bot_data.get("allowlist")
        if not is_allowed(user_id, allowlist):
            return
        chat_id = update.effective_chat.id
        active_jobs = context.application.bot_data.setdefault("active_jobs", {})
        active_tasks = context.application.bot_data.setdefault("active_tasks", {})
        agent_service = context.bot_data.get("agent_service")
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
            for chunk in chunk_text(out or "(no output)", MAX_OUTPUT_CHARS):
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
    status_msg = await update.message.reply_text("Running: received prompt, preparing execution...")

    async def set_status(text_value: str) -> None:
        try:
            await status_msg.edit_text(text_value[:3900])
        except Exception:
            pass

    async def progress(update_payload: dict) -> None:
        event = update_payload.get("event", "")
        if event == "loop.started":
            run_state[update.effective_chat.id]["steps_total"] = int(update_payload.get("steps_total", 0) or 0)
            await set_status(f"Running: tool loop started ({update_payload.get('steps_total', 0)} step(s))...")
        elif event == "loop.step.started":
            run_state[update.effective_chat.id]["active_step"] = int(update_payload.get("step", 0) or 0)
            await set_status(f"Running: step {update_payload.get('step')} -> {update_payload.get('command', '')[:120]}")
        elif event == "loop.step.awaiting_approval":
            approval_id = str(update_payload.get("approval_id", ""))
            await set_status(f"Running paused: awaiting approval `{approval_id[:8]}`")
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
            await set_status(f"Running: model job queued `{str(update_payload.get('job_id', ''))[:8]}`...")
        elif event == "loop.finished":
            await set_status("Running: tool loop finished, finalizing response...")

    try:
        response = await agent.handle_message(
            chat_id=update.effective_chat.id,
            user_id=user_id,
            text=text,
            agent_id="default",
            progress_callback=progress,
        )
    finally:
        active_jobs.pop(update.effective_chat.id, None)
        active_tasks.pop(update.effective_chat.id, None)
        run_state.pop(update.effective_chat.id, None)
    output = response.output
    output = output.strip() if output else ""
    if not output:
        output = "(no output)"

    await set_status("Completed. Cleaning up status message...")
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
    app.bot_data.update(callbacks)

    app.add_handler(CommandHandler("ping", handle_ping))
    app.add_handler(CommandHandler("new", handle_new))
    app.add_handler(CommandHandler("resume", handle_resume))
    app.add_handler(CommandHandler("branch", handle_branch))
    app.add_handler(CommandHandler("pending", handle_pending))
    app.add_handler(CommandHandler("approve", handle_approve))
    app.add_handler(CommandHandler("deny", handle_deny))
    app.add_handler(CommandHandler("reset", handle_reset))
    app.add_handler(CommandHandler("status", handle_status))
    app.add_handler(CommandHandler("help", handle_help))
    app.add_handler(CommandHandler("workspace", handle_workspace))
    app.add_handler(CommandHandler("reinstall", handle_reinstall))
    app.add_handler(CommandHandler("purge", handle_purge))
    app.add_handler(CommandHandler("restart", handle_restart))
    app.add_handler(CommandHandler("interrupt", handle_interrupt))
    app.add_handler(CommandHandler("continue", handle_continue))
    app.add_handler(CallbackQueryHandler(handle_approval_callback, pattern=r"^approval:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(handle_error)
    return app
