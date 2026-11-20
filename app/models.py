"""Domain models for the versioned report builder.

Lineage direction (all edges point from a node *up* to what it depends on):

    DataSource <- Field <- MetricVersion <- ReportSnapshot
                     ^                       ^
                     +------ Dimension ------+

Every edge also carries the source entity version so a snapshot can answer
"which exact version of every upstream thing did I use when I ran".
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- catalog ---

class DataSource(Base):
    __tablename__ = "data_sources"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    table_name = Column(String(120), unique=True, nullable=False)
    description = Column(Text, default="")
    fields = relationship("Field", back_populates="source", cascade="all, delete-orphan")


class Field(Base):
    """A physical column. ``status`` drives field-sunset behaviour."""

    __tablename__ = "fields"
    __table_args__ = (UniqueConstraint("source_id", "column_name", name="uq_field_column"),)

    id = Column(Integer, primary_key=True)
    source_id = Column(Integer, ForeignKey("data_sources.id"), nullable=False)
    name = Column(String(120), nullable=False)          # logical name used inside reports
    column_name = Column(String(120), nullable=False)  # physical SQL column
    data_type = Column(String(40), nullable=False)
    status = Column(String(20), default="active", nullable=False)  # active | deprecated | retired
    sunset_note = Column(Text, default="")
    source = relationship("DataSource", back_populates="fields")


class Metric(Base):
    """A metric definition; its concrete implementations are versioned."""

    __tablename__ = "metrics"
    id = Column(Integer, primary_key=True)
    key = Column(String(80), unique=True, nullable=False)
    name = Column(String(120), nullable=False)
    description = Column(Text, default="")
    versions = relationship(
        "MetricVersion", back_populates="metric", cascade="all, delete-orphan",
        order_by="MetricVersion.version",
    )


class MetricVersion(Base):
    __tablename__ = "metric_versions"
    __table_args__ = (UniqueConstraint("metric_id", "version", name="uq_metric_version"),)

    id = Column(Integer, primary_key=True)
    metric_id = Column(Integer, ForeignKey("metrics.id"), nullable=False)
    version = Column(Integer, nullable=False)
    status = Column(String(20), default="draft", nullable=False)  # draft | active | deprecated
    # expression references logical field names, e.g. "SUM(amount_usd)"
    expression = Column(Text, nullable=False)
    output_type = Column(String(40), default="number")
    change_note = Column(Text, default="")
    created_at = Column(DateTime, default=utcnow)
    metric = relationship("Metric", back_populates="versions")


class Dimension(Base):
    __tablename__ = "dimensions"
    id = Column(Integer, primary_key=True)
    key = Column(String(80), unique=True, nullable=False)
    name = Column(String(120), nullable=False)
    field_id = Column(Integer, ForeignKey("fields.id"), nullable=False)
    status = Column(String(20), default="active", nullable=False)  # active | deprecated | retired


class LineageEdge(Base):
    """Direct dependency edge with the upstream version captured at write time."""

    __tablename__ = "lineage_edges"
    id = Column(Integer, primary_key=True)
    # downstream node
    from_kind = Column(String(30), nullable=False)  # metric_version | report_snapshot
    from_version_id = Column(Integer, nullable=False)
    # upstream node
    to_kind = Column(String(30), nullable=False)    # field | dimension | data_source | metric_version
    to_id = Column(Integer, nullable=False)
    to_version = Column(String(40), nullable=False)  # field/ds revision label or metric version "v3"
    label = Column(String(200), default="")


# ------------------------------------------------------------- permissions ---

class Scope(Base):
    """A permission scope: row-level filter plus a column allow-list.

    ``column_allow`` is JSON; ``null``/empty means "all columns the user may
    otherwise see". Row filters are AND-ed into every query the scope runs.
    """

    __tablename__ = "scopes"
    id = Column(Integer, primary_key=True)
    key = Column(String(80), unique=True, nullable=False)
    name = Column(String(120), nullable=False)
    row_filters = Column(Text, default="[]")   # JSON list of {field, op, value}
    column_allow = Column(Text, default="null")  # JSON list of logical field names, or null
    status = Column(String(20), default="active", nullable=False)  # active | revoked
    users = relationship("UserScope", back_populates="scope", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(80), unique=True, nullable=False)
    display_name = Column(String(120), nullable=False)
    role = Column(String(40), default="analyst")
    scopes = relationship("UserScope", back_populates="user", cascade="all, delete-orphan")


class UserScope(Base):
    __tablename__ = "user_scopes"
    __table_args__ = (UniqueConstraint("user_id", "scope_id", name="uq_user_scope"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    scope_id = Column(Integer, ForeignKey("scopes.id"), nullable=False)
    user = relationship("User", back_populates="scopes")
    scope = relationship("Scope", back_populates="users")


# ----------------------------------------------------------------- reports ---

class Report(Base):
    __tablename__ = "reports"
    id = Column(Integer, primary_key=True)
    key = Column(String(80), unique=True, nullable=False)
    title = Column(String(200), nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    # Optimistic-locking revision; every save increments it.
    revision = Column(Integer, default=1, nullable=False)
    current_snapshot_id = Column(
        Integer,
        ForeignKey("report_snapshots.id", use_alter=True, name="fk_report_current_snapshot"),
        nullable=True,
    )
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    owner = relationship("User")
    snapshots = relationship(
        "ReportSnapshot", back_populates="report",
        cascade="all, delete-orphan",
        foreign_keys="ReportSnapshot.report_id",
        order_by="ReportSnapshot.snapshot_no",
    )
    current_snapshot = relationship("ReportSnapshot", foreign_keys=[current_snapshot_id])


class ReportSnapshot(Base):
    """An immutable, version-pinned point-in-time definition of a report."""

    __tablename__ = "report_snapshots"
    id = Column(Integer, primary_key=True)
    report_id = Column(Integer, ForeignKey("reports.id"), nullable=False)
    snapshot_no = Column(Integer, nullable=False)
    revision = Column(Integer, nullable=False)
    # Full self-contained definition: metrics (with mv ids), dimensions,
    # filters, scope id + a frozen copy of the scope at save time.
    definition = Column(Text, nullable=False)          # JSON
    lineage = Column(Text, nullable=False)             # JSON list of dependency refs
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=utcnow)
    report = relationship("Report", back_populates="snapshots", foreign_keys=[report_id])
    created_by = relationship("User")


class ExportRecord(Base):
    """An export is generated from a snapshot and the result is frozen here.

    Preview and export call the same engine; the stored checksum + body make
    any later page/file divergence detectable, and make the download
    deterministic (it never re-queries).
    """

    __tablename__ = "export_records"
    id = Column(Integer, primary_key=True)
    report_id = Column(Integer, ForeignKey("reports.id"), nullable=False)
    snapshot_id = Column(Integer, ForeignKey("report_snapshots.id"), nullable=False)
    requested_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    format = Column(String(10), default="csv")
    checksum = Column(String(64), nullable=False)       # sha256 of body
    row_count = Column(Integer, nullable=False)
    body = Column(Text, nullable=False)                 # frozen CSV content
    created_at = Column(DateTime, default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id = Column(Integer, primary_key=True)
    at = Column(DateTime, default=utcnow, index=True)
    actor_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    actor_name = Column(String(80), default="")
    action = Column(String(80), nullable=False)         # e.g. report.save / export.create
    target_type = Column(String(40), default="")
    target_id = Column(String(80), default="")
    detail = Column(Text, default="")                   # JSON
    result = Column(String(20), default="success")      # success | denied | conflict
