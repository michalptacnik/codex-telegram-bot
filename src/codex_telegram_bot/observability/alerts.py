import json
import os
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict


class AlertDispatcher:
    def __init__(self, webhook_url: str = "", timeout_sec: int = 3):
        self._webhook_url = (webhook_url or os.environ.get("ALERT_WEBHOOK_URL") or "").strip()
        self._timeout_sec = max(1, int(timeout_sec or int(os.environ.get("ALERT_WEBHOOK_TIMEOUT_SEC", "3"))))

    @property
    def enabled(self) -> bool:
        return bool(self._webhook_url)

    def send(self, category: str, severity: str, message: str, **fields: Any) -> bool:
        if not self._webhook_url:
            return False
        payload: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "category": category,
            "severity": severity,
            "message": message,
        }
        payload.update(fields)
        data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        req = urllib.request.Request(
            url=self._webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_sec):
                return True
        except Exception:
            return False
