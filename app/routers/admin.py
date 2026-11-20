"""Admin/demo endpoints: metric version lifecycle, field sunset, scope grants/revocation, audit."""
from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import current_user
from ..errors import NotFound, ValidationFailed
from ..models import AuditEvent, Field, Metric, MetricVersion, Scope, User, UserScope
from ..services import catalog as catalog_svc
from ..services.permissions import user_scopes
from ..schemas import AuditOut

router = APIRouter(prefix="/api/admin", tags=["admin"])


class NewVersionIn(BaseModel):
    expression: str
    change_note: str = ""
    deprecate_previous: bool = True


class FieldStatusIn(BaseModel):
    status: str                       # active | deprecated | retired
    sunset_note: Optional[str] = None


class ScopeUpsertIn(BaseModel):
    name: str
    row_filters: list[dict[str, Any]] = []
    column_allow: Optional[list[str]] = None
    status: str = "active"


class GrantIn(BaseModel):
    username: str
    scope_key: str


@router.post("/metrics/{metric_key}/versions")
def publish_version(metric_key: str, payload: NewVersionIn, db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    """Publish a new metric version; the previous active one is deprecated."""
    metric = catalog_svc.get_metric(db, metric_key)
    known = catalog_svc.all_fields_map(db)
    fields = catalog_svc.expression_fields(payload.expression, known)
    unknown = catalog_svc.unknown_identifiers(payload.expression, known)
    if unknown:
        raise ValidationFailed(f"表达式引用未知字段: {sorted(unknown)}")

    previous = catalog_svc.active_version(metric)
    next_no = max(v.version for v in metric.versions) + 1
    mv = MetricVersion(
        metric_id=metric.id, version=next_no, status="active",
        expression=payload.expression, change_note=payload.change_note,
    )
    db.add(mv)
    db.flush()
    catalog_svc.persist_metric_edges(db, mv, fields)
    if payload.deprecate_previous:
        previous.status = "deprecated"
    audit.record(
        db, action="metric.publish_version", actor_id=user.id, actor_name=user.username,
        target_type="metric", target_id=metric.key,
        detail={"new_version": next_no, "previous_version": previous.version,
                "expression": payload.expression},
    )
    db.commit()
    return {"metric_key": metric.key, "new_version_id": mv.id, "version": next_no,
            "deprecated_previous": payload.deprecate_previous}


@router.post("/fields/{field_name}/status")
def set_field_status(field_name: str, payload: FieldStatusIn, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    if payload.status not in {"active", "deprecated", "retired"}:
        raise ValidationFailed("status must be active|deprecated|retired")
    field = catalog_svc.get_field_by_name(db, field_name)
    old = field.status
    field.status = payload.status
    if payload.sunset_note is not None:
        field.sunset_note = payload.sunset_note
    audit.record(
        db, action="field.set_status", actor_id=user.id, actor_name=user.username,
        target_type="field", target_id=field.name,
        detail={"from": old, "to": payload.status, "note": payload.sunset_note},
    )
    db.commit()
    return {"field": field.name, "from": old, "to": field.status}


@router.put("/scopes/{scope_key}")
def upsert_scope(scope_key: str, payload: ScopeUpsertIn, db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    scope = db.query(Scope).filter(Scope.key == scope_key).first()
    created = scope is None
    if scope is None:
        scope = Scope(key=scope_key)
        db.add(scope)
    scope.name = payload.name
    scope.row_filters = json.dumps(payload.row_filters, ensure_ascii=False)
    scope.column_allow = json.dumps(payload.column_allow, ensure_ascii=False)
    scope.status = payload.status
    db.flush()
    audit.record(
        db, action="scope.upsert", actor_id=user.id, actor_name=user.username,
        target_type="scope", target_id=scope.key,
        detail={"created": created, "status": scope.status,
                "row_filters": payload.row_filters, "column_allow": payload.column_allow},
    )
    db.commit()
    return {"scope_key": scope.key, "id": scope.id, "created": created, "status": scope.status}


@router.post("/scopes/{scope_key}/revoke")
def revoke_scope(scope_key: str, db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    """Revocation is live: every report/preview using this scope fails afterwards."""
    scope = db.query(Scope).filter(Scope.key == scope_key).first()
    if scope is None:
        raise NotFound(f"scope '{scope_key}' not found")
    scope.status = "revoked"
    audit.record(
        db, action="scope.revoke", actor_id=user.id, actor_name=user.username,
        target_type="scope", target_id=scope.key,
        detail={"affected_grants": len(scope.users)},
    )
    db.commit()
    return {"scope_key": scope.key, "status": "revoked", "affected_grants": len(scope.users)}


@router.post("/grants")
def grant_scope(payload: GrantIn, db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    target = db.query(User).filter(User.username == payload.username).first()
    scope = db.query(Scope).filter(Scope.key == payload.scope_key).first()
    if target is None or scope is None:
        raise NotFound("用户或权限范围不存在")
    exists = db.query(UserScope).filter(
        UserScope.user_id == target.id, UserScope.scope_id == scope.id
    ).first()
    if not exists:
        db.add(UserScope(user_id=target.id, scope_id=scope.id))
    audit.record(
        db, action="scope.grant", actor_id=user.id, actor_name=user.username,
        target_type="user_scope", target_id=f"{payload.username}:{payload.scope_key}",
        detail={"created": not exists},
    )
    db.commit()
    return {"username": payload.username, "scope_key": payload.scope_key, "created": not exists}


@router.get("/users")
def list_users(db: Session = Depends(get_db), user: User = Depends(current_user)):
    out = []
    for u in db.query(User).order_by(User.username).all():
        out.append({
            "username": u.username, "display_name": u.display_name, "role": u.role,
            "scopes": [
                {"key": s.key, "name": s.name, "status": s.status} for s in user_scopes(db, u)
            ],
        })
    return out


@router.get("/audit", response_model=list[AuditOut])
def query_audit(action: str | None = None, target_id: str | None = None,
                limit: int = 100, db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    q = db.query(AuditEvent)
    if action:
        q = q.filter(AuditEvent.action == action)
    if target_id:
        q = q.filter(AuditEvent.target_id == target_id)
    events = q.order_by(AuditEvent.id.desc()).limit(min(limit, 500)).all()
    return [
        AuditOut(
            id=e.id, at=e.at, actor_name=e.actor_name, action=e.action,
            target_type=e.target_type, target_id=e.target_id,
            detail=json.loads(e.detail) if e.detail else None, result=e.result,
        )
        for e in events
    ]
