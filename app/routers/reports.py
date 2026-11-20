"""Report endpoints: build, save, preview, lineage, impact, version/scope switch, export."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import current_user
from ..models import ExportRecord, Report, ReportSnapshot, User
from ..schemas import (
    AuditOut,
    ExportOut,
    ImpactOut,
    PreviewIn,
    PreviewOut,
    ReportCreateIn,
    ReportOut,
    ReportSnapshotOut,
    ReportUpdateIn,
    ScopeSwitchIn,
    VersionSwitchIn,
)
from ..services import catalog as catalog_svc
from ..services import reports as reports_svc
from ..services.engine import execute_adhoc, execute_snapshot

router = APIRouter(prefix="/api/reports", tags=["reports"])


def _report_out(db: Session, report: Report) -> ReportOut:
    return ReportOut(
        id=report.id, key=report.key, title=report.title,
        owner=report.owner.username if report.owner else "?",
        revision=report.revision, current_snapshot_id=report.current_snapshot_id,
        updated_at=report.updated_at, health=reports_svc.health_of(db, report),
    )


@router.get("", response_model=list[ReportOut])
def list_reports(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return [_report_out(db, r) for r in db.query(Report).order_by(Report.key).all()]


@router.post("", response_model=ReportOut, status_code=201)
def create_report(payload: ReportCreateIn, db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    report = reports_svc.create_report(db, payload, user)
    return _report_out(db, report)


@router.put("/{report_key}", response_model=ReportOut)
def update_report(report_key: str, payload: ReportUpdateIn, db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    report = reports_svc.update_report(db, report_key, payload, user)
    return _report_out(db, report)


@router.get("/{report_key}", response_model=ReportOut)
def get_report(report_key: str, db: Session = Depends(get_db),
               user: User = Depends(current_user)):
    return _report_out(db, reports_svc.get_report(db, report_key))


@router.post("/preview", response_model=PreviewOut)
def preview(payload: PreviewIn, db: Session = Depends(get_db),
            user: User = Depends(current_user)):
    """Preview via snapshot OR an ad-hoc builder definition — one engine, one checksum."""
    if payload.snapshot_id is not None:
        snapshot = db.get(ReportSnapshot, payload.snapshot_id)
        if snapshot is None:
            from ..errors import NotFound
            raise NotFound("快照不存在")
        result = execute_snapshot(db, snapshot, user)
        audit.record(
            db, action="report.preview", actor_id=user.id, actor_name=user.username,
            target_type="snapshot", target_id=snapshot.id,
            detail={"basis": result["basis"], "checksum": result["checksum"]},
        )
        db.commit()
        return result
    if payload.definition is None:
        from ..errors import ValidationFailed
        raise ValidationFailed("preview 需要 snapshot_id 或 definition")
    result = execute_adhoc(db, payload.definition, user)
    return result


@router.get("/{report_key}/impact", response_model=ImpactOut)
def impact(report_key: str, db: Session = Depends(get_db),
           user: User = Depends(current_user)):
    report = reports_svc.get_report(db, report_key)
    definition = reports_svc.current_definition(db, report)
    result = catalog_svc.analyze_impact(db, definition)
    return ImpactOut(report_key=report.key, revision=report.revision, **result)


@router.get("/{report_key}/lineage")
def lineage(report_key: str, db: Session = Depends(get_db),
            user: User = Depends(current_user)):
    """Full version-pinned lineage of the current snapshot (also embedded in snapshot)."""
    report = reports_svc.get_report(db, report_key)
    if report.current_snapshot_id is None:
        from ..errors import ValidationFailed
        raise ValidationFailed("报表尚无快照")
    snapshot = db.get(ReportSnapshot, report.current_snapshot_id)
    return {
        "report_key": report.key,
        "revision": report.revision,
        "snapshot_id": snapshot.id,
        "nodes": json.loads(snapshot.lineage),
    }


@router.get("/{report_key}/snapshots", response_model=list[ReportSnapshotOut])
def snapshots(report_key: str, db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    report = reports_svc.get_report(db, report_key)
    out = []
    for snap in reports_svc.list_snapshots(db, report):
        out.append(ReportSnapshotOut(
            id=snap.id, snapshot_no=snap.snapshot_no, revision=snap.revision,
            definition=json.loads(snap.definition), lineage=json.loads(snap.lineage),
            created_by=snap.created_by.username if snap.created_by else "?",
            created_at=snap.created_at,
        ))
    return out


@router.post("/{report_key}/switch-version", response_model=ReportOut)
def switch_version(report_key: str, payload: VersionSwitchIn, db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    report = reports_svc.switch_metric_version(db, report_key, payload, user)
    return _report_out(db, report)


@router.post("/{report_key}/switch-scope", response_model=ReportOut)
def switch_scope(report_key: str, payload: ScopeSwitchIn, db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    report = reports_svc.switch_scope(db, report_key, payload, user)
    return _report_out(db, report)


@router.post("/{report_key}/exports", response_model=ExportOut, status_code=201)
def create_export(report_key: str, snapshot_id: int | None = None,
                  db: Session = Depends(get_db), user: User = Depends(current_user)):
    record = reports_svc.create_export(db, report_key, user, snapshot_id=snapshot_id)
    return ExportOut(
        export_id=record.id, report_id=record.report_id, snapshot_id=record.snapshot_id,
        format=record.format, checksum=record.checksum, row_count=record.row_count,
        created_at=record.created_at,
        download_url=f"/api/reports/{report_key}/exports/{record.id}/download",
    )


@router.get("/{report_key}/exports/{export_id}/download", response_class=PlainTextResponse)
def download_export(report_key: str, export_id: int, db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    record = db.get(ExportRecord, export_id)
    report = reports_svc.get_report(db, report_key)
    if record is None or record.report_id != report.id:
        from ..errors import NotFound
        raise NotFound("导出记录不存在")
    audit.record(
        db, action="export.download", actor_id=user.id, actor_name=user.username,
        target_type="export", target_id=record.id,
        detail={"checksum": record.checksum},
    )
    db.commit()
    return PlainTextResponse(
        content=record.body, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{report.key}-export-{export_id}.csv"',
                 "X-Export-Checksum": record.checksum},
    )


@router.get("/{report_key}/exports/{export_id}/verify")
def verify_export(report_key: str, export_id: int, db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    reports_svc.get_report(db, report_key)
    return reports_svc.verify_export(db, export_id)


@router.get("/{report_key}/exports")
def list_exports(report_key: str, db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    report = reports_svc.get_report(db, report_key)
    records = (
        db.query(ExportRecord)
        .filter(ExportRecord.report_id == report.id)
        .order_by(ExportRecord.id.desc())
        .all()
    )
    return [
        {"export_id": r.id, "snapshot_id": r.snapshot_id, "format": r.format,
         "checksum": r.checksum, "row_count": r.row_count,
         "requested_by": db.get(User, r.requested_by_id).username, "created_at": r.created_at}
        for r in records
    ]
