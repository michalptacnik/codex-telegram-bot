import json
from datetime import datetime, timezone
from logging import Logger
from typing import Any, Dict


def log_json(logger: Logger, event: str, **fields: Any) -> None:
    payload: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
    }
    payload.update(fields)
    logger.info(json.dumps(payload, ensure_ascii=True, sort_keys=True))

