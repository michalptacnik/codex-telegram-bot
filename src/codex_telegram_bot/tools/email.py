"""Email tool with safety gating (PRODUCT BAR).

Sends email via SMTP.  The tool is deliberately absent from the default tool
registry unless ``ENABLE_EMAIL_TOOL=true`` is set, so it never appears in the
PROBE catalog for deployments that do not opt in.

Even when enabled, every invocation must be authorised through the approval
queue (``requires_approval = True``).  The tool itself does not block on
approval — that gate lives in AgentService.run_prompt_with_tool_loop().

Configuration:
  ENABLE_EMAIL_TOOL  – must be exactly ``true`` to enable this tool
  SMTP_HOST          – SMTP server hostname (default: localhost)
  SMTP_PORT          – SMTP port (default: 587)
  SMTP_USER          – SMTP username (optional, for STARTTLS auth)
  SMTP_PASSWORD      – SMTP password (optional, for STARTTLS auth)
  EMAIL_FROM         – sender address (required when tool is enabled)
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from codex_telegram_bot.tools.base import ToolContext, ToolRequest, ToolResult

logger = logging.getLogger(__name__)

_ENV_ENABLE = "ENABLE_EMAIL_TOOL"
_ENV_SMTP_HOST = "SMTP_HOST"
_ENV_SMTP_PORT = "SMTP_PORT"
_ENV_SMTP_USER = "SMTP_USER"
_ENV_SMTP_PASSWORD = "SMTP_PASSWORD"
_ENV_EMAIL_FROM = "EMAIL_FROM"


def is_email_tool_enabled() -> bool:
    """Return True only when ENABLE_EMAIL_TOOL=true (case-insensitive)."""
    return os.environ.get(_ENV_ENABLE, "").strip().lower() == "true"


class SendEmailTool:
    """Send an email via SMTP.

    Required args:
      to (str)      – recipient email address
      subject (str) – email subject line
      body (str)    – plain-text email body

    Optional args:
      dry_run (bool) – preview the email without actually sending

    Security notes:
      - Only active when ENABLE_EMAIL_TOOL=true.
      - ``requires_approval = True`` signals the agent loop that human
        approval must be obtained before execution.
      - Use ``dry_run=True`` to inspect what would be sent.
    """

    name = "send_email"

    # Signals to ProbeLoop / AgentService that this tool always needs
    # human approval before it executes.
    requires_approval: bool = True

    def __init__(
        self,
        smtp_host: Optional[str] = None,
        smtp_port: Optional[int] = None,
        smtp_user: Optional[str] = None,
        smtp_password: Optional[str] = None,
        email_from: Optional[str] = None,
    ) -> None:
        self._host: str = smtp_host or os.environ.get(_ENV_SMTP_HOST) or "localhost"
        raw_port = smtp_port or os.environ.get(_ENV_SMTP_PORT) or "587"
        try:
            self._port: int = int(raw_port)
        except (TypeError, ValueError):
            self._port = 587
        self._user: str = smtp_user or os.environ.get(_ENV_SMTP_USER) or ""
        self._password: str = smtp_password or os.environ.get(_ENV_SMTP_PASSWORD) or ""
        self._from: str = email_from or os.environ.get(_ENV_EMAIL_FROM) or ""

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        if not is_email_tool_enabled():
            return ToolResult(
                ok=False,
                output=(
                    "Error: email tool is disabled. "
                    "Set ENABLE_EMAIL_TOOL=true and configure SMTP_* / EMAIL_FROM to enable."
                ),
            )

        to = str(request.args.get("to") or "").strip()
        subject = str(request.args.get("subject") or "").strip()
        body = str(request.args.get("body") or "").strip()

        if not to or not subject or not body:
            return ToolResult(ok=False, output="Error: 'to', 'subject', and 'body' are required.")

        if not self._from:
            return ToolResult(
                ok=False,
                output="Error: EMAIL_FROM is not configured. Set EMAIL_FROM env var.",
            )

        if bool(request.args.get("dry_run", False)):
            return ToolResult(
                ok=True,
                output=(
                    f"[dry_run] Would send email:\n"
                    f"From: {self._from}\n"
                    f"To: {to}\n"
                    f"Subject: {subject}\n"
                    f"Body:\n{body}"
                ),
            )

        try:
            msg = MIMEMultipart()
            msg["From"] = self._from
            msg["To"] = to
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain"))

            with smtplib.SMTP(self._host, self._port, timeout=30) as smtp:
                smtp.ehlo()
                if self._port == 587:
                    smtp.starttls()
                    smtp.ehlo()
                if self._user and self._password:
                    smtp.login(self._user, self._password)
                smtp.send_message(msg)

            logger.info("email_tool: sent to=%s subject=%s", to, subject)
            return ToolResult(ok=True, output=f"Email sent to {to} — subject: {subject}")

        except Exception as exc:
            logger.exception("email_tool: send failed to=%s", to)
            return ToolResult(ok=False, output=f"Error: email send failed: {exc}")
