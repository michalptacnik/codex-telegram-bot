from __future__ import annotations

import os
import socket
import smtplib
import time
from email.message import EmailMessage

from codex_telegram_bot.tools.base import ToolContext, ToolRequest, ToolResult


class SendEmailSmtpTool:
    """Send outbound email through SMTP using app-password style auth."""

    name = "send_email_smtp"
    _MAX_ATTEMPTS = 3

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        profile = (getattr(context, "policy_profile", "balanced") or "balanced").strip().lower()
        if profile == "strict":
            return ToolResult(ok=False, output="Error: outbound email blocked by strict policy profile.")

        to_addr = str(request.args.get("to") or "").strip()
        subject = str(request.args.get("subject") or "").strip()
        body = str(request.args.get("body") or "").strip()
        if not to_addr or not subject or not body:
            return ToolResult(ok=False, output="Error: 'to', 'subject', and 'body' are required.")

        smtp_host = str(request.args.get("smtp_host") or os.environ.get("SMTP_HOST") or "").strip()
        smtp_port_raw = request.args.get("smtp_port") or os.environ.get("SMTP_PORT") or 587
        smtp_user = str(request.args.get("smtp_user") or os.environ.get("SMTP_USER") or "").strip()
        smtp_password = "".join(
            str(request.args.get("smtp_password") or os.environ.get("SMTP_APP_PASSWORD") or "").split()
        )
        from_addr = str(request.args.get("from") or os.environ.get("SMTP_FROM") or smtp_user).strip()
        dry_run = bool(request.args.get("dry_run", False))

        if not smtp_host or not smtp_user or not smtp_password or not from_addr:
            return ToolResult(
                ok=False,
                output=(
                    "Error: missing SMTP configuration. "
                    "Need SMTP_HOST, SMTP_USER, SMTP_APP_PASSWORD, SMTP_FROM (or smtp args)."
                ),
            )
        try:
            smtp_port = int(smtp_port_raw)
        except Exception:
            smtp_port = 587
        smtp_port = max(1, min(65535, smtp_port))

        if dry_run:
            return ToolResult(
                ok=True,
                output=(
                    "[dry_run] Would send email\n"
                    f"from: {from_addr}\n"
                    f"to: {to_addr}\n"
                    f"subject: {subject}\n"
                    f"smtp: {smtp_host}:{smtp_port}"
                ),
            )

        msg = EmailMessage()
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.set_content(body)

        last_exc: Exception | None = None
        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            try:
                if smtp_port == 465:
                    with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=25) as server:
                        server.login(smtp_user, smtp_password)
                        server.send_message(msg)
                else:
                    with smtplib.SMTP(smtp_host, smtp_port, timeout=25) as server:
                        server.starttls()
                        server.login(smtp_user, smtp_password)
                        server.send_message(msg)
                return ToolResult(ok=True, output=f"Email sent to {to_addr} with subject '{subject}'.")
            except Exception as exc:
                last_exc = exc
                transient = isinstance(
                    exc, (socket.gaierror, TimeoutError, ConnectionError, smtplib.SMTPServerDisconnected, OSError)
                )
                if transient and attempt < self._MAX_ATTEMPTS:
                    time.sleep(1.2 * attempt)
                    continue
                break
        return ToolResult(ok=False, output=f"Error: SMTP send failed after {self._MAX_ATTEMPTS} attempt(s): {last_exc}")
