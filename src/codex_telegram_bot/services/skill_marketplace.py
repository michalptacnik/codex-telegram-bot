from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import parse, request

from codex_telegram_bot.services.skill_pack import parse_skill_md


CATALOG_REFRESH_TTL_HOURS = 6


@dataclass(frozen=True)
class SkillSource:
    name: str
    type: str
    repo: str = ""
    path: str = ""
    url: str = ""
    ref: str = "main"


def default_skill_sources() -> List[SkillSource]:
    return [
        SkillSource(
            name="openai-skills",
            type="github_repo",
            repo="openai/skills",
            path=".agents/skills",
            ref="main",
        ),
    ]


def load_skill_sources() -> List[SkillSource]:
    raw = (os.environ.get("SKILL_SOURCES_JSON") or "").strip()
    if not raw:
        return default_skill_sources()
    try:
        payload = json.loads(raw)
    except Exception:
        return default_skill_sources()
    if not isinstance(payload, list):
        return default_skill_sources()
    out: List[SkillSource] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip().lower()
        source_type = str(row.get("type") or "").strip().lower()
        if not name or not source_type:
            continue
        out.append(
            SkillSource(
                name=name,
                type=source_type,
                repo=str(row.get("repo") or "").strip(),
                path=str(row.get("path") or "").strip(),
                url=str(row.get("url") or "").strip(),
                ref=str(row.get("ref") or "main").strip() or "main",
            )
        )
    return out or default_skill_sources()


