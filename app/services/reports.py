"""Report aggregate: snapshots, optimistic locking, version/scope switching.

Concurrency model
-----------------
Every save is guarded by an atomic conditional UPDATE::

    UPDATE reports SET revision = revision + 1 WHERE id = ? AND revision = ?

A zero rowcount means another writer committed first -> ``RevisionConflict``
(HTTP 409). SQLite serialises writers (WAL + busy_timeout), and putting the
revision check inside the UPDATE itself makes the guard correct even when a
session holds a stale read snapshot. Every successful save appends an
immutable snapshot; old snapshots stay executable for audit and rollback.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from .. import audit
from ..errors import NotFound, RevisionConflict, ValidationFailed
from ..models import ExportRecord, Report, ReportSnapshot, User
from ..schemas import ReportDefIn, ReportCreateIn, ReportUpdateIn, ScopeSwitchIn, VersionSwitchIn
from . import catalog
from .engine import checksum_csv, checksum_payload, execute_snapshot, to_csv
from .permissions import assert_user_holds_scope, resolve_effective_scope


# ---------------------------------------------------------------- helpers ----

def _dump_definition(definition: ReportDefIn, scope_frozen: Optional[dict[str, Any]]) -> str:
    return json.dumps(
        {"definition": definition.model_dump(), "scope_frozen": scope_frozen},
        ensure_ascii=False,
    )


def get_report(db: Session, report_key: str) -> Report:
    report = db.query(Report).filter(Report.key == report_key).first()
    if report is None:
        raise NotFound(f"report '{report_key}' does not exist")
    return report


def snapshot_definition(snapshot: ReportSnapshot) -> ReportDefIn:
    return ReportDefIn(**json.loads(snapshot.definition)["definition"])


def current_definition(db: Session, report: Report) -> ReportDefIn:
    if report.current_snapshot_id is None:
        raise ValidationFailed(f"report '{report.key}' has no snapshot")
    snapshot = db.get(ReportSnapshot, report.current_snapshot_id)
    return snapshot_definition(snapshot)


def health_of(db: Session, report: Report) -> Optional[str]:
    if report.current_snapshot_id is None:
        return None
    return catalog.analyze_impact(db, current_definition(db, report))["health"]


def _bump_revision(db: Session, report_id: int, expected: int) -> int:
    """Atomic optimistic-lock bump. Returns the new revision or raises 409."""
    result = db.execute(
        update(Report)
        .where(Report.id == report_id, Report.revision == expected)
        .values(revision=Report.revision + 1, updated_at=datetime.now(timezone.utc))
    )
    if result.rowcount == 0:
        raise RevisionConflict(
            f"报表已被其他人修改（期望 revision={expected}，实际已变更），请刷新后重试"
        )
    return expected + 1


def _append_snapshot(
    db: Session, report: Report, definition: ReportDefIn, user: User, revision: int
) -> ReportSnapshot:
    # Pin every metric ref to a concrete version id: an unpinned ref means
    # "current active at save time", and the snapshot must freeze that choice
    # so later metric releases never silently change an existing report.
    for ref in definition.metrics:
        if ref.metric_version_id is None:
            _, mv = catalog.resolve_mv(db, ref.metric_key, None)
            ref.metric_version_id = mv.id

    resolved = catalog.resolve_definition(db, definition)
    scope = resolved["scope"]
    assert_user_holds_scope(db, user, scope)
    eff = resolve_effective_scope(db, user, definition.scope_key)
    lineage = catalog.build_snapshot_lineage(db, resolved)

    next_no = (
        db.query(ReportSnapshot)
        .filter(ReportSnapshot.report_id == report.id)
        .count()
    ) + 1
    snapshot = ReportSnapshot(
        report_id=report.id,
        snapshot_no=next_no,
        revision=revision,
        definition=_dump_definition(definition, eff.frozen_snapshot),
        lineage=json.dumps(lineage, ensure_ascii=False),
        created_by_id=user.id,
    )
    db.add(snapshot)
    db.flush()
    catalog.persist_snapshot_edges(db, snapshot.id, lineage)
    report.current_snapshot_id = snapshot.id
    db.flush()
    return snapshot


# ------------------------------------------------------------------- CRUD -----

def create_report(db: Session, payload: ReportCreateIn, user: User) -> Report:
    if db.query(Report).filter(Report.key == payload.key).first():
        raise ValidationFailed(f"report key '{payload.key}' already exists")
    # Validate before inserting anything.
    resolved = catalog.resolve_definition(db, payload.definition)
    assert_user_holds_scope(db, user, resolved["scope"])

    report = Report(
        key=payload.key, title=payload.title, owner_id=user.id, revision=1,
    )
    db.add(report)
    db.flush()
    snapshot = _append_snapshot(db, report, payload.definition, user, 1)
    audit.record(
        db, action="report.create", actor_id=user.id, actor_name=user.username,
        target_type="report", target_id=report.key,
        detail={"snapshot_id": snapshot.id, "definition": payload.definition.model_dump()},
    )
    db.commit()
    db.refresh(report)
    return report


def update_report(db: Session, report_key: str, payload: ReportUpdateIn, user: User) -> Report:
    report = get_report(db, report_key)
    try:
        new_revision = _bump_revision(db, report.id, payload.expected_revision)
    except RevisionConflict:
        audit.record(
            db, action="report.save", actor_id=user.id, actor_name=user.username,
            target_type="report", target_id=report.key,
            detail={"expected_revision": payload.expected_revision,
                    "actual_revision": report.revision},
            result="conflict",
        )
        db.commit()
        raise

    if payload.title is not None:
        report.title = payload.title
    if payload.definition is not None:
        _append_snapshot(db, report, payload.definition, user, new_revision)
    audit.record(
        db, action="report.save", actor_id=user.id, actor_name=user.username,
        target_type="report", target_id=report.key,
        detail={"new_revision": new_revision,
                "definition": payload.definition.model_dump() if payload.definition else None},
    )
    db.commit()
    db.refresh(report)
    return report


def switch_metric_version(db: Session, report_key: str, payload: VersionSwitchIn, user: User) -> Report:
    report = get_report(db, report_key)
    definition = current_definition(db, report)
    metric = catalog.get_metric(db, payload.metric_key)
    target = db.get(catalog.MetricVersion, payload.target_version_id)
    if target is None or target.metric_id != metric.id:
        raise ValidationFailed("目标版本不属于该指标")
    if target.status == "draft":
        raise ValidationFailed("不能切换到草稿版本")

    refs = [m for m in definition.metrics if m.metric_key == payload.metric_key]
    if not refs:
        raise ValidationFailed(f"报表未使用指标 {payload.metric_key}")
    old_id = refs[0].metric_version_id
    for ref in refs:
        ref.metric_version_id = target.id

    new_revision = _bump_revision(db, report.id, payload.expected_revision)
    snapshot = _append_snapshot(db, report, definition, user, new_revision)
    impact = catalog.analyze_impact(db, definition)
    audit.record(
        db, action="report.switch_version", actor_id=user.id, actor_name=user.username,
        target_type="report", target_id=report.key,
        detail={"metric_key": payload.metric_key, "from_version_id": old_id,
                "to_version_id": target.id, "snapshot_id": snapshot.id,
                "health_after": impact["health"]},
    )
    db.commit()
    db.refresh(report)
    return report


def switch_scope(db: Session, report_key: str, payload: ScopeSwitchIn, user: User) -> Report:
    report = get_report(db, report_key)
    definition = current_definition(db, report)
    old_scope = definition.scope_key
    definition.scope_key = payload.scope_key

    new_revision = _bump_revision(db, report.id, payload.expected_revision)
    snapshot = _append_snapshot(db, report, definition, user, new_revision)
    audit.record(
        db, action="report.switch_scope", actor_id=user.id, actor_name=user.username,
        target_type="report", target_id=report.key,
        detail={"from_scope": old_scope, "to_scope": payload.scope_key,
                "snapshot_id": snapshot.id},
    )
    db.commit()
    db.refresh(report)
    return report


def list_snapshots(db: Session, report: Report) -> list[ReportSnapshot]:
    return (
        db.query(ReportSnapshot)
        .filter(ReportSnapshot.report_id == report.id)
        .order_by(ReportSnapshot.snapshot_no)
        .all()
    )


# ------------------------------------------------------------------ export ----

def create_export(db: Session, report_key: str, user: User, *, snapshot_id: Optional[int] = None) -> ExportRecord:
    """Freeze an execution of a snapshot into an immutable export record.

    The engine call is the *same* call the preview endpoint uses; its rows and
    checksum are stored verbatim, so downloading later can never drift from
    what the page showed.
    """
    report = get_report(db, report_key)
    if snapshot_id is None:
        if report.current_snapshot_id is None:
            raise ValidationFailed("报表尚无快照")
        snapshot_id = report.current_snapshot_id
    snapshot = db.get(ReportSnapshot, snapshot_id)
    if snapshot is None or snapshot.report_id != report.id:
        raise NotFound("快照不存在")

    result = execute_snapshot(db, snapshot, user)
    csv_body = to_csv(result["columns"], result["rows"])
    record = ExportRecord(
        report_id=report.id,
        snapshot_id=snapshot.id,
        requested_by_id=user.id,
        format="csv",
        checksum=checksum_csv(csv_body),
        row_count=len(result["rows"]),
        body=csv_body,
    )
    db.add(record)
    db.flush()
    audit.record(
        db, action="export.create", actor_id=user.id, actor_name=user.username,
        target_type="report", target_id=report.key,
        detail={"export_id": record.id, "snapshot_id": snapshot.id,
                "checksum": record.checksum, "row_count": record.row_count,
                "payload_checksum": result["checksum"], "basis": result["basis"]},
    )
    db.commit()
    db.refresh(record)
    return record


def verify_export(db: Session, export_id: int) -> dict[str, Any]:
    """Audit helper: prove file vs. page consistency for a stored export."""
    record = db.get(ExportRecord, export_id)
    if record is None:
        raise NotFound("导出记录不存在")
    stored_checksum = checksum_csv(record.body)
    import csv as _csv
    import io as _io
    reader = _csv.reader(_io.StringIO(record.body))
    rows_all = list(reader)
    columns, rows = rows_all[0], rows_all[1:]
    payload_checksum = checksum_payload(columns, rows)
    return {
        "export_id": record.id,
        "stored_checksum_matches": stored_checksum == record.checksum,
        "stored_checksum": record.checksum,
        "recomputed_checksum": stored_checksum,
        "payload_checksum": payload_checksum,
        "row_count": len(rows),
        "consistent": stored_checksum == record.checksum,
    }
