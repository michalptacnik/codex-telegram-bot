from __future__ import annotations

import logging
import os
from typing import Awaitable, Callable, Optional

import httpx

logger = logging.getLogger(__name__)

WhatsAppSender = Callable[[str, str], Awaitable[None]]


def _normalize_whatsapp_number(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return raw if raw.lower().startswith("whatsapp:") else f"whatsapp:{raw}"


def build_twilio_whatsapp_sender_from_env() -> Optional[WhatsAppSender]:
    account_sid = (os.environ.get("TWILIO_ACCOUNT_SID") or "").strip()
    auth_token = (os.environ.get("TWILIO_AUTH_TOKEN") or "").strip()
    from_number = _normalize_whatsapp_number(os.environ.get("TWILIO_WHATSAPP_FROM") or "")
    if not account_sid or not auth_token or not from_number:
        return None
    timeout_s_raw = (os.environ.get("WHATSAPP_HTTP_TIMEOUT_S") or "15").strip()
    try:
        timeout_s = max(3.0, float(timeout_s_raw))
    except ValueError:
        timeout_s = 15.0

    async def _send(external_user_id: str, text: str) -> None:
        to_number = _normalize_whatsapp_number(external_user_id)
        body = str(text or "").strip()
        if not to_number or not body:
            return
        url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
        payload = {
            "To": to_number,
            "From": from_number,
            "Body": body[:3500],
        }
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(url, data=payload, auth=(account_sid, auth_token))
            if resp.status_code >= 400:
                logger.warning(
                    "whatsapp.twilio.send_failed status=%s body=%s",
                    resp.status_code,
                    (resp.text or "")[:240],
                )
                resp.raise_for_status()

    return _send
