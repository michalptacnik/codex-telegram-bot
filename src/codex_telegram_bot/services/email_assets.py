from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


@dataclass(frozen=True)
class ContactRecord:
    email: str
    name: str
    updated_at: str


@dataclass(frozen=True)
class TemplateRecord:
    template_id: str
    subject: str
    body: str
    updated_at: str


class EmailAssetsStore:
    def __init__(self, config_dir: Path):
        self._config_dir = config_dir.expanduser().resolve()
        self._root = self._config_dir / "skills" / "email"
        self._contacts_path = self._root / "contacts.json"
        self._templates_path = self._root / "templates.json"
        self._root.mkdir(parents=True, exist_ok=True)
        self._ensure_seed_files()

    def list_contacts(self) -> List[ContactRecord]:
        data = self._load_json(self._contacts_path, "contacts")
        out: List[ContactRecord] = []
        for _, row in sorted(data.get("contacts", {}).items()):
            out.append(
                ContactRecord(
                    email=str(row.get("email") or "").strip().lower(),
                    name=str(row.get("name") or "").strip(),
                    updated_at=str(row.get("updated_at") or ""),
                )
            )
        return out

    def upsert_contact(self, email: str, name: str = "") -> ContactRecord:
        normalized = (email or "").strip().lower()
        now = _utc_now()
        data = self._load_json(self._contacts_path, "contacts")
        data.setdefault("contacts", {})[normalized] = {
            "email": normalized,
            "name": (name or "").strip(),
            "updated_at": now,
        }
        self._write_json(self._contacts_path, data)
        return ContactRecord(email=normalized, name=(name or "").strip(), updated_at=now)

    def remove_contact(self, email: str) -> bool:
        normalized = (email or "").strip().lower()
        data = self._load_json(self._contacts_path, "contacts")
        contacts = data.setdefault("contacts", {})
        if normalized not in contacts:
            return False
        del contacts[normalized]
        self._write_json(self._contacts_path, data)
        return True

    def get_contact(self, email: str) -> Optional[ContactRecord]:
        normalized = (email or "").strip().lower()
        data = self._load_json(self._contacts_path, "contacts")
        row = data.get("contacts", {}).get(normalized)
        if not row:
            return None
        return ContactRecord(
            email=str(row.get("email") or "").strip().lower(),
            name=str(row.get("name") or "").strip(),
            updated_at=str(row.get("updated_at") or ""),
        )

    def list_templates(self) -> List[TemplateRecord]:
        data = self._load_json(self._templates_path, "templates")
        out: List[TemplateRecord] = []
        for _, row in sorted(data.get("templates", {}).items()):
            out.append(
                TemplateRecord(
                    template_id=str(row.get("template_id") or "").strip().lower(),
                    subject=str(row.get("subject") or "").strip(),
                    body=str(row.get("body") or ""),
                    updated_at=str(row.get("updated_at") or ""),
                )
            )
        return out

    def upsert_template(self, template_id: str, subject: str, body: str) -> TemplateRecord:
        tid = (template_id or "").strip().lower()
        now = _utc_now()
        data = self._load_json(self._templates_path, "templates")
        data.setdefault("templates", {})[tid] = {
            "template_id": tid,
            "subject": (subject or "").strip(),
            "body": body or "",
            "updated_at": now,
        }
        self._write_json(self._templates_path, data)
        return TemplateRecord(template_id=tid, subject=(subject or "").strip(), body=body or "", updated_at=now)

    def get_template(self, template_id: str) -> Optional[TemplateRecord]:
        tid = (template_id or "").strip().lower()
        data = self._load_json(self._templates_path, "templates")
        row = data.get("templates", {}).get(tid)
        if not row:
            return None
        return TemplateRecord(
            template_id=str(row.get("template_id") or "").strip().lower(),
            subject=str(row.get("subject") or "").strip(),
            body=str(row.get("body") or ""),
            updated_at=str(row.get("updated_at") or ""),
        )

    def remove_template(self, template_id: str) -> bool:
        tid = (template_id or "").strip().lower()
        data = self._load_json(self._templates_path, "templates")
        templates = data.setdefault("templates", {})
        if tid not in templates:
            return False
        del templates[tid]
        self._write_json(self._templates_path, data)
        return True

    def _ensure_seed_files(self) -> None:
        if not self._contacts_path.exists():
            self._write_json(self._contacts_path, {"contacts": {}})
        if not self._templates_path.exists():
            self._write_json(self._templates_path, {"templates": {}})

    def _load_json(self, path: Path, top_key: str) -> Dict[str, object]:
        if not path.exists():
            return {top_key: {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except Exception:
            return {top_key: {}}
        if not isinstance(data, dict):
            return {top_key: {}}
        if not isinstance(data.get(top_key), dict):
            data[top_key] = {}
        return data

    def _write_json(self, path: Path, payload: Dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
