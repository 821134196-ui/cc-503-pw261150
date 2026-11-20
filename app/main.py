"""FastAPI application entry point."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import audit
from .db import Base, SessionLocal, engine
from .errors import PermissionDenied, ReportBuilderError
from .routers import admin, catalog, reports


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="Pointwise Report Builder", version="1.0.0", lifespan=lifespan)

app.include_router(catalog.router)
app.include_router(reports.router)
app.include_router(admin.router)


@app.exception_handler(ReportBuilderError)
async def domain_error_handler(request: Request, exc: ReportBuilderError):
    # Permission denials are security-relevant and must land in the audit trail
    # even when raised deep inside the engine, outside a route's try/except.
    if isinstance(exc, PermissionDenied):
        db = SessionLocal()
        try:
            actor = request.headers.get("x-username", "") or request.query_params.get("username", "")
            path_parts = [p for p in request.url.path.split("/") if p]
            audit.record(
                db, action="access.denied", actor_name=actor,
                target_type=path_parts[1] if len(path_parts) > 1 else "resource",
                target_id="/".join(path_parts[2:4]),
                detail={"path": request.url.path, "reason": str(exc)},
                result="denied",
            )
            db.commit()
        finally:
            db.close()
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": str(exc)}},
    )


@app.get("/api/health")
def health():
    return {"status": "ok"}


_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/app", StaticFiles(directory=_static_dir, html=True), name="static")
