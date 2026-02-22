import json
import os
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict

SEVERITY_ORDER = {
    "low": 10,
    "medium": 20,
    "high": 30,
    "critical": 40,
}


class AlertDispatcher:
    def __init__(self, webhook_url: str = "", timeout_sec: int = 3):
        self._webhook_url = (webhook_url or os.environ.get("ALERT_WEBHOOK_URL") or "").strip()
        self._timeout_sec = max(1, int(timeout_sec or int(os.environ.get("ALERT_WEBHOOK_TIMEOUT_SEC", "3"))))
        self._min_severity = self._normalize_severity(os.environ.get("ALERT_MIN_SEVERITY", "medium"))
        self._dedup_window_sec = max(0, int(os.environ.get("ALERT_DEDUP_WINDOW_SEC", "90")))
        self._max_retries = max(0, int(os.environ.get("ALERT_RETRY_COUNT", "2")))
        self._max_dead_letters = max(1, int(os.environ.get("ALERT_DEAD_LETTER_MAX", "200")))
        self._recent_signatures: Dict[str, float] = {}
        self._dead_letters: list[Dict[str, Any]] = []
        self._dropped_by_threshold = 0
        self._dropped_by_dedup = 0

    @property
    def enabled(self) -> bool:
        return bool(self._webhook_url)

    def send(self, category: str, severity: str, message: str, **fields: Any) -> bool:
        if not self._webhook_url:
            return False
        if not self._severity_allowed(severity):
            self._dropped_by_threshold += 1
            return True

        self.flush_dead_letters()
        payload: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "category": category,
            "severity": self._normalize_severity(severity),
            "message": message,
        }
        payload.update(fields)
        signature = self._signature(payload)
        now = datetime.now(timezone.utc).timestamp()
        if self._dedup_window_sec > 0:
            expires_at = self._recent_signatures.get(signature, 0.0)
            if expires_at > now:
                self._dropped_by_dedup += 1
                return True
            self._recent_signatures[signature] = now + self._dedup_window_sec
            self._cleanup_recent_signatures(now)
        ok = self._deliver(payload)
        if ok:
            return True
        self._dead_letters.append({"payload": payload, "attempts": 1})
        if len(self._dead_letters) > self._max_dead_letters:
            self._dead_letters = self._dead_letters[-self._max_dead_letters :]
        return False

    def flush_dead_letters(self) -> int:
        if not self._dead_letters:
            return 0
        kept: list[Dict[str, Any]] = []
        delivered = 0
        for item in self._dead_letters:
            payload = item.get("payload") or {}
            attempts = int(item.get("attempts", 1) or 1)
            if self._deliver(payload):
                delivered += 1
                continue
            if attempts < self._max_retries + 1:
                kept.append({"payload": payload, "attempts": attempts + 1})
        self._dead_letters = kept
        return delivered

    def state(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "min_severity": self._min_severity,
            "dedup_window_sec": self._dedup_window_sec,
            "max_retries": self._max_retries,
            "queued_dead_letters": len(self._dead_letters),
            "dropped_by_threshold": self._dropped_by_threshold,
            "dropped_by_dedup": self._dropped_by_dedup,
        }

    def _deliver(self, payload: Dict[str, Any]) -> bool:
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

    def _severity_allowed(self, severity: str) -> bool:
        current = SEVERITY_ORDER.get(self._normalize_severity(severity), SEVERITY_ORDER["medium"])
        minimum = SEVERITY_ORDER.get(self._min_severity, SEVERITY_ORDER["medium"])
        return current >= minimum

    def _normalize_severity(self, severity: str) -> str:
        value = (severity or "").strip().lower()
        if value in SEVERITY_ORDER:
            return value
        return "medium"

    def _signature(self, payload: Dict[str, Any]) -> str:
        material = {
            "category": payload.get("category"),
            "severity": payload.get("severity"),
            "message": payload.get("message"),
            "run_id": payload.get("run_id"),
            "agent_id": payload.get("agent_id"),
            "job_id": payload.get("job_id"),
        }
        return json.dumps(material, ensure_ascii=True, sort_keys=True)

    def _cleanup_recent_signatures(self, now: float) -> None:
        expired = [k for k, ts in self._recent_signatures.items() if ts <= now]
        for key in expired:
            del self._recent_signatures[key]
