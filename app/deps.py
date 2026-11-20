"""Shared FastAPI dependencies."""
from __future__ import annotations

from fastapi import Depends, Header, Query
from sqlalchemy.orm import Session

from .db import get_db
from .errors import PermissionDenied
from .models import User
from .services.permissions import get_user


def current_user(
    x_username: str = Header(default="", alias="X-Username"),
    username: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> User:
    """Auth is header-based for the demo: ``X-Username: alice``.

    The seed creates users with different scopes so permission revocation can
    be exercised end-to-end without a full identity provider.
    """
    name = x_username or username or "alice"
    return get_user(db, name)