class SkillMarketplace:
    def __init__(
        self,
        *,
        store: Any = None,
        skill_manager: Any = None,
        workspace_root: Optional[Path] = None,
        config_dir: Optional[Path] = None,
    ) -> None:
        self._store = store
        self._skill_manager = skill_manager
        self._workspace_root = (
            Path(workspace_root).expanduser().resolve()
            if workspace_root is not None
            else Path.cwd().resolve()
        )
        self._config_dir = (
            Path(config_dir).expanduser().resolve()
            if config_dir is not None
            else (Path.home() / ".config" / "codex-telegram-bot").resolve()
        )
        self._sources = load_skill_sources()

    def sources_list(self) -> List[Dict[str, Any]]:
        out = []
        for src in self._sources:
            out.append(
                {
                    "name": src.name,
                    "type": src.type,
                    "repo": src.repo,
                    "path": src.path,
                    "url": src.url,
                    "ref": src.ref,
                }
            )
        return out

    def search(self, query: str, source: str = "", refresh: bool = False, limit: int = 50) -> List[Dict[str, Any]]:
        normalized_source = str(source or "").strip().lower()
        if refresh:
            self.refresh(source_name=normalized_source or None, force=True)
        elif self._is_cache_stale(normalized_source):
            self.refresh(source_name=normalized_source or None, force=False)
        if self._store is None:
            return []
        return self._store.list_skill_catalog_entries(
            query=query,
            source_name=normalized_source,
            limit=limit,
        )

    def refresh(self, source_name: Optional[str] = None, force: bool = False) -> Dict[str, int]:
        if self._store is None:
            return {"sources": 0, "entries": 0}
        names = {str(source_name or "").strip().lower()} if source_name else {s.name for s in self._sources}
        total_entries = 0
        done_sources = 0
        for src in self._sources:
            if src.name not in names:
                continue
            if (not force) and (not self._source_stale(src.name)):
                continue
            entries = self._fetch_source_entries(src)
            self._store.clear_skill_catalog_source(src.name)
            self._store.upsert_skill_catalog_entries(src.name, entries)
            total_entries += len(entries)
            done_sources += 1
        return {"sources": done_sources, "entries": total_entries}

    def install(self, skill_ref: str, target: str = "workspace") -> Dict[str, Any]:
        normalized_target = str(target or "workspace").strip().lower()
        if normalized_target not in {"workspace", "global"}:
            raise ValueError("target must be 'workspace' or 'global'")
        entry = self._resolve_entry(skill_ref)
        install_ref = dict(entry.get("install_ref") or {})
        bundle = self._fetch_skill_bundle(install_ref)
        skill_md = bundle.get("SKILL.md")
        if skill_md is None:
            raise ValueError("install bundle missing SKILL.md")
        parsed = parse_skill_md(skill_md.decode("utf-8", errors="replace"))
        if parsed is None:
            raise ValueError("SKILL.md frontmatter could not be parsed")
        skill_id = parsed.skill_id
        dest_root = (
            self._workspace_root / ".skills" / "marketplace"
            if normalized_target == "workspace"
            else self._config_dir / "skills" / "packs" / "marketplace"
        )
        skill_root = dest_root / skill_id
        if skill_root.exists():
            shutil.rmtree(skill_root, ignore_errors=True)
        skill_root.mkdir(parents=True, exist_ok=True)
        hashes: Dict[str, str] = {}
        for rel_path, payload in bundle.items():
            clean_rel = str(rel_path).strip().lstrip("/").replace("\\", "/")
            if not clean_rel or ".." in Path(clean_rel).parts:
                raise ValueError("invalid bundle path")
            out_path = (skill_root / clean_rel).resolve()
            if not str(out_path).startswith(str(skill_root.resolve())):
                raise ValueError("bundle path escapes install root")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(payload)
            hashes[clean_rel] = hashlib.sha256(payload).hexdigest()
        metadata = {
            "skill_id": skill_id,
            "source_name": entry.get("source_name") or "",
            "entry_id": entry.get("id") or "",
            "install_ref": install_ref,
            "hashes": hashes,
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "target": normalized_target,
        }
        (skill_root / ".marketplace.json").write_text(
            json.dumps(metadata, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        if self._skill_manager is not None:
            self._skill_manager.upsert_instruction_skill(
                skill_id=skill_id,
                name=parsed.name,
                description=parsed.description,
                keywords=list(parsed.keywords),
                source=f"market:{entry.get('source_name')}",
                version=str(entry.get("version") or ""),
                tags=list(entry.get("tags") or []),
                install_ref=install_ref,
                sha256_manifest=hashes,
                enabled=False,
            )
        return {
            "skill_id": skill_id,
            "installed_path": str(skill_root),
            "target": normalized_target,
            "hash_count": len(hashes),
        }

    def enable(self, skill_id: str) -> Dict[str, Any]:
        self._verify_hashes(skill_id)
        if self._skill_manager is None:
            raise ValueError("Skill manager is not configured.")
        row = self._skill_manager.enable(skill_id=skill_id, enabled=True)
        if row is None:
            raise ValueError("Skill not found.")
        return {"skill_id": row.skill_id, "enabled": True}

    def disable(self, skill_id: str) -> Dict[str, Any]:
        if self._skill_manager is None:
            raise ValueError("Skill manager is not configured.")
        row = self._skill_manager.enable(skill_id=skill_id, enabled=False)
        if row is None:
            raise ValueError("Skill not found.")
        return {"skill_id": row.skill_id, "enabled": False}

    def remove(self, skill_id: str) -> Dict[str, Any]:
        removed = 0
        for base in [
            self._workspace_root / ".skills" / "marketplace",
            self._config_dir / "skills" / "packs" / "marketplace",
        ]:
            target = base / skill_id
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
                removed += 1
        if self._skill_manager is not None:
            self._skill_manager.enable(skill_id=skill_id, enabled=False)
        return {"skill_id": skill_id, "removed": bool(removed)}

    def _resolve_entry(self, skill_ref: str) -> Dict[str, Any]:
        if self._store is None:
            raise ValueError("Skill catalog store is not configured.")
        raw = str(skill_ref or "").strip()
        if not raw:
            raise ValueError("skill_ref is required")
        candidates = self._store.list_skill_catalog_entries(query="", source_name="", limit=1000)
        for item in candidates:
            if str(item.get("id") or "") == raw:
                return item
        if raw.startswith("{") and raw.endswith("}"):
            obj = json.loads(raw)
            return {
                "id": "inline",
                "source_name": "inline",
                "skill_name": str(obj.get("skill_name") or obj.get("id") or "inline-skill"),
                "version": str(obj.get("version") or ""),
                "description": str(obj.get("description") or ""),
                "tags": list(obj.get("tags") or []),
                "install_ref": dict(obj.get("install_ref") or obj),
                "last_fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        raise ValueError("skill_ref not found in catalog")

    def _is_cache_stale(self, source_name: str) -> bool:
        if self._store is None:
            return False
        if source_name:
            return self._source_stale(source_name)
        for src in self._sources:
            if self._source_stale(src.name):
                return True
        return False

    def _source_stale(self, source_name: str) -> bool:
        if self._store is None:
            return False
        rows = self._store.list_skill_catalog_entries(query="", source_name=source_name, limit=1)
        if not rows:
            return True
        latest = str(rows[0].get("last_fetched_at") or "")
        if not latest:
            return True
        try:
            dt = datetime.fromisoformat(latest)
        except Exception:
            return True
        return (datetime.now(timezone.utc) - dt) > timedelta(hours=CATALOG_REFRESH_TTL_HOURS)

    def _fetch_source_entries(self, src: SkillSource) -> List[Dict[str, Any]]:
        if src.type == "github_repo":
            return self._fetch_source_entries_github_repo(src)
        if src.type == "url_index":
            return self._fetch_source_entries_url_index(src)
        return []

    def _fetch_source_entries_url_index(self, src: SkillSource) -> List[Dict[str, Any]]:
        if not src.url:
            return []
        payload = json.loads(self._http_get_text(src.url))
        if not isinstance(payload, list):
            return []
        out: List[Dict[str, Any]] = []
        now = datetime.now(timezone.utc).isoformat()
        for item in payload:
            if not isinstance(item, dict):
                continue
            skill_name = str(item.get("skill_name") or item.get("id") or "").strip()
            if not skill_name:
                continue
            out.append(
                {
                    "id": f"{src.name}:{skill_name.lower()}",
                    "source_name": src.name,
                    "skill_name": skill_name,
                    "version": str(item.get("version") or ""),
                    "description": str(item.get("description") or ""),
                    "tags": list(item.get("tags") or []),
                    "install_ref": dict(item.get("install_ref") or {}),
                    "last_fetched_at": now,
                }
            )
        return out

    def _fetch_source_entries_github_repo(self, src: SkillSource) -> List[Dict[str, Any]]:
        if not src.repo or not src.path:
            return []
        listing_url = f"https://api.github.com/repos/{src.repo}/contents/{src.path}?ref={src.ref}"
        listing = json.loads(self._http_get_text(listing_url))
        if not isinstance(listing, list):
            return []
        out: List[Dict[str, Any]] = []
        now = datetime.now(timezone.utc).isoformat()
        for item in listing:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "") != "dir":
                continue
            dir_name = str(item.get("name") or "").strip()
            if not dir_name:
                continue
            skill_md_url = f"https://api.github.com/repos/{src.repo}/contents/{src.path}/{dir_name}/SKILL.md?ref={src.ref}"
            try:
                md_obj = json.loads(self._http_get_text(skill_md_url))
            except Exception:
                continue
            if not isinstance(md_obj, dict):
                continue
            content_b64 = str(md_obj.get("content") or "").strip()
            if not content_b64:
                continue
            try:
                md_text = base64.b64decode(content_b64).decode("utf-8", errors="replace")
            except Exception:
                continue
            parsed_skill = parse_skill_md(md_text, source="catalog", source_path=f"{src.path}/{dir_name}/SKILL.md")
            if parsed_skill is None:
                continue
            install_ref = {
                "type": "github_repo_skill",
                "repo": src.repo,
                "path": f"{src.path}/{dir_name}",
                "ref": src.ref,
            }
            out.append(
                {
                    "id": f"{src.name}:{parsed_skill.skill_id}",
                    "source_name": src.name,
                    "skill_name": parsed_skill.skill_id,
                    "version": "",
                    "description": parsed_skill.description or parsed_skill.name,
                    "tags": list(parsed_skill.keywords or []),
                    "install_ref": install_ref,
                    "last_fetched_at": now,
                }
            )
        return out

    def _fetch_skill_bundle(self, install_ref: Dict[str, Any]) -> Dict[str, bytes]:
        ref_type = str(install_ref.get("type") or "").strip().lower()
        if ref_type != "github_repo_skill":
            raise ValueError("unsupported install_ref type")
        repo = str(install_ref.get("repo") or "").strip()
        base_path = str(install_ref.get("path") or "").strip().strip("/")
        ref = str(install_ref.get("ref") or "main").strip() or "main"
        if not repo or not base_path:
            raise ValueError("install_ref repo/path required")
        return self._download_github_tree(repo=repo, base_path=base_path, ref=ref)

    def _download_github_tree(self, *, repo: str, base_path: str, ref: str) -> Dict[str, bytes]:
        out: Dict[str, bytes] = {}
        queue = [base_path]
        while queue:
            current = queue.pop(0)
            listing_url = f"https://api.github.com/repos/{repo}/contents/{current}?ref={ref}"
            listing = json.loads(self._http_get_text(listing_url))
            if not isinstance(listing, list):
                raise ValueError("invalid github contents response")
            for item in listing:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "")
                item_path = str(item.get("path") or "")
                if item_type == "dir":
                    queue.append(item_path)
                    continue
                if item_type != "file":
                    continue
                download_url = str(item.get("download_url") or "")
                if not download_url:
                    continue
                rel = item_path[len(base_path):].lstrip("/")
                if not rel:
                    continue
                out[rel] = self._http_get_bytes(download_url)
        return out

    def _http_get_text(self, url: str) -> str:
        with request.urlopen(request.Request(url, headers={"User-Agent": "codex-telegram-bot"}), timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def _http_get_bytes(self, url: str) -> bytes:
        with request.urlopen(request.Request(url, headers={"User-Agent": "codex-telegram-bot"}), timeout=20) as resp:
            return resp.read()

    def _verify_hashes(self, skill_id: str) -> None:
        found = False
        for base in [
            self._workspace_root / ".skills" / "marketplace",
            self._config_dir / "skills" / "packs" / "marketplace",
        ]:
            meta_path = base / skill_id / ".marketplace.json"
            if not meta_path.exists():
                continue
            found = True
            payload = json.loads(meta_path.read_text(encoding="utf-8") or "{}")
            hashes = dict(payload.get("hashes") or {})
            root = meta_path.parent
            for rel, expected in hashes.items():
                p = (root / rel).resolve()
                if not str(p).startswith(str(root.resolve())):
                    raise ValueError("hash verification path escape")
                if not p.exists() or not p.is_file():
                    raise ValueError(f"missing file during verify: {rel}")
                actual = hashlib.sha256(p.read_bytes()).hexdigest()
                if actual != expected:
                    raise ValueError(f"hash mismatch for {rel}")
        if not found:
            raise ValueError("installed skill metadata not found")
