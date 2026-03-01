import asyncio
from dataclasses import asdict
import os
import secrets
from pathlib import Path
from typing import Any, Dict, List, Set
from xml.sax.saxutils import escape as xml_escape

from pydantic import BaseModel
from fastapi import FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from codex_telegram_bot.services.agent_service import AgentService, AUTONOMOUS_TOOL_LOOP_ENV
from codex_telegram_bot.services.error_codes import ERROR_CATALOG, detect_error_code, get_catalog_entry
from codex_telegram_bot.services.onboarding import OnboardingStore
from codex_telegram_bot.services.plugin_lifecycle import PluginLifecycleManager
from codex_telegram_bot.services.whatsapp_bridge import WhatsAppBridge
from codex_telegram_bot.services.whatsapp_transport import build_twilio_whatsapp_sender_from_env
from codex_telegram_bot.config import get_env_path, load_env_file, write_env_file


class HandoffRequest(BaseModel):
    from_agent_id: str
    to_agent_id: str
    prompt: str
    parent_run_id: str = ""


class RecoveryRequest(BaseModel):
    action_id: str


class ApproveToolRequest(BaseModel):
    approval_id: str
    chat_id: int
    user_id: int


class LocalApiPromptRequest(BaseModel):
    prompt: str
    agent_id: str = "default"


class PluginInstallRequest(BaseModel):
    manifest_path: str
    enable: bool = False


class PluginUpdateRequest(BaseModel):
    manifest_path: str


class SkillInstallRequest(BaseModel):
    source_url: str


class ExecutionProfileRequest(BaseModel):
    profile: str
    user_id: int = 0


class UnsafeUnlockConfirmRequest(BaseModel):
    code: str
    phrase: str
    user_id: int = 0


class WhatsAppLinkCodeRequest(BaseModel):
    chat_id: int
    user_id: int
    ttl_sec: int = 900


def _chunk_text(text: str, chunk_size: int = 220) -> List[str]:
    raw = str(text or "")
    if not raw:
        return []
    size = max(80, min(int(chunk_size or 220), 1200))
    return [raw[i : i + size] for i in range(0, len(raw), size)]


def _run_to_dict(run) -> Dict[str, Any]:
    data = asdict(run)
    for key in ("created_at", "started_at", "completed_at"):
        if data.get(key) is not None:
            data[key] = data[key].isoformat()
    if data.get("error"):
        code = detect_error_code(data["error"])
        catalog = get_catalog_entry(code)
        data["error_code"] = code
        data["error_title"] = catalog.title
        data["error_message"] = catalog.user_message
    else:
        data["error_code"] = ""
        data["error_title"] = ""
        data["error_message"] = ""
    return data


def _event_to_dict(event) -> Dict[str, Any]:
    return {
        "run_id": event.run_id,
        "event_type": event.event_type,
        "payload": event.payload,
        "created_at": event.created_at.isoformat(),
    }


def _catalog_to_dict() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for entry in ERROR_CATALOG:
        out.append(
            {
                "code": entry.code,
                "title": entry.title,
                "user_message": entry.user_message,
                "actions": [
                    {
                        "action_id": action.action_id,
                        "label": action.label,
                        "description": action.description,
                    }
                    for action in entry.actions
                ],
            }
        )
    return out


def _allowed_recovery_actions() -> Dict[str, str]:
    return {
        "retry_same_agent": "Queue the same prompt on the original agent.",
        "retry_default_agent": "Queue the same prompt on the default agent.",
        "open_settings": "Open settings page for provider diagnostics.",
        "open_agents": "Open agents page for profile/provider fixes.",
        "download_artifact": "Download run artifact for triage.",
    }


def _plugin_to_dict(plugin) -> Dict[str, Any]:
    return {
        "plugin_id": plugin.plugin_id,
        "name": plugin.name,
        "version": plugin.version,
        "manifest_version": plugin.manifest_version,
        "requires_api_version": plugin.requires_api_version,
        "capabilities": plugin.capabilities,
        "enabled": plugin.enabled,
        "trust_status": plugin.trust_status,
        "manifest_path": plugin.manifest_path,
        "created_at": plugin.created_at.isoformat(),
        "updated_at": plugin.updated_at.isoformat(),
    }


def _plugin_audit_to_dict(event) -> Dict[str, Any]:
    return {
        "ts": event.ts.isoformat(),
        "action": event.action,
        "plugin_id": event.plugin_id,
        "outcome": event.outcome,
        "details": event.details,
    }


def _parse_local_api_keys(raw: str) -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = {}
    for chunk in (raw or "").split(";"):
        value = chunk.strip()
        if not value:
            continue
        parts = value.split(":", 1)
        if len(parts) != 2:
            continue
        token = parts[0].strip()
        scopes_raw = parts[1].strip()
        if not token:
            continue
        scopes = {s.strip().lower() for s in scopes_raw.split(",") if s.strip()}
        if not scopes:
            scopes = {"runs:read"}
        out[token] = scopes
    return out


def _env_flag_enabled(value: str) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_checkbox_flag(value: str) -> str:
    return "1" if _env_flag_enabled(value) else "0"


def _twiml_message(text: str) -> str:
    safe = xml_escape(str(text or "").strip()[:1400])
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{safe}</Message></Response>'


def create_app(agent_service: AgentService, provider_registry=None, metrics_collector=None) -> FastAPI:
    return create_app_with_config(
        agent_service=agent_service,
        config_dir=None,
        provider_registry=provider_registry,
        metrics_collector=metrics_collector,
    )


