"""Audit logging helper — every state-changing or sensitive read is recorded."""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy.orm import Session

from .models import AuditEvent


def record(
    db: Session,
    *,
    action: str,
    actor_id: Optional[int] = None,
    actor_name: str = "",
    target_type: str = "",
    target_id: str = "",
    detail: Any = None,
    result: str = "success",
) -> AuditEvent:
    event = AuditEvent(
        action=action,
        actor_id=actor_id,
        actor_name=actor_name,
        target_type=target_type,
        target_id=str(target_id),
        detail=json.dumps(detail, ensure_ascii=False, default=str) if detail is not None else "",
        result=result,
    )
    db.add(event)
    db.flush()
    return event
