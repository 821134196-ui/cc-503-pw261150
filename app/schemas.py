"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# ------------------------------------------------------------------ catalog ---

class FieldOut(BaseModel):
    id: int
    name: str
    column_name: str
    data_type: str
    status: str
    sunset_note: str
    source: str

    model_config = ConfigDict(from_attributes=True)


class MetricVersionOut(BaseModel):
    id: int
    version: int
    status: str
    expression: str
    change_note: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MetricOut(BaseModel):
    id: int
    key: str
    name: str
    description: str
    versions: list[MetricVersionOut]

    model_config = ConfigDict(from_attributes=True)


class DimensionOut(BaseModel):
    id: int
    key: str
    name: str
    field_name: str
    status: str

    model_config = ConfigDict(from_attributes=True)


class ScopeOut(BaseModel):
    id: int
    key: str
    name: str
    row_filters: Any
    column_allow: Any
    status: str

    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------------- reports ---

class FilterSpec(BaseModel):
    field: str
    op: str = Field(description="one of: eq, ne, gt, gte, lt, lte, in")
    value: Any


class MetricRef(BaseModel):
    metric_key: str
    metric_version_id: Optional[int] = None  # default: current active version


class ReportDefIn(BaseModel):
    metrics: list[MetricRef]
    dimensions: list[str] = []          # dimension keys
    filters: list[FilterSpec] = []
    scope_key: Optional[str] = None


class ReportCreateIn(BaseModel):
    key: str
    title: str
    definition: ReportDefIn


class ReportUpdateIn(BaseModel):
    title: Optional[str] = None
    definition: Optional[ReportDefIn] = None
    expected_revision: int             # optimistic-lock guard


class ReportSnapshotOut(BaseModel):
    id: int
    snapshot_no: int
    revision: int
    definition: Any
    lineage: Any
    created_by: str
    created_at: datetime


class ReportOut(BaseModel):
    id: int
    key: str
    title: str
    owner: str
    revision: int
    current_snapshot_id: Optional[int]
    updated_at: datetime
    health: Optional[str] = None        # ok | impacted | broken (computed on read)


class VersionSwitchIn(BaseModel):
    metric_key: str
    target_version_id: int
    expected_revision: int


class ScopeSwitchIn(BaseModel):
    scope_key: Optional[str] = None
    expected_revision: int


class PreviewIn(BaseModel):
    """Preview either a saved snapshot or an ad-hoc definition."""

    snapshot_id: Optional[int] = None
    definition: Optional[ReportDefIn] = None


class PreviewOut(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    lineage: list[dict[str, Any]]
    warnings: list[str]
    checksum: str
    basis: str                          # snapshot id or "ad-hoc"


class ExportOut(BaseModel):
    export_id: int
    report_id: int
    snapshot_id: int
    format: str
    checksum: str
    row_count: int
    created_at: datetime
    download_url: str


class ImpactOut(BaseModel):
    report_key: str
    revision: int
    health: str                         # ok | impacted | broken
    issues: list[dict[str, Any]]
    switch_options: list[dict[str, Any]]


class AuditOut(BaseModel):
    id: int
    at: datetime
    actor_name: str
    action: str
    target_type: str
    target_id: str
    detail: Any
    result: str