def create_app_with_config(
    agent_service: AgentService,
    config_dir: "Path | None",
    provider_registry=None,
    metrics_collector=None,
) -> FastAPI:
    if provider_registry is None:
        provider_registry = agent_service.provider_registry()
    base_dir = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=str(base_dir / "templates"))
    app = FastAPI(title="Codex Control Center", version="0.2.0")
    app.mount("/static", StaticFiles(directory=str(base_dir / "static")), name="static")
    onboarding = OnboardingStore(config_dir=config_dir)
    plugin_manager = PluginLifecycleManager(config_dir=config_dir or (Path.cwd() / ".codex-telegram-bot"))
    onboarding_key_env = (os.environ.get("ONBOARDING_PROVIDER_KEY_ENV") or "OPENAI_API_KEY").strip() or "OPENAI_API_KEY"
    provider_key_hint = f"Optional: set {onboarding_key_env} for provider runtime."
    local_api_version = "v1"
    local_api_keys = _parse_local_api_keys(os.environ.get("LOCAL_API_KEYS", ""))
    local_api_enabled = bool(local_api_keys)

    def _resolve_api_token(request: Request) -> str:
        bearer = (request.headers.get("authorization") or "").strip()
        if bearer.lower().startswith("bearer "):
            return bearer[7:].strip()
        return (request.headers.get("x-local-api-key") or "").strip()

    def _require_local_api_scope(request: Request, required_scope: str) -> str:
        if not local_api_enabled:
            raise HTTPException(status_code=503, detail="Local integration API disabled.")
        token = _resolve_api_token(request)
        if not token:
            raise HTTPException(status_code=401, detail="Missing local API token.")
        scopes = local_api_keys.get(token)
        if not scopes:
            raise HTTPException(status_code=401, detail="Invalid local API token.")
        if "admin:*" in scopes or required_scope in scopes:
            return token
        raise HTTPException(status_code=403, detail=f"Missing scope: {required_scope}")

    # ------------------------------------------------------------------
    # UI auth helpers (CONTROL_CENTER_UI_SECRET)
    # ------------------------------------------------------------------
    _UI_SECRET_ENV = "CONTROL_CENTER_UI_SECRET"
    _UI_COOKIE = "cc_ui_token"

    def _ui_auth_redirect(request: Request):
        """Return a RedirectResponse to /login if the UI secret is set and the
        request does not carry a valid session cookie.  Returns None when auth
        passes or when the feature is not configured (open / localhost-only mode)."""
        secret = os.environ.get(_UI_SECRET_ENV, "").strip()
        if not secret:
            return None
        token = request.cookies.get(_UI_COOKIE, "")
        if token == secret:
            return None
        next_path = str(request.url.path)
        return RedirectResponse(url=f"/login?next={next_path}", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(next: str = "/"):
        safe_next = next if next.startswith("/") else "/"
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Login – Codex Control Center</title>
  <style>
    body {{ font-family: system-ui, sans-serif; display: flex; align-items: center;
            justify-content: center; min-height: 100vh; margin: 0; background: #f3f4f6; }}
    .card {{ background: white; padding: 2rem; border-radius: 0.5rem;
             box-shadow: 0 1px 4px rgba(0,0,0,.12); width: 320px; }}
    h1 {{ font-size: 1.1rem; margin: 0 0 1.2rem; }}
    label {{ font-size: .875rem; color: #374151; display: block; margin-bottom: .3rem; }}
    input[type=password] {{ width: 100%; padding: .5rem .75rem; border: 1px solid #d1d5db;
                            border-radius: .375rem; font-size: .9rem; box-sizing: border-box; }}
    button {{ margin-top: 1rem; width: 100%; padding: .55rem; background: #2563eb;
              color: white; border: none; border-radius: .375rem; cursor: pointer;
              font-size: .9rem; }}
    button:hover {{ background: #1d4ed8; }}
    .err {{ color: #dc2626; font-size: .85rem; margin-top: .5rem; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Codex Control Center</h1>
    <form method="POST" action="/login">
      <input type="hidden" name="next" value="{safe_next}">
      <label for="secret">Access secret</label>
      <input type="password" id="secret" name="secret" autofocus required>
      <button type="submit">Sign in</button>
    </form>
  </div>
</body>
</html>"""
        return HTMLResponse(content=html)

    @app.post("/login")
    async def login_submit(request: Request, secret: str = Form(""), next: str = Form("/")):
        ui_secret = os.environ.get(_UI_SECRET_ENV, "").strip()
        safe_next = next if next.startswith("/") and next != "/login" else "/"
        if ui_secret and secret == ui_secret:
            resp = RedirectResponse(url=safe_next, status_code=303)
            resp.set_cookie(
                _UI_COOKIE, ui_secret,
                httponly=True, samesite="lax",
                max_age=60 * 60 * 24 * 7,  # 1 week
            )
            return resp
        html = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Login – Codex Control Center</title>
<style>body{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;
min-height:100vh;margin:0;background:#f3f4f6;}
.card{background:white;padding:2rem;border-radius:.5rem;box-shadow:0 1px 4px rgba(0,0,0,.12);width:320px;}
h1{font-size:1.1rem;margin:0 0 1.2rem;}label{font-size:.875rem;color:#374151;display:block;margin-bottom:.3rem;}
input[type=password]{width:100%;padding:.5rem .75rem;border:1px solid #d1d5db;border-radius:.375rem;
font-size:.9rem;box-sizing:border-box;}
button{margin-top:1rem;width:100%;padding:.55rem;background:#2563eb;color:white;border:none;
border-radius:.375rem;cursor:pointer;font-size:.9rem;}
.err{color:#dc2626;font-size:.85rem;margin-top:.5rem;}</style></head>
<body><div class="card"><h1>Codex Control Center</h1>
<form method="POST" action="/login">
<input type="hidden" name="next" value="/"><label for="secret">Access secret</label>
<input type="password" id="secret" name="secret" autofocus required>
<button type="submit">Sign in</button></form>
<p class="err">Incorrect secret. Please try again.</p>
</div></body></html>"""
        return HTMLResponse(content=html, status_code=401)

    @app.get("/logout")
    async def logout():
        resp = RedirectResponse(url="/login", status_code=303)
        resp.delete_cookie(_UI_COOKIE)
        return resp

    def _opt_api_scope(request: Request, read_scope: str = "api:read", write_scope: str = "api:write") -> None:
        """Optionally enforce API auth on non-v1 endpoints.

        When LOCAL_API_KEYS is configured all /api/* requests must carry a valid
        token.  The required scope is ``read_scope`` for safe (GET/HEAD) methods
        and ``write_scope`` for all mutating methods.  When LOCAL_API_KEYS is not
        configured the check is skipped so that local / dev deployments keep
        working without configuration.
        """
        if not local_api_enabled:
            return
        token = _resolve_api_token(request)
        if not token:
            raise HTTPException(status_code=401, detail="Missing API token.")
        scopes = local_api_keys.get(token)
        if not scopes:
            raise HTTPException(status_code=401, detail="Invalid API token.")
        if "admin:*" in scopes:
            return
        required = read_scope if request.method in {"GET", "HEAD", "OPTIONS"} else write_scope
        if required not in scopes:
            raise HTTPException(status_code=403, detail=f"Missing scope: {required}")

    def _provider_options() -> List[str]:
        if provider_registry is not None:
            try:
                raw = provider_registry.list_providers()
            except Exception:
                raw = []
            names = [str(item.get("name") or "").strip() for item in raw if isinstance(item, dict)]
            names = [n for n in names if n]
            if names:
                return names
        names = [n for n in agent_service.available_provider_names() if n]
        if names:
            return names
        return [agent_service.default_provider_name()]

    _chat_subscriptions: Dict[str, List[WebSocket]] = {}
    _chat_subscriptions_lock = asyncio.Lock()

    async def _chat_subscribe(session_id: str, websocket: WebSocket) -> None:
        if not session_id:
            return
        async with _chat_subscriptions_lock:
            bucket = _chat_subscriptions.setdefault(session_id, [])
            if not any(ws is websocket for ws in bucket):
                bucket.append(websocket)

    async def _chat_unsubscribe(session_id: str, websocket: WebSocket) -> None:
        if not session_id:
            return
        async with _chat_subscriptions_lock:
            bucket = _chat_subscriptions.get(session_id) or []
            kept = [ws for ws in bucket if ws is not websocket]
            if kept:
                _chat_subscriptions[session_id] = kept
            else:
                _chat_subscriptions.pop(session_id, None)

    async def _chat_broadcast_session(session_id: str, payload: Dict[str, Any]) -> None:
        if not session_id:
            return
        async with _chat_subscriptions_lock:
            sockets = list(_chat_subscriptions.get(session_id) or [])
        stale: List[WebSocket] = []
        for ws in sockets:
            try:
                await ws.send_json(payload)
            except Exception:
                stale.append(ws)
        if not stale:
            return
        async with _chat_subscriptions_lock:
            existing = list(_chat_subscriptions.get(session_id) or [])
            kept = [ws for ws in existing if not any(ws is bad for bad in stale)]
            if kept:
                _chat_subscriptions[session_id] = kept
            else:
                _chat_subscriptions.pop(session_id, None)

    def _ws_ui_authorized(websocket: WebSocket) -> bool:
        secret = os.environ.get(_UI_SECRET_ENV, "").strip()
        if not secret:
            return True
        cookie_token = (websocket.cookies.get(_UI_COOKIE) or "").strip()
        query_token = (websocket.query_params.get("token") or "").strip()
        return cookie_token == secret or query_token == secret

    def _resolve_ws_session(payload: Dict[str, Any], current_session_id: str):
        requested = str(payload.get("session_id") or current_session_id or "").strip()
        if requested:
            session = agent_service.get_session(requested)
            if session is not None:
                return session, ""
            return None, "Session not found."
        chat_raw = payload.get("chat_id")
        user_raw = payload.get("user_id")
        try:
            chat_id = int(chat_raw if chat_raw is not None else 1)
            user_id = int(user_raw if user_raw is not None else 1)
        except (TypeError, ValueError):
            return None, "chat_id and user_id must be integers."
        try:
            session = agent_service.get_or_create_session(chat_id=chat_id, user_id=user_id)
        except Exception as exc:
            return None, f"Failed to create session: {str(exc)[:200]}"
        return session, ""

    def _progress_to_tool_event(
        update_payload: Dict[str, Any],
        *,
        chat_id: int,
        user_id: int,
    ) -> Dict[str, Any] | None:
        event = str(update_payload.get("event") or "").strip()
        if not event:
            return None
        if event == "native_loop.tool_call":
            tool_name = str(update_payload.get("tool_name") or "tool")
            return {
                "type": "tool_event",
                "name": tool_name,
                "status": "called",
                "detail": {
                    "tool_use_id": str(update_payload.get("tool_use_id") or ""),
                    "turn": int(update_payload.get("turn") or 0),
                },
            }
        if event == "loop.step.started":
            return {
                "type": "tool_event",
                "name": str(update_payload.get("command") or "step"),
                "status": "called",
                "detail": {
                    "step": int(update_payload.get("step") or 0),
                    "action_id": str(update_payload.get("action_id") or ""),
                },
            }
        if event == "loop.step.awaiting_approval":
            approval_id = str(update_payload.get("approval_id") or "")
            return {
                "type": "tool_event",
                "name": "approval",
                "status": "awaiting_approval",
                "detail": {
                    "step": int(update_payload.get("step") or 0),
                    "action_id": str(update_payload.get("action_id") or ""),
                    "approval_id": approval_id,
                    "chat_id": int(chat_id),
                    "user_id": int(user_id),
                },
            }
        if event == "loop.step.completed":
            rc = int(update_payload.get("returncode") or 0)
            return {
                "type": "tool_event",
                "name": str(update_payload.get("action_id") or "step"),
                "status": "result" if rc == 0 else "error",
                "detail": {
                    "step": int(update_payload.get("step") or 0),
                    "action_id": str(update_payload.get("action_id") or ""),
                    "returncode": rc,
                },
            }
        if event == "loop.step.blocked":
            return {
                "type": "tool_event",
                "name": str(update_payload.get("action_id") or "step"),
                "status": "error",
                "detail": {
                    "step": int(update_payload.get("step") or 0),
                    "action_id": str(update_payload.get("action_id") or ""),
                    "risk_tier": str(update_payload.get("risk_tier") or ""),
                },
            }
        return None

    async def _deliver_websocket_proactive(payload: Dict[str, Any]) -> None:
        session_id = str(payload.get("session_id") or "").strip()
        text = str(payload.get("text") or "").strip()
        if not session_id or not text:
            return
        await _chat_broadcast_session(
            session_id,
            {
                "type": "assistant_chunk",
                "session_id": session_id,
                "text": text,
                "proactive": True,
            },
        )
        await _chat_broadcast_session(
            session_id,
            {
                "type": "done",
                "session_id": session_id,
                "proactive": True,
            },
        )

    agent_service.register_proactive_transport("websocket", _deliver_websocket_proactive)
    whatsapp_bridge: WhatsAppBridge | None = None
    if _env_flag_enabled(os.environ.get("WHATSAPP_ENABLED", "1")):
        run_store = getattr(agent_service, "run_store", None)
        if run_store is not None:
            whatsapp_sender = build_twilio_whatsapp_sender_from_env()
            whatsapp_bridge = WhatsAppBridge(
                agent_service=agent_service,
                run_store=run_store,
                sender=whatsapp_sender,
            )
            if whatsapp_sender is not None:
                agent_service.register_proactive_transport("whatsapp", whatsapp_bridge.deliver_proactive)

    def _whatsapp_webhook_authorized(request: Request) -> bool:
        expected = (os.environ.get("WHATSAPP_WEBHOOK_TOKEN") or "").strip()
        if not expected:
            return True
        supplied = (
            (request.headers.get("x-codex-webhook-token") or "").strip()
            or (request.query_params.get("token") or "").strip()
        )
        return bool(supplied) and secrets.compare_digest(supplied, expected)

    @app.on_event("startup")
    async def _startup_background_scheduler() -> None:
        async def _cron_loop() -> None:
            while True:
                try:
                    await agent_service.run_cron_tick_once()
                except Exception:
                    pass
                await asyncio.sleep(60)

        app.state.cron_task = asyncio.create_task(_cron_loop(), name="control-center-cron-tick")

    @app.on_event("shutdown")
    async def _shutdown_background_scheduler() -> None:
        task = getattr(app.state, "cron_task", None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        metrics = agent_service.metrics()
        reliability = agent_service.reliability_snapshot(limit=300)
        runtime = agent_service.runtime_capabilities()
        provider_version = await agent_service.provider_version()
        provider_health = await agent_service.provider_health()
        return {
            "status": "ok",
            "provider_version": provider_version,
            "provider_health": provider_health,
            "runtime": runtime,
            "metrics": metrics,
            "reliability": reliability,
        }

    @app.get("/api/runtime/capabilities")
    async def api_runtime_capabilities(request: Request) -> Dict[str, Any]:
        _opt_api_scope(request)
        return agent_service.runtime_capabilities()

    @app.get("/api/metrics")
    async def api_metrics(request: Request) -> Dict[str, int]:
        _opt_api_scope(request)
        return agent_service.metrics()

    @app.get("/api/reliability")
    async def api_reliability(request: Request, limit: int = 500) -> Dict[str, Any]:
        _opt_api_scope(request)
        return agent_service.reliability_snapshot(limit=max(10, min(limit, 5000)))

    @app.get("/api/onboarding/status")
    async def onboarding_status(request: Request) -> Dict[str, Any]:
        _opt_api_scope(request)
        return onboarding.load()

    @app.get("/api/onboarding/readiness")
    async def onboarding_readiness(request: Request) -> Dict[str, Any]:
        """Return structured readiness checks for first-run validation."""
        import shutil
        import subprocess as _subprocess
        _opt_api_scope(request)
        checks: Dict[str, Any] = {}

        # Workspace check
        ws_root_env = os.environ.get("EXECUTION_WORKSPACE_ROOT", "")
        if ws_root_env:
            ws_path = Path(ws_root_env)
            ws_exists = ws_path.is_dir()
            ws_writable = False
            if ws_exists:
                try:
                    test_file = ws_path / ".codex_write_test"
                    test_file.touch()
                    test_file.unlink()
                    ws_writable = True
                except OSError:
                    pass
            checks["workspace"] = {
                "pass": ws_exists and ws_writable,
                "path": ws_root_env,
                "exists": ws_exists,
                "writable": ws_writable,
            }
        else:
            checks["workspace"] = {"pass": False, "reason": "EXECUTION_WORKSPACE_ROOT not set"}

        # Codex CLI check
        codex_path = shutil.which("codex")
        codex_ok = codex_path is not None
        if codex_ok:
            try:
                result = _subprocess.run(
                    ["codex", "--version"],
                    capture_output=True, text=True, timeout=5
                )
                codex_version = result.stdout.strip() or result.stderr.strip() or "unknown"
                codex_ok = result.returncode == 0
            except Exception:
                codex_version = "unavailable"
                codex_ok = False
        else:
            codex_version = "not found"
        checks["codex_cli"] = {"pass": codex_ok, "path": codex_path, "version": codex_version}

        # Telegram token check
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        tg_present = bool(tg_token and len(tg_token) > 20)
        checks["telegram_token"] = {
            "pass": tg_present,
            "configured": tg_present,
            "hint": "Set TELEGRAM_BOT_TOKEN env var" if not tg_present else "",
        }

        overall = all(c.get("pass", False) for c in checks.values())
        return {"ready": overall, "checks": checks}

    @app.get("/api/runs")
    async def list_runs(request: Request, limit: int = 20) -> List[Dict[str, Any]]:
        _opt_api_scope(request)
        runs = agent_service.list_recent_runs(limit=max(1, min(limit, 100)))
        return [_run_to_dict(r) for r in runs]

    @app.get("/api/runs/{run_id}")
    async def get_run(request: Request, run_id: str) -> Dict[str, Any]:
        _opt_api_scope(request)
        run = agent_service.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        return _run_to_dict(run)

    @app.get("/api/error-catalog")
    async def api_error_catalog(request: Request) -> List[Dict[str, Any]]:
        _opt_api_scope(request)
        return _catalog_to_dict()

    @app.get("/api/runs/{run_id}/recovery-options")
    async def get_recovery_options(request: Request, run_id: str) -> Dict[str, Any]:
        _opt_api_scope(request)
        run = agent_service.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        if not run.error:
            return {"run_id": run_id, "error_code": "", "actions": []}
        code = detect_error_code(run.error)
        entry = get_catalog_entry(code)
        actions = [
            {
                "action_id": a.action_id,
                "label": a.label,
                "description": a.description,
                "kind": "api" if a.action_id.startswith("retry_") else "link",
            }
            for a in entry.actions
        ]
        return {"run_id": run_id, "error_code": code, "actions": actions}

    @app.get("/api/runs/{run_id}/events")
    async def get_run_events(request: Request, run_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        _opt_api_scope(request)
        run = agent_service.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        events = agent_service.list_run_events(run_id=run_id, limit=max(1, min(limit, 500)))
        return [_event_to_dict(e) for e in events]

    @app.get("/api/runs/{run_id}/artifact.txt")
    async def download_run_artifact(request: Request, run_id: str) -> PlainTextResponse:
        _opt_api_scope(request)
        run = agent_service.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        events = agent_service.list_run_events(run_id=run_id, limit=500)

        body = []
        body.append(f"Run ID: {run.run_id}")
        body.append(f"Status: {run.status}")
        body.append(f"Created: {run.created_at.isoformat()}")
        body.append(f"Started: {run.started_at.isoformat() if run.started_at else '-'}")
        body.append(f"Completed: {run.completed_at.isoformat() if run.completed_at else '-'}")
        body.append("")
        body.append("=== Prompt ===")
        body.append(run.prompt or "")
        body.append("")
        body.append("=== Output ===")
        body.append(run.output or "")
        body.append("")
        body.append("=== Error ===")
        body.append(run.error or "")
        body.append("")
        body.append("=== Timeline ===")
        for event in events:
            body.append(f"{event.created_at.isoformat()} [{event.event_type}] {event.payload}")

        content = "\n".join(body).strip() + "\n"
        headers = {"Content-Disposition": f'attachment; filename="run-{run_id}.txt"'}
        return PlainTextResponse(content=content, headers=headers)

    @app.get("/api/agents")
    async def api_list_agents(request: Request) -> List[Dict[str, Any]]:
        _opt_api_scope(request)
        agents = agent_service.list_agents()
        return [
            {
                "agent_id": a.agent_id,
                "name": a.name,
                "provider": a.provider,
                "policy_profile": a.policy_profile,
                "max_concurrency": a.max_concurrency,
                "enabled": a.enabled,
                "created_at": a.created_at.isoformat(),
                "updated_at": a.updated_at.isoformat(),
            }
            for a in agents
        ]

    @app.get("/api/sessions")
    async def api_list_sessions(request: Request, limit: int = 50) -> List[Dict[str, Any]]:
        _opt_api_scope(request)
        sessions = agent_service.list_recent_sessions(limit=max(1, min(limit, 200)))
        return [
            {
                "session_id": s.session_id,
                "chat_id": s.chat_id,
                "user_id": s.user_id,
                "status": s.status,
                "current_agent_id": s.current_agent_id,
                "summary": s.summary,
                "last_run_id": s.last_run_id,
                "created_at": s.created_at.isoformat(),
                "updated_at": s.updated_at.isoformat(),
            }
            for s in sessions
        ]

    @app.get("/api/costs/session/{session_id}")
    async def api_session_costs(request: Request, session_id: str) -> Dict[str, Any]:
        _opt_api_scope(request)
        summary = agent_service.session_cost_summary(session_id=session_id)
        events = agent_service.list_session_usage_events(session_id=session_id, limit=50)
        return {"summary": summary, "events": events}

    @app.get("/api/costs/user/{user_id}/daily")
    async def api_user_daily_costs(request: Request, user_id: int, date: str = "") -> Dict[str, Any]:
        _opt_api_scope(request)
        return agent_service.user_daily_cost_summary(user_id=user_id, date=date)

    @app.get("/api/costs/daily")
    async def api_costs_daily(request: Request, date: str = "", limit: int = 100) -> Dict[str, Any]:
        _opt_api_scope(request)
        items = agent_service.list_daily_cost_summaries(date=date, limit=max(1, min(limit, 500)))
        return {"date": date, "items": items}

    @app.get("/api/execution-profile")
    async def api_execution_profile(request: Request) -> Dict[str, Any]:
        _opt_api_scope(request)
        state = agent_service.execution_profile_state()
        state["warning"] = agent_service.execution_profile_warning()
        return state

    @app.get("/api/execution-profile/audit")
    async def api_execution_profile_audit(request: Request, limit: int = 100) -> Dict[str, Any]:
        _opt_api_scope(request)
        items = agent_service.list_execution_profile_audit(limit=max(1, min(limit, 500)))
        return {"items": items}

    @app.post("/api/execution-profile/set")
    async def api_execution_profile_set(request: Request, req: ExecutionProfileRequest) -> Dict[str, Any]:
        _opt_api_scope(request, write_scope="admin:*")
        state = agent_service.set_execution_profile(
            profile=req.profile,
            user_id=int(req.user_id or 0),
            origin="web",
        )
        state["warning"] = agent_service.execution_profile_warning()
        return state

    @app.post("/api/execution-profile/start-unsafe-unlock")
    async def api_execution_profile_start_unlock(request: Request, user_id: int = 0) -> Dict[str, Any]:
        _opt_api_scope(request, write_scope="admin:*")
        return agent_service.start_unsafe_unlock(
            user_id=int(user_id or 0),
            origin="web",
        )

    @app.post("/api/execution-profile/confirm-unsafe-unlock")
    async def api_execution_profile_confirm_unlock(
        request: Request, req: UnsafeUnlockConfirmRequest
    ) -> Dict[str, Any]:
        _opt_api_scope(request, write_scope="admin:*")
        state = agent_service.confirm_unsafe_unlock(
            user_id=int(req.user_id or 0),
            origin="web",
            code=req.code,
            phrase=req.phrase,
        )
        state["warning"] = agent_service.execution_profile_warning()
        return state

    @app.post("/api/whatsapp/link-code")
    async def api_whatsapp_link_code(request: Request, req: WhatsAppLinkCodeRequest) -> Dict[str, Any]:
        _opt_api_scope(request, write_scope="admin:*")
        if whatsapp_bridge is None:
            raise HTTPException(status_code=503, detail="WhatsApp bridge not enabled.")
        return whatsapp_bridge.create_link_code(
            chat_id=int(req.chat_id),
            user_id=int(req.user_id),
            ttl_sec=max(60, int(req.ttl_sec or 900)),
        )

    @app.post("/whatsapp/webhook")
    async def whatsapp_webhook(request: Request):
        if not _whatsapp_webhook_authorized(request):
            raise HTTPException(status_code=401, detail="Unauthorized webhook token.")
        if whatsapp_bridge is None:
            return PlainTextResponse(_twiml_message("WhatsApp bridge is not enabled."), media_type="application/xml")
        form = await request.form()
        sender = str(form.get("From") or "").strip()
        text = str(form.get("Body") or "").strip()
        try:
            reply = await whatsapp_bridge.handle_inbound(external_user_id=sender, text=text)
        except Exception as exc:
            reply = f"Error: {str(exc)[:180]}"
        return PlainTextResponse(_twiml_message(reply), media_type="application/xml")

    @app.get("/api/approvals")
    async def api_list_approvals(request: Request, limit: int = 200) -> List[Dict[str, Any]]:
        _opt_api_scope(request)
        items = agent_service.list_all_pending_tool_approvals(limit=max(1, min(limit, 500)))
        return items

    @app.get("/api/retrieval")
    async def api_retrieval(request: Request, query: str, limit: int = 4) -> Dict[str, Any]:
        _opt_api_scope(request)
        context_lines = agent_service.build_retrieval_context(user_prompt=query, limit=max(1, min(limit, 10)))
        return {"query": query, "context": context_lines}

    @app.get("/api/retrieval/stats")
    async def api_retrieval_stats(request: Request) -> Dict[str, Any]:
        _opt_api_scope(request)
        return agent_service.retrieval_stats()

    @app.post("/api/retrieval/refresh")
    async def api_retrieval_refresh(request: Request) -> Dict[str, Any]:
        _opt_api_scope(request, write_scope="api:write")
        return agent_service.refresh_retrieval_index(force=True)

    @app.post("/api/approvals/approve")
    async def api_approve_tool(request: Request, req: ApproveToolRequest) -> Dict[str, Any]:
        _opt_api_scope(request, write_scope="api:write")
        output = await agent_service.approve_tool_action(
            approval_id=req.approval_id,
            chat_id=req.chat_id,
            user_id=req.user_id,
        )
        return {"approval_id": req.approval_id, "output": output}

    @app.post("/api/approvals/deny")
    async def api_deny_tool(request: Request, req: ApproveToolRequest) -> Dict[str, Any]:
        _opt_api_scope(request, write_scope="api:write")
        output = agent_service.deny_tool_action(
            approval_id=req.approval_id,
            chat_id=req.chat_id,
            user_id=req.user_id,
        )
        return {"approval_id": req.approval_id, "output": output}

    @app.post("/api/jobs/{job_id}/cancel")
    async def api_cancel_job(request: Request, job_id: str) -> Dict[str, Any]:
        _opt_api_scope(request, write_scope="api:write")
        cancelled = agent_service.cancel_job(job_id)
        return {"job_id": job_id, "cancelled": cancelled, "status": agent_service.job_status(job_id)}

    @app.post("/api/handoffs")
    async def api_handoff(request: Request, req: HandoffRequest) -> Dict[str, Any]:
        _opt_api_scope(request, write_scope="api:write")
        return await agent_service.handoff_prompt(
            from_agent_id=req.from_agent_id,
            to_agent_id=req.to_agent_id,
            prompt=req.prompt,
            parent_run_id=req.parent_run_id,
        )

    @app.post("/api/runs/{run_id}/recover")
    async def api_recover_run(request: Request, run_id: str, req: RecoveryRequest) -> Dict[str, Any]:
        _opt_api_scope(request, write_scope="api:write")
        run = agent_service.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        action = (req.action_id or "").strip()
        allowed = _allowed_recovery_actions()
        if action not in allowed:
            raise HTTPException(status_code=400, detail="Unsupported recovery action")

        agent_service.append_run_event(
            run_id=run_id,
            event_type="recovery.attempted",
            payload=f"action={action}",
        )

        if action in {"open_settings", "open_agents", "download_artifact"}:
            destination = {
                "open_settings": "/settings",
                "open_agents": "/agents",
                "download_artifact": f"/api/runs/{run_id}/artifact.txt",
            }[action]
            agent_service.append_run_event(
                run_id=run_id,
                event_type="recovery.completed",
                payload=f"action={action}, destination={destination}",
            )
            return {"run_id": run_id, "status": "completed", "action_id": action, "redirect_to": destination}

        target_agent = "default" if action == "retry_default_agent" else agent_service.infer_run_agent_id(run_id)
        try:
            job_id = await agent_service.queue_prompt(prompt=run.prompt or "", agent_id=target_agent)
        except Exception as exc:
            agent_service.append_run_event(
                run_id=run_id,
                event_type="recovery.failed",
                payload=f"action={action}, reason={str(exc)[:200]}",
            )
            return {
                "run_id": run_id,
                "status": "failed",
                "action_id": action,
                "reason": "queue_failed",
            }

        agent_service.append_run_event(
            run_id=run_id,
            event_type="recovery.queued",
            payload=f"action={action}, job_id={job_id}, target_agent={target_agent}",
        )
        return {
            "run_id": run_id,
            "status": "queued",
            "action_id": action,
            "job_id": job_id,
            "target_agent": target_agent,
        }

    @app.get("/api/recovery/playbook")
    async def api_recovery_playbook(request: Request) -> Dict[str, Any]:
        _opt_api_scope(request)
        return {
            "actions": _allowed_recovery_actions(),
            "docs": "/docs/recovery_playbook.md",
            "notes": [
                "Use retry_same_agent for transient provider issues.",
                "Use retry_default_agent when agent-specific profile likely caused failure.",
                "Do not auto-approve high-risk tool actions during recovery.",
            ],
        }

    @app.get("/api/v1/meta")
    async def api_v1_meta(request: Request) -> Dict[str, Any]:
        _require_local_api_scope(request, "meta:read")
        return {
            "api_version": local_api_version,
            "service": "codex-telegram-bot",
            "compatibility": "minor backward compatible within v1",
            "endpoints": {
                "runs_list": {"path": "/api/v1/runs", "scope": "runs:read"},
                "runs_get": {"path": "/api/v1/runs/{run_id}", "scope": "runs:read"},
                "prompt_queue": {"path": "/api/v1/prompts", "scope": "prompts:write"},
                "job_get": {"path": "/api/v1/jobs/{job_id}", "scope": "jobs:read"},
                "job_cancel": {"path": "/api/v1/jobs/{job_id}/cancel", "scope": "jobs:write"},
                "plugins_list": {"path": "/api/v1/plugins", "scope": "plugins:read"},
                "skills_list": {"path": "/api/v1/skills", "scope": "plugins:read"},
            },
        }

    @app.get("/api/v1/runs")
    async def api_v1_runs(request: Request, limit: int = 20) -> Dict[str, Any]:
        _require_local_api_scope(request, "runs:read")
        runs = agent_service.list_recent_runs(limit=max(1, min(limit, 100)))
        return {"items": [_run_to_dict(r) for r in runs], "limit": limit}

    @app.get("/api/v1/runs/{run_id}")
    async def api_v1_run(request: Request, run_id: str) -> Dict[str, Any]:
        _require_local_api_scope(request, "runs:read")
        run = agent_service.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        return _run_to_dict(run)

    @app.post("/api/v1/prompts")
    async def api_v1_prompt(request: Request, req: LocalApiPromptRequest) -> Dict[str, Any]:
        _require_local_api_scope(request, "prompts:write")
        text = (req.prompt or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="Prompt is required")
        job_id = await agent_service.queue_prompt(prompt=text, agent_id=(req.agent_id or "default"))
        return {"job_id": job_id, "status": agent_service.job_status(job_id), "agent_id": req.agent_id}

    @app.get("/api/v1/jobs/{job_id}")
    async def api_v1_job_status(request: Request, job_id: str) -> Dict[str, Any]:
        _require_local_api_scope(request, "jobs:read")
        return {"job_id": job_id, "status": agent_service.job_status(job_id)}

    @app.post("/api/v1/jobs/{job_id}/cancel")
    async def api_v1_job_cancel(request: Request, job_id: str) -> Dict[str, Any]:
        _require_local_api_scope(request, "jobs:write")
        cancelled = agent_service.cancel_job(job_id)
        return {"job_id": job_id, "cancelled": cancelled, "status": agent_service.job_status(job_id)}

    @app.get("/api/v1/plugins")
    async def api_v1_plugins(request: Request) -> Dict[str, Any]:
        _require_local_api_scope(request, "plugins:read")
        items = [_plugin_to_dict(p) for p in plugin_manager.list_plugins()]
        return {"items": items}

    @app.get("/api/v1/skills")
    async def api_v1_skills(request: Request) -> Dict[str, Any]:
        _require_local_api_scope(request, "plugins:read")
        return {"items": agent_service.list_skills()}

    @app.get("/api/plugins")
    async def api_plugins(request: Request) -> List[Dict[str, Any]]:
        _opt_api_scope(request)
        return [_plugin_to_dict(p) for p in plugin_manager.list_plugins()]

    @app.get("/api/plugins/audit")
    async def api_plugins_audit(request: Request, limit: int = 200) -> List[Dict[str, Any]]:
        _opt_api_scope(request)
        return [_plugin_audit_to_dict(e) for e in plugin_manager.list_audit_events(limit=max(1, min(limit, 1000)))]

    @app.post("/api/plugins/install")
    async def api_plugins_install(request: Request, req: PluginInstallRequest) -> Dict[str, Any]:
        _opt_api_scope(request, write_scope="admin:*")
        try:
            plugin = plugin_manager.install_plugin(manifest_path=Path(req.manifest_path), enable=req.enable)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _plugin_to_dict(plugin)

    @app.post("/api/plugins/{plugin_id}/enable")
    async def api_plugins_enable(request: Request, plugin_id: str) -> Dict[str, Any]:
        _opt_api_scope(request, write_scope="admin:*")
        try:
            plugin = plugin_manager.enable_plugin(plugin_id=plugin_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _plugin_to_dict(plugin)

    @app.post("/api/plugins/{plugin_id}/disable")
    async def api_plugins_disable(request: Request, plugin_id: str) -> Dict[str, Any]:
        _opt_api_scope(request, write_scope="admin:*")
        try:
            plugin = plugin_manager.disable_plugin(plugin_id=plugin_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _plugin_to_dict(plugin)

    @app.post("/api/plugins/{plugin_id}/uninstall")
    async def api_plugins_uninstall(request: Request, plugin_id: str) -> Dict[str, Any]:
        _opt_api_scope(request, write_scope="admin:*")
        deleted = plugin_manager.uninstall_plugin(plugin_id=plugin_id)
        return {"plugin_id": plugin_id, "deleted": deleted}

    @app.post("/api/plugins/{plugin_id}/update")
    async def api_plugins_update(request: Request, plugin_id: str, req: PluginUpdateRequest) -> Dict[str, Any]:
        _opt_api_scope(request, write_scope="admin:*")
        try:
            plugin = plugin_manager.update_plugin(plugin_id=plugin_id, manifest_path=Path(req.manifest_path))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _plugin_to_dict(plugin)

    @app.get("/api/skills")
    async def api_skills(request: Request) -> List[Dict[str, Any]]:
        _opt_api_scope(request)
        return agent_service.list_skills()

    @app.post("/api/skills/install")
    async def api_skills_install(request: Request, req: SkillInstallRequest) -> Dict[str, Any]:
        _opt_api_scope(request, write_scope="admin:*")
        try:
            return agent_service.install_skill_from_url(req.source_url)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/skills/{skill_id}/enable")
    async def api_skills_enable(request: Request, skill_id: str) -> Dict[str, Any]:
        _opt_api_scope(request, write_scope="admin:*")
        try:
            return agent_service.set_skill_enabled(skill_id=skill_id, enabled=True)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/skills/{skill_id}/disable")
    async def api_skills_disable(request: Request, skill_id: str) -> Dict[str, Any]:
        _opt_api_scope(request, write_scope="admin:*")
        try:
            return agent_service.set_skill_enabled(skill_id=skill_id, enabled=False)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        if (redir := _ui_auth_redirect(request)) is not None:
            return redir
        runs = [_run_to_dict(r) for r in agent_service.list_recent_runs(limit=30)]
        completed = len([r for r in runs if r["status"] == "completed"])
        failed = len([r for r in runs if r["status"] == "failed"])
        running = len([r for r in runs if r["status"] == "running"])
        onboarding_state = onboarding.load()
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "nav": "dashboard",
                "runs": runs,
                "onboarding_state": onboarding_state,
                "metrics": {
                    "total": len(runs),
                    "completed": completed,
                    "failed": failed,
                    "running": running,
                },
            },
        )

    @app.get("/onboarding", response_class=HTMLResponse)
    async def onboarding_page(request: Request):
        if (redir := _ui_auth_redirect(request)) is not None:
            return redir
        onboarding.record(step="wizard.view", outcome="visit")
        env = load_env_file(get_env_path(config_dir)) if config_dir else {}
        runtime_env = load_env_file(config_dir / "runtime.env") if config_dir else {}
        workspace_root = env.get("EXECUTION_WORKSPACE_ROOT", str(Path.cwd()))
        profile = "trusted"
        autonomous_tool_loop = _env_flag_enabled(
            runtime_env.get(AUTONOMOUS_TOOL_LOOP_ENV)
            or env.get(AUTONOMOUS_TOOL_LOOP_ENV)
            or os.environ.get(AUTONOMOUS_TOOL_LOOP_ENV, "")
        )
        default_agent = agent_service.get_agent("default")
        if default_agent:
            profile = default_agent.policy_profile
        return templates.TemplateResponse(
            "onboarding.html",
            {
                "request": request,
                "nav": "onboarding",
                "onboarding_state": onboarding.load(),
                "workspace_root": workspace_root,
                "policy_profile": profile,
                "autonomous_tool_loop": autonomous_tool_loop,
                "provider_key_hint": provider_key_hint,
                "error": "",
                "result": "",
            },
        )

    @app.post("/onboarding", response_class=HTMLResponse)
    async def onboarding_submit(
        request: Request,
        provider_key: str = Form(""),
        workspace_root: str = Form(""),
        policy_profile: str = Form("trusted"),
        autonomous_tool_loop: str = Form(""),
    ):
        if (redir := _ui_auth_redirect(request)) is not None:
            return redir
        onboarding.record(step="wizard.submit", outcome="attempt")
        key = (provider_key or "").strip()
        workspace = Path((workspace_root or "").strip()).expanduser()
        profile = (policy_profile or "").strip().lower()
        autonomous_value = _normalize_checkbox_flag(autonomous_tool_loop)
        autonomous_enabled = autonomous_value == "1"

        if profile not in {"strict", "balanced", "trusted"}:
            onboarding.record(step="wizard.validate", outcome="invalid_profile")
            return templates.TemplateResponse(
                "onboarding.html",
                {
                    "request": request,
                    "nav": "onboarding",
                    "onboarding_state": onboarding.load(),
                    "workspace_root": str(workspace),
                    "policy_profile": profile,
                    "autonomous_tool_loop": autonomous_enabled,
                    "provider_key_hint": provider_key_hint,
                    "error": "Invalid safety profile.",
                    "result": "",
                },
                status_code=400,
            )
        if not workspace.exists() or not workspace.is_dir():
            onboarding.record(step="wizard.validate", outcome="invalid_workspace")
            return templates.TemplateResponse(
                "onboarding.html",
                {
                    "request": request,
                    "nav": "onboarding",
                    "onboarding_state": onboarding.load(),
                    "workspace_root": str(workspace),
                    "policy_profile": profile,
                    "autonomous_tool_loop": autonomous_enabled,
                    "provider_key_hint": provider_key_hint,
                    "error": "Workspace path must exist and be a directory.",
                    "result": "",
                },
                status_code=400,
            )
        if key and len(key) < 12:
            onboarding.record(step="wizard.validate", outcome="invalid_provider_key")
            return templates.TemplateResponse(
                "onboarding.html",
                {
                    "request": request,
                    "nav": "onboarding",
                    "onboarding_state": onboarding.load(),
                    "workspace_root": str(workspace),
                    "policy_profile": profile,
                    "autonomous_tool_loop": autonomous_enabled,
                    "provider_key_hint": provider_key_hint,
                    "error": "Provider key format looks invalid.",
                    "result": "",
                },
                status_code=400,
            )

        if config_dir:
            env_path = get_env_path(config_dir)
            env = load_env_file(env_path)
            env["EXECUTION_WORKSPACE_ROOT"] = str(workspace.resolve())
            env[AUTONOMOUS_TOOL_LOOP_ENV] = autonomous_value
            if key:
                env[onboarding_key_env] = key
            write_env_file(env_path, env)
            runtime_env_path = config_dir / "runtime.env"
            runtime_env = load_env_file(runtime_env_path) if runtime_env_path.exists() else {}
            runtime_env[AUTONOMOUS_TOOL_LOOP_ENV] = autonomous_value
            write_env_file(runtime_env_path, runtime_env)
            os.environ[AUTONOMOUS_TOOL_LOOP_ENV] = autonomous_value

        default_agent = agent_service.get_agent("default")
        if default_agent:
            agent_service.upsert_agent(
                agent_id=default_agent.agent_id,
                name=default_agent.name,
                provider=default_agent.provider,
                policy_profile=profile,
                max_concurrency=default_agent.max_concurrency,
                enabled=default_agent.enabled,
            )

        onboarding.record(step="wizard.validate", outcome="passed")
        test_prompt = "Reply with exactly: onboarding-ok"
        try:
            job_id = await agent_service.queue_prompt(prompt=test_prompt, agent_id="default")
            output = await agent_service.wait_job(job_id)
        except Exception as exc:
            onboarding.record(step="wizard.test_run", outcome="failed_exception")
            return templates.TemplateResponse(
                "onboarding.html",
                {
                    "request": request,
                    "nav": "onboarding",
                    "onboarding_state": onboarding.load(),
                    "workspace_root": str(workspace.resolve()),
                    "policy_profile": profile,
                    "autonomous_tool_loop": autonomous_enabled,
                    "provider_key_hint": provider_key_hint,
                    "error": f"Test run failed: {str(exc)[:200]}",
                    "result": "",
                },
                status_code=500,
            )

        if output.startswith("Error:"):
            onboarding.record(step="wizard.test_run", outcome="failed_output")
            return templates.TemplateResponse(
                "onboarding.html",
                {
                    "request": request,
                    "nav": "onboarding",
                    "onboarding_state": onboarding.load(),
                    "workspace_root": str(workspace.resolve()),
                    "policy_profile": profile,
                    "autonomous_tool_loop": autonomous_enabled,
                    "provider_key_hint": provider_key_hint,
                    "error": "Test run did not succeed. Review settings and try again.",
                    "result": output[:220],
                },
                status_code=400,
            )

        onboarding.record(step="wizard.test_run", outcome="passed")
        onboarding.complete()
        return templates.TemplateResponse(
            "onboarding.html",
            {
                "request": request,
                "nav": "onboarding",
                "onboarding_state": onboarding.load(),
                "workspace_root": str(workspace.resolve()),
                "policy_profile": profile,
                "autonomous_tool_loop": autonomous_enabled,
                "provider_key_hint": provider_key_hint,
                "error": "",
                "result": output[:220],
            },
            status_code=200,
        )

    @app.get("/runs", response_class=HTMLResponse)
    async def runs_page(request: Request):
        if (redir := _ui_auth_redirect(request)) is not None:
            return redir
        runs = [_run_to_dict(r) for r in agent_service.list_recent_runs(limit=100)]
        return templates.TemplateResponse(
            "runs.html",
            {"request": request, "nav": "runs", "runs": runs},
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_detail(request: Request, run_id: str):
        if (redir := _ui_auth_redirect(request)) is not None:
            return redir
        run = agent_service.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        events = [_event_to_dict(e) for e in agent_service.list_run_events(run_id, limit=200)]
        run_dict = _run_to_dict(run)
        recovery_options = []
        if run_dict.get("error_code"):
            entry = get_catalog_entry(run_dict["error_code"])
            recovery_options = [
                {
                    "action_id": action.action_id,
                    "label": action.label,
                    "description": action.description,
                    "kind": "api" if action.action_id.startswith("retry_") else "link",
                    "href": {
                        "open_settings": "/settings",
                        "open_agents": "/agents",
                        "download_artifact": f"/api/runs/{run_id}/artifact.txt",
                    }.get(action.action_id, ""),
                }
                for action in entry.actions
            ]
        return templates.TemplateResponse(
            "run_detail.html",
            {
                "request": request,
                "nav": "runs",
                "run": run_dict,
                "events": events,
                "recovery_options": recovery_options,
            },
        )

    @app.get("/agents", response_class=HTMLResponse)
    async def agents_page(request: Request, error: str = ""):
        if (redir := _ui_auth_redirect(request)) is not None:
            return redir
        agents = agent_service.list_agents()
        provider_options = _provider_options()
        return templates.TemplateResponse(
            "agents.html",
            {
                "request": request,
                "nav": "agents",
                "agents": agents,
                "provider_options": provider_options,
                "default_provider": provider_options[0] if provider_options else "codex_cli",
                "error": error,
            },
        )

    @app.get("/plugins", response_class=HTMLResponse)
    async def plugins_page(request: Request, error: str = ""):
        if (redir := _ui_auth_redirect(request)) is not None:
            return redir
        plugins = plugin_manager.list_plugins()
        audit = plugin_manager.list_audit_events(limit=50)
        return templates.TemplateResponse(
            "plugins.html",
            {
                "request": request,
                "nav": "plugins",
                "plugins": plugins,
                "audit_events": audit,
                "error": error,
            },
        )

    @app.post("/plugins/install")
    async def plugins_install_form(request: Request, manifest_path: str = Form(...), enable: str = Form("false")):
        if (redir := _ui_auth_redirect(request)) is not None:
            return redir
        try:
            plugin_manager.install_plugin(Path(manifest_path), enable=(enable == "true"))
        except Exception as exc:
            return RedirectResponse(url=f"/plugins?error={str(exc)}", status_code=303)
        return RedirectResponse(url="/plugins", status_code=303)

    @app.post("/plugins/{plugin_id}/enable")
    async def plugins_enable_form(request: Request, plugin_id: str):
        if (redir := _ui_auth_redirect(request)) is not None:
            return redir
        try:
            plugin_manager.enable_plugin(plugin_id)
        except Exception as exc:
            return RedirectResponse(url=f"/plugins?error={str(exc)}", status_code=303)
        return RedirectResponse(url="/plugins", status_code=303)

    @app.post("/plugins/{plugin_id}/disable")
    async def plugins_disable_form(request: Request, plugin_id: str):
        if (redir := _ui_auth_redirect(request)) is not None:
            return redir
        try:
            plugin_manager.disable_plugin(plugin_id)
        except Exception as exc:
            return RedirectResponse(url=f"/plugins?error={str(exc)}", status_code=303)
        return RedirectResponse(url="/plugins", status_code=303)

    @app.post("/plugins/{plugin_id}/uninstall")
    async def plugins_uninstall_form(request: Request, plugin_id: str):
        if (redir := _ui_auth_redirect(request)) is not None:
            return redir
        plugin_manager.uninstall_plugin(plugin_id)
        return RedirectResponse(url="/plugins", status_code=303)

    @app.get("/skills", response_class=HTMLResponse)
    async def skills_page(request: Request, error: str = ""):
        skills = agent_service.list_skills()
        return templates.TemplateResponse(
            "skills.html",
            {"request": request, "nav": "skills", "skills": skills, "error": error},
        )

    @app.post("/skills/install")
    async def skills_install_form(source_url: str = Form(...)):
        try:
            agent_service.install_skill_from_url(source_url)
        except Exception as exc:
            return RedirectResponse(url=f"/skills?error={str(exc)}", status_code=303)
        return RedirectResponse(url="/skills", status_code=303)

    @app.post("/skills/{skill_id}/enable")
    async def skills_enable_form(skill_id: str):
        try:
            agent_service.set_skill_enabled(skill_id=skill_id, enabled=True)
        except Exception as exc:
            return RedirectResponse(url=f"/skills?error={str(exc)}", status_code=303)
        return RedirectResponse(url="/skills", status_code=303)

    @app.post("/skills/{skill_id}/disable")
    async def skills_disable_form(skill_id: str):
        try:
            agent_service.set_skill_enabled(skill_id=skill_id, enabled=False)
        except Exception as exc:
            return RedirectResponse(url=f"/skills?error={str(exc)}", status_code=303)
        return RedirectResponse(url="/skills", status_code=303)

    @app.get("/sessions", response_class=HTMLResponse)
    async def sessions_page(request: Request):
        if (redir := _ui_auth_redirect(request)) is not None:
            return redir
        sessions = agent_service.list_recent_sessions(limit=100)
        return templates.TemplateResponse(
            "sessions.html",
            {"request": request, "nav": "sessions", "sessions": sessions},
        )

    @app.get("/chat", response_class=HTMLResponse)
    async def chat_page(request: Request, session_id: str = ""):
        if (redir := _ui_auth_redirect(request)) is not None:
            return redir
        sessions = agent_service.list_recent_sessions(limit=100)
        selected = (session_id or "").strip()
        if not selected and sessions:
            selected = sessions[0].session_id
        selected_session = agent_service.get_session(selected) if selected else None
        return templates.TemplateResponse(
            "chat.html",
            {
                "request": request,
                "nav": "chat",
                "sessions": sessions,
                "selected_session_id": selected,
                "selected_session": selected_session,
                "selected_cost_summary": (agent_service.session_cost_summary(selected) if selected else {}),
            },
        )

    @app.websocket("/ws/chat")
    async def ws_chat(websocket: WebSocket) -> None:
        if not _ws_ui_authorized(websocket):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        subscribed_sessions: Set[str] = set()
        current_session_id = ""
        try:
            while True:
                inbound = await websocket.receive_json()
                if not isinstance(inbound, dict):
                    await websocket.send_json({"type": "error", "detail": "Invalid payload."})
                    continue
                msg_type = str(inbound.get("type") or "").strip().lower()
                if msg_type == "ping":
                    await websocket.send_json({"type": "pong"})
                    continue
                if msg_type == "subscribe":
                    requested_session = str(inbound.get("session_id") or "").strip()
                    session = agent_service.get_session(requested_session)
                    if session is None:
                        await websocket.send_json({"type": "error", "detail": "Session not found."})
                        continue
                    await _chat_subscribe(session.session_id, websocket)
                    subscribed_sessions.add(session.session_id)
                    current_session_id = session.session_id
                    await websocket.send_json(
                        {
                            "type": "session",
                            "session_id": session.session_id,
                            "chat_id": int(session.chat_id),
                            "user_id": int(session.user_id),
                            "cost_summary": agent_service.session_cost_summary(session.session_id),
                        }
                    )
                    continue
                if msg_type in {"approve", "deny"}:
                    approval_id = str(inbound.get("approval_id") or "").strip()
                    if not approval_id:
                        await websocket.send_json({"type": "error", "detail": "approval_id is required."})
                        continue
                    resolved_session_id = str(inbound.get("session_id") or current_session_id or "").strip()
                    session = agent_service.get_session(resolved_session_id) if resolved_session_id else None
                    chat_id = int(inbound.get("chat_id") or (session.chat_id if session else 0) or 0)
                    user_id = int(inbound.get("user_id") or (session.user_id if session else 0) or 0)
                    if chat_id == 0 or user_id == 0:
                        await websocket.send_json({"type": "error", "detail": "chat_id and user_id are required."})
                        continue
                    if msg_type == "approve":
                        output = await agent_service.approve_tool_action(
                            approval_id=approval_id,
                            chat_id=chat_id,
                            user_id=user_id,
                        )
                    else:
                        output = agent_service.deny_tool_action(
                            approval_id=approval_id,
                            chat_id=chat_id,
                            user_id=user_id,
                        )
                    status = "error" if str(output).strip().lower().startswith("error:") else "result"
                    await websocket.send_json(
                        {
                            "type": "tool_event",
                            "name": "approval",
                            "status": status,
                            "detail": {
                                "approval_id": approval_id,
                                "action": msg_type,
                                "output": output,
                                "chat_id": chat_id,
                                "user_id": user_id,
                            },
                        }
                    )
                    continue
                if msg_type != "user_message":
                    await websocket.send_json({"type": "error", "detail": "Unsupported message type."})
                    continue

                text = str(inbound.get("text") or "").strip()
                if not text:
                    await websocket.send_json({"type": "error", "detail": "text is required."})
                    continue
                session, err = _resolve_ws_session(inbound, current_session_id=current_session_id)
                if err:
                    await websocket.send_json({"type": "error", "detail": err})
                    continue
                if session is None:
                    await websocket.send_json({"type": "error", "detail": "Failed to resolve session."})
                    continue
                current_session_id = session.session_id
                await _chat_subscribe(session.session_id, websocket)
                subscribed_sessions.add(session.session_id)
                await websocket.send_json(
                    {
                        "type": "session",
                        "session_id": session.session_id,
                        "chat_id": int(session.chat_id),
                        "user_id": int(session.user_id),
                        "cost_summary": agent_service.session_cost_summary(session.session_id),
                    }
                )

                async def _progress(update_payload: Dict[str, Any]) -> None:
                    if not isinstance(update_payload, dict):
                        return
                    event = _progress_to_tool_event(
                        update_payload,
                        chat_id=int(session.chat_id),
                        user_id=int(session.user_id),
                    )
                    if event is not None:
                        await websocket.send_json(
                            {
                                **event,
                                "session_id": session.session_id,
                            }
                        )

                try:
                    turn = await agent_service.run_turn(
                        prompt=text,
                        chat_id=int(session.chat_id),
                        user_id=int(session.user_id),
                        session_id=session.session_id,
                        agent_id=str(inbound.get("agent_id") or "default"),
                        progress_callback=_progress,
                    )
                except Exception as exc:
                    await websocket.send_json(
                        {
                            "type": "tool_event",
                            "name": "runtime",
                            "status": "error",
                            "session_id": session.session_id,
                            "detail": {"message": str(exc)[:300]},
                        }
                    )
                    await websocket.send_json({"type": "done", "session_id": session.session_id})
                    continue

                chunks = _chunk_text(turn.text or "")
                if not chunks:
                    chunks = ["(no output)"]
                for chunk in chunks:
                    await websocket.send_json(
                        {
                            "type": "assistant_chunk",
                            "session_id": session.session_id,
                            "text": chunk,
                        }
                    )
                await websocket.send_json(
                    {
                        "type": "done",
                        "session_id": session.session_id,
                        "kind": turn.kind,
                        "cost_summary": agent_service.session_cost_summary(session.session_id),
                    }
                )
        except WebSocketDisconnect:
            pass
        finally:
            for session_id in list(subscribed_sessions):
                await _chat_unsubscribe(session_id, websocket)

    @app.get("/costs", response_class=HTMLResponse)
    async def costs_page(request: Request, date: str = ""):
        if (redir := _ui_auth_redirect(request)) is not None:
            return redir
        day = (date or "").strip()
        items = agent_service.list_daily_cost_summaries(date=day, limit=200)
        return templates.TemplateResponse(
            "costs.html",
            {
                "request": request,
                "nav": "costs",
                "items": items,
                "date": day,
            },
        )

    @app.get("/approvals", response_class=HTMLResponse)
    async def approvals_page(request: Request):
        if (redir := _ui_auth_redirect(request)) is not None:
            return redir
        approvals = agent_service.list_all_pending_tool_approvals(limit=200)
        return templates.TemplateResponse(
            "approvals.html",
            {"request": request, "nav": "approvals", "approvals": approvals},
        )

    @app.post("/agents", response_class=HTMLResponse)
    async def create_or_update_agent(
        request: Request,
        agent_id: str = Form(...),
        name: str = Form(...),
        provider: str = Form("codex_cli"),
        policy_profile: str = Form("trusted"),
        max_concurrency: int = Form(1),
        enabled: str = Form("true"),
    ):
        if (redir := _ui_auth_redirect(request)) is not None:
            return redir
        try:
            agent_service.upsert_agent(
                agent_id=agent_id,
                name=name,
                provider=provider,
                policy_profile=policy_profile,
                max_concurrency=max_concurrency,
                enabled=enabled == "true",
            )
        except ValueError as exc:
            agents = agent_service.list_agents()
            provider_options = _provider_options()
            return templates.TemplateResponse(
                "agents.html",
                {
                    "request": request,
                    "nav": "agents",
                    "agents": agents,
                    "provider_options": provider_options,
                    "default_provider": provider_options[0] if provider_options else "codex_cli",
                    "error": str(exc),
                },
                status_code=400,
            )
        return RedirectResponse(url="/agents", status_code=303)

    @app.post("/agents/{agent_id}/delete")
    async def delete_agent(request: Request, agent_id: str):
        if (redir := _ui_auth_redirect(request)) is not None:
            return redir
        agent_service.delete_agent(agent_id)
        return RedirectResponse(url="/agents", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, error: str = ""):
        if (redir := _ui_auth_redirect(request)) is not None:
            return redir
        version = await agent_service.provider_version()
        provider_health = await agent_service.provider_health()
        profile_state = agent_service.execution_profile_state()
        return templates.TemplateResponse(
            "settings.html",
            {
                "request": request,
                "nav": "settings",
                "provider_version": version,
                "provider_health": provider_health,
                "execution_profile": profile_state,
                "execution_profile_warning": agent_service.execution_profile_warning(),
                "execution_profile_audit": agent_service.list_execution_profile_audit(limit=25),
                "unsafe_unlock_phrase": "I UNDERSTAND THIS CAN EXECUTE ARBITRARY CODE ON MY MACHINE",
                "error": error,
                "whatsapp_enabled": bool(whatsapp_bridge is not None),
            },
        )

    @app.post("/settings/execution-profile")
    async def settings_set_execution_profile(request: Request, profile: str = Form(...), user_id: str = Form("0")):
        if (redir := _ui_auth_redirect(request)) is not None:
            return redir
        try:
            if str(profile or "").strip().lower() == "unsafe":
                agent_service.start_unsafe_unlock(
                    user_id=int(str(user_id or "0") or 0),
                    origin="web",
                )
            else:
                agent_service.set_execution_profile(
                    profile=profile,
                    user_id=int(str(user_id or "0") or 0),
                    origin="web",
                )
        except Exception as exc:
            return RedirectResponse(url=f"/settings?error={str(exc)}", status_code=303)
        return RedirectResponse(url="/settings", status_code=303)

    @app.post("/settings/execution-profile/confirm-unsafe")
    async def settings_confirm_unsafe_unlock(
        request: Request,
        code: str = Form(""),
        phrase: str = Form(""),
        user_id: str = Form("0"),
    ):
        if (redir := _ui_auth_redirect(request)) is not None:
            return redir
        try:
            agent_service.confirm_unsafe_unlock(
                user_id=int(str(user_id or "0") or 0),
                origin="web",
                code=code,
                phrase=phrase,
            )
        except Exception as exc:
            return RedirectResponse(url=f"/settings?error={str(exc)}", status_code=303)
        return RedirectResponse(url="/settings", status_code=303)

    # ------------------------------------------------------------------
    # EPIC 3: Provider management API
    # ------------------------------------------------------------------

    @app.get("/api/providers")
    async def api_providers(request: Request):
        """List all registered providers and their capabilities."""
        _opt_api_scope(request)
        if provider_registry is None:
            return {"providers": [], "active": None}
        return {
            "providers": provider_registry.list_providers(),
            "active": provider_registry.get_active_name(),
            "switch_history": provider_registry.switch_history[-10:],
        }

    @app.post("/api/providers/switch")
    async def api_provider_switch(request: Request):
        """Switch the active provider.  Body: {\"name\": \"...\"}"""
        _opt_api_scope(request, write_scope="admin:*")
        if provider_registry is None:
            raise HTTPException(status_code=503, detail="No provider registry configured")
        body = await request.json()
        name = (body.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="'name' is required")
        try:
            msg = provider_registry.switch(name)
            return {"ok": True, "message": msg, "active": provider_registry.get_active_name()}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.get("/api/providers/health")
    async def api_providers_health(request: Request):
        """Return aggregated health check for all providers."""
        _opt_api_scope(request)
        if provider_registry is None:
            return {"error": "no provider registry"}
        return await provider_registry.health()

    # ------------------------------------------------------------------
    # EPIC 4: Streaming log viewer (Server-Sent Events)
    # ------------------------------------------------------------------

    @app.get("/api/logs/stream")
    async def api_logs_stream(request: Request, limit: int = 50, max_polls: int = 0):
        """SSE endpoint streaming recent run events as JSON lines.
        Auth is checked before the stream is opened.


        Clients connect and receive a batch of recent events, then stay
        connected for new events (polled every 2 s from the store).
        Pass ``max_polls=N`` to limit polling iterations (useful for tests).
        """
        _opt_api_scope(request)
        from fastapi.responses import StreamingResponse
        import json as _json

        async def _event_generator():
            import json as _json2
            import asyncio as _asyncio
            seen_run_ids: set = set()
            # Initial batch: emit recent runs as events
            runs = agent_service.list_recent_runs(limit=limit)
            for run in runs:
                rid = run.run_id
                if rid not in seen_run_ids:
                    seen_run_ids.add(rid)
                    payload = _json2.dumps({
                        "run_id": rid,
                        "event_type": "run.status",
                        "payload": {"status": run.status, "prompt": (run.prompt or "")[:120]},
                        "created_at": run.created_at.isoformat() if run.created_at else "",
                    })
                    yield f"data: {payload}\n\n"
            # Tail: poll for new runs every 2 s
            polls = 0
            while True:
                if await request.is_disconnected():
                    break
                if max_polls > 0 and polls >= max_polls:
                    break
                polls += 1
                await _asyncio.sleep(0.05 if max_polls > 0 else 2)
                new_runs = agent_service.list_recent_runs(limit=10)
                for run in new_runs:
                    rid = run.run_id
                    if rid not in seen_run_ids:
                        seen_run_ids.add(rid)
                        payload = _json2.dumps({
                            "run_id": rid,
                            "event_type": "run.status",
                            "payload": {"status": run.status, "prompt": (run.prompt or "")[:120]},
                            "created_at": run.created_at.isoformat() if run.created_at else "",
                        })
                        yield f"data: {payload}\n\n"

        return StreamingResponse(
            _event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ------------------------------------------------------------------
    # EPIC 5: Metrics API (wired to MetricsCollector from EPIC 10)
    # ------------------------------------------------------------------

    @app.get("/api/mission-metrics")
    async def api_mission_metrics(request: Request):
        """Return a live mission dashboard snapshot (EPIC 10 MetricsCollector)."""
        _opt_api_scope(request)
        if metrics_collector is None:
            # Provide a basic stub so the endpoint always works
            return {
                "error": "metrics_collector not configured",
                "hint": "Pass metrics_collector to create_app()",
            }
        snapshot = metrics_collector.snapshot()
        return snapshot.to_dict()

    @app.get("/api/mission-metrics/text")
    async def api_mission_metrics_text(request: Request):
        """Return the metrics dashboard as plain text (for CLI / Telegram)."""
        _opt_api_scope(request)
        from fastapi.responses import PlainTextResponse as _PlainTextResponse
        if metrics_collector is None:
            return _PlainTextResponse("metrics_collector not configured")
        snapshot = metrics_collector.snapshot()
        return _PlainTextResponse(snapshot.format_text())

    # ------------------------------------------------------------------
    # Parity 1: Session detail API (list is already at /api/sessions)
    # ------------------------------------------------------------------

    @app.get("/api/sessions/{session_id}/detail")
    async def api_session_detail(request: Request, session_id: str):
        """Return metadata and recent messages for a specific session."""
        _opt_api_scope(request)
        session = agent_service.get_session(session_id)
        if session is None:
            from fastapi import HTTPException as _HTTPException
            raise _HTTPException(status_code=404, detail="Session not found")
        messages = agent_service.list_session_messages(session_id, limit=20)
        return {
            "session": {
                "session_id": session.session_id,
                "chat_id": session.chat_id,
                "user_id": session.user_id,
                "status": session.status,
                "current_agent_id": session.current_agent_id,
                "last_run_id": session.last_run_id,
                "summary": (session.summary or "")[:500],
                "created_at": session.created_at.isoformat() if session.created_at else "",
                "updated_at": session.updated_at.isoformat() if session.updated_at else "",
            },
            "recent_messages": [
                {
                    "role": m.role,
                    "content": (m.content or "")[:500],
                    "run_id": m.run_id,
                    "created_at": m.created_at.isoformat() if m.created_at else "",
                }
                for m in messages
            ],
            "cost_summary": agent_service.session_cost_summary(session_id=session_id),
        }

    return app
