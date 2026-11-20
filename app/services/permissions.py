"""Permission scopes: row-level filters and column-level allow-lists.

A scope is resolved *at execution time* against its current row in the
database — revoking a scope therefore takes effect immediately, even on
reports that pinned the scope long ago. The snapshot keeps a frozen copy for
lineage display, but enforcement always uses the live record.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..errors import PermissionDenied
from ..models import Scope, User, UserScope


@dataclass
class EffectiveScope:
    scope_id: Optional[int]
    key: Optional[str]
    row_filters: list[dict[str, Any]] = field(default_factory=list)
    column_allow: Optional[set[str]] = None  # None => no column restriction
    frozen_snapshot: Optional[dict[str, Any]] = None


def get_user(db: Session, username: str) -> User:
    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise PermissionDenied(f"unknown user '{username}'")
    return user


def user_scopes(db: Session, user: User) -> list[Scope]:
    rows = (
        db.query(Scope)
        .join(UserScope, UserScope.scope_id == Scope.id)
        .filter(UserScope.user_id == user.id)
        .all()
    )
    return rows


def resolve_effective_scope(db: Session, user: User, scope_key: Optional[str]) -> EffectiveScope:
    """Resolve the scope a user actually runs under, with live enforcement."""
    if scope_key is None:
        # Personal baseline: no extra row filters, no column restriction.
        return EffectiveScope(scope_id=None, key=None)

    scope = db.query(Scope).filter(Scope.key == scope_key).first()
    if scope is None:
        raise PermissionDenied(f"权限范围 '{scope_key}' 不存在")

    granted = (
        db.query(UserScope)
        .filter(UserScope.user_id == user.id, UserScope.scope_id == scope.id)
        .first()
    )
    if granted is None:
        raise PermissionDenied(
            f"用户 {user.username} 未被授予权限范围 {scope.name}（scope_key={scope.key}）"
        )
    if scope.status == "revoked":
        # Live revocation: the grant still exists historically, but the scope
        # is no longer usable. Existing saved reports surface this as broken.
        raise PermissionDenied(
            f"权限范围 {scope.name}（scope_key={scope.key}）已被撤销，无法执行报表"
        )

    row_filters = json.loads(scope.row_filters or "[]")
    raw_allow = json.loads(scope.column_allow or "null")
    column_allow = set(raw_allow) if raw_allow else None
    return EffectiveScope(
        scope_id=scope.id,
        key=scope.key,
        row_filters=row_filters,
        column_allow=column_allow,
        frozen_snapshot={
            "scope_id": scope.id,
            "key": scope.key,
            "name": scope.name,
            "row_filters": row_filters,
            "column_allow": sorted(column_allow) if column_allow else None,
        },
    )


def assert_user_holds_scope(db: Session, user: User, scope: Optional[Scope]) -> None:
    """Build/save-time check that the chosen scope is currently granted."""
    if scope is None:
        return
    if scope.status == "revoked":
        raise PermissionDenied(f"权限范围 {scope.name} 已被撤销")
    granted = (
        db.query(UserScope)
        .filter(UserScope.user_id == user.id, UserScope.scope_id == scope.id)
        .first()
    )
    if granted is None:
        raise PermissionDenied(f"用户 {user.username} 未被授予权限范围 {scope.name}")
