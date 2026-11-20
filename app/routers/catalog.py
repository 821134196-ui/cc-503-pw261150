"""Read-only catalog endpoints: metrics (with versions), dimensions, fields, scopes."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import DataSource, Dimension, Field, Metric, Scope
from ..schemas import DimensionOut, FieldOut, MetricOut, ScopeOut

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get("/metrics", response_model=list[MetricOut])
def list_metrics(db: Session = Depends(get_db)):
    return db.query(Metric).order_by(Metric.key).all()


@router.get("/dimensions", response_model=list[DimensionOut])
def list_dimensions(db: Session = Depends(get_db)):
    out = []
    for dim in db.query(Dimension).order_by(Dimension.key).all():
        field = db.get(Field, dim.field_id)
        out.append(DimensionOut(
            id=dim.id, key=dim.key, name=dim.name,
            field_name=field.name if field else "?", status=dim.status,
        ))
    return out


@router.get("/fields", response_model=list[FieldOut])
def list_fields(db: Session = Depends(get_db)):
    out = []
    for field in db.query(Field).order_by(Field.name).all():
        out.append(FieldOut(
            id=field.id, name=field.name, column_name=field.column_name,
            data_type=field.data_type, status=field.status,
            sunset_note=field.sunset_note or "",
            source=field.source.name if field.source else "?",
        ))
    return out


@router.get("/data-sources")
def list_sources(db: Session = Depends(get_db)):
    return [
        {"id": ds.id, "name": ds.name, "table": ds.table_name, "description": ds.description}
        for ds in db.query(DataSource).order_by(DataSource.name).all()
    ]


@router.get("/scopes", response_model=list[ScopeOut])
def list_scopes(db: Session = Depends(get_db)):
    return db.query(Scope).order_by(Scope.key).all()
