from __future__ import annotations

import re
from pathlib import Path

from codex_telegram_bot.services.email_assets import EmailAssetsStore
from codex_telegram_bot.tools.base import ToolContext, ToolRequest, ToolResult
from codex_telegram_bot.tools.email import SendEmailSmtpTool

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _is_valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.match((value or "").strip()))


class EmailValidateTool:
    name = "email_validate"

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        email = str(request.args.get("email") or "").strip().lower()
        if not email:
            return ToolResult(ok=False, output="Error: 'email' is required.")
        valid = _is_valid_email(email)
        return ToolResult(ok=True, output=f"email={email} valid={'yes' if valid else 'no'}")


class ContactUpsertTool:
    name = "contact_upsert"

    def __init__(self, store: EmailAssetsStore):
        self._store = store

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        email = str(request.args.get("email") or "").strip().lower()
        name = str(request.args.get("name") or "").strip()
        if not email:
            return ToolResult(ok=False, output="Error: 'email' is required.")
        if not _is_valid_email(email):
            return ToolResult(ok=False, output="Error: invalid email format.")
        rec = self._store.upsert_contact(email=email, name=name)
        return ToolResult(ok=True, output=f"contact saved: {rec.email} name={rec.name or '-'}")


class ContactListTool:
    name = "contact_list"

    def __init__(self, store: EmailAssetsStore):
        self._store = store

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        rows = self._store.list_contacts()
        if not rows:
            return ToolResult(ok=True, output="contacts: none")
        lines = ["contacts:"]
        for row in rows:
            lines.append(f"- {row.email} name={row.name or '-'}")
        return ToolResult(ok=True, output="\n".join(lines))


class ContactRemoveTool:
    name = "contact_remove"

    def __init__(self, store: EmailAssetsStore):
        self._store = store

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        email = str(request.args.get("email") or "").strip().lower()
        if not email:
            return ToolResult(ok=False, output="Error: 'email' is required.")
        ok = self._store.remove_contact(email)
        return ToolResult(ok=ok, output=("contact removed" if ok else "Error: contact not found."))


class TemplateUpsertTool:
    name = "template_upsert"

    def __init__(self, store: EmailAssetsStore):
        self._store = store

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        template_id = str(request.args.get("template_id") or "").strip().lower()
        subject = str(request.args.get("subject") or "").strip()
        body = str(request.args.get("body") or "")
        if not template_id or not subject or not body:
            return ToolResult(ok=False, output="Error: template_id, subject, and body are required.")
        rec = self._store.upsert_template(template_id=template_id, subject=subject, body=body)
        return ToolResult(ok=True, output=f"template saved: {rec.template_id}")


class TemplateListTool:
    name = "template_list"

    def __init__(self, store: EmailAssetsStore):
        self._store = store

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        rows = self._store.list_templates()
        if not rows:
            return ToolResult(ok=True, output="templates: none")
        lines = ["templates:"]
        for row in rows:
            lines.append(f"- {row.template_id} subject={row.subject}")
        return ToolResult(ok=True, output="\n".join(lines))


class TemplateGetTool:
    name = "template_get"

    def __init__(self, store: EmailAssetsStore):
        self._store = store

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        template_id = str(request.args.get("template_id") or "").strip().lower()
        if not template_id:
            return ToolResult(ok=False, output="Error: template_id is required.")
        row = self._store.get_template(template_id)
        if row is None:
            return ToolResult(ok=False, output="Error: template not found.")
        return ToolResult(ok=True, output=f"template={row.template_id}\nsubject={row.subject}\nbody:\n{row.body}")


class TemplateDeleteTool:
    name = "template_delete"

    def __init__(self, store: EmailAssetsStore):
        self._store = store

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        template_id = str(request.args.get("template_id") or "").strip().lower()
        if not template_id:
            return ToolResult(ok=False, output="Error: template_id is required.")
        ok = self._store.remove_template(template_id)
        return ToolResult(ok=ok, output=("template deleted" if ok else "Error: template not found."))


class SendEmailTemplateTool:
    name = "send_email_template"

    def __init__(self, store: EmailAssetsStore):
        self._store = store
        self._smtp = SendEmailSmtpTool()

    def run(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        template_id = str(request.args.get("template_id") or "").strip().lower()
        to_addr = str(request.args.get("to") or "").strip().lower()
        if not template_id or not to_addr:
            return ToolResult(ok=False, output="Error: template_id and to are required.")
        if not _is_valid_email(to_addr):
            return ToolResult(ok=False, output="Error: invalid recipient email format.")
        row = self._store.get_template(template_id)
        if row is None:
            return ToolResult(ok=False, output="Error: template not found.")
        smtp_req = ToolRequest(
            name="send_email_smtp",
            args={
                "to": to_addr,
                "subject": row.subject,
                "body": row.body,
                "dry_run": bool(request.args.get("dry_run", False)),
            },
        )
        return self._smtp.run(smtp_req, context)
