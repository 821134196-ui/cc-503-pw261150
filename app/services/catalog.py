"""Catalog resolution, lineage construction and impact analysis.

The catalog is the single source of truth for metrics (versioned), dimensions
and physical fields (with sunset status). Reports never embed SQL — they embed
*refs* (metric key + pinned version id, dimension key, field name), and this
module resolves those refs against the catalog every time a report is saved,
previewed or exported.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..errors import BrokenSnapshot, NotFound, ValidationFailed
from ..models import DataSource, Dimension, Field, LineageEdge, Metric, MetricVersion, Scope
from ..schemas import ReportDefIn

_SQL_WORDS = {
    "sum", "count", "avg", "min", "max", "distinct", "case", "when", "then",
    "else", "end", "null", "and", "or", "not", "as", "if", "coalesce", "cast",
    "integer", "float", "text", "round", "nullif", "between", "is", "in", "like",
}
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# ------------------------------------------------------------------ lookup ---

def get_metric(db: Session, metric_key: str) -> Metric:
    metric = db.query(Metric).filter(Metric.key == metric_key).first()
    if metric is None:
        raise NotFound(f"metric '{metric_key}' does not exist")
    return metric


def active_version(metric: Metric) -> MetricVersion:
    active = [v for v in metric.versions if v.status == "active"]
    if not active:
        raise ValidationFailed(f"metric '{metric.key}' has no active version")
    return max(active, key=lambda v: v.version)


def resolve_mv(db: Session, metric_key: str, mv_id: Optional[int]) -> tuple[Metric, MetricVersion]:
    metric = get_metric(db, metric_key)
    if mv_id is None:
        return metric, active_version(metric)
    mv = db.get(MetricVersion, mv_id)
    if mv is None or mv.metric_id != metric.id:
        raise ValidationFailed(
            f"metric version id {mv_id} does not belong to metric '{metric_key}'"
        )
    if mv.status == "draft":
        raise ValidationFailed(f"metric '{metric_key}' v{mv.version} is a draft and cannot be used")
    return metric, mv


def get_dimension(db: Session, key: str) -> Dimension:
    dim = db.query(Dimension).filter(Dimension.key == key).first()
    if dim is None:
        raise NotFound(f"dimension '{key}' does not exist")
    return dim


def get_field_by_name(db: Session, name: str) -> Field:
    field = db.query(Field).filter(Field.name == name).first()
    if field is None:
        raise NotFound(f"field '{name}' does not exist")
    return field


def get_scope(db: Session, key: Optional[str]) -> Optional[Scope]:
    if key is None:
        return None
    scope = db.query(Scope).filter(Scope.key == key).first()
    if scope is None:
        raise NotFound(f"scope '{key}' does not exist")
    return scope


def expression_fields(expression: str, known: dict[str, Field]) -> dict[str, Field]:
    """Return the logical fields an expression references (identifiers minus SQL words)."""
    found: dict[str, Field] = {}
    for token in _IDENT_RE.findall(expression):
        if token.lower() in _SQL_WORDS:
            continue
        if token in known:
            found[token] = known[token]
    return found


def unknown_identifiers(expression: str, known: dict[str, Field]) -> set[str]:
    """Identifiers that are neither SQL keywords nor known logical fields."""
    return {
        token
        for token in _IDENT_RE.findall(expression)
        if token.lower() not in _SQL_WORDS and token not in known
    }


def all_fields_map(db: Session) -> dict[str, Field]:
    return {f.name: f for f in db.query(Field).all()}


# --------------------------------------------------------------- resolution ---

def resolve_definition(db: Session, definition: ReportDefIn, *, allow_retired: bool = False) -> dict[str, Any]:
    """Resolve every ref in a report definition to live catalog entities.

    Returns a structured bundle the engine and snapshot builder share.
    ``allow_retired=False`` (build/save path) rejects retired entities hard;
    the snapshot-execution path catches them separately so it can label the
    snapshot *broken* instead.
    """
    warnings: list[str] = []
    known_fields = all_fields_map(db)

    metrics_resolved: list[dict[str, Any]] = []
    for ref in definition.metrics:
        metric, mv = resolve_mv(db, ref.metric_key, ref.metric_version_id)
        fields = expression_fields(mv.expression, known_fields)
        unknown = unknown_identifiers(mv.expression, known_fields)
        if unknown:
            raise ValidationFailed(
                f"metric '{metric.key}' v{mv.version} expression references unknown fields: {sorted(unknown)}"
            )
        if mv.status == "deprecated":
            warnings.append(
                f"指标 {metric.name} v{mv.version} 已弃用（deprecated），建议切换到新版本"
            )
        for fname, field in fields.items():
            if field.status == "retired":
                msg = f"字段 {fname} 已下线（retired），指标 {metric.key} v{mv.version} 不可再用"
                if not allow_retired:
                    raise ValidationFailed(msg)
            elif field.status == "deprecated":
                warnings.append(f"字段 {fname} 已标记弃用，计划下线：{field.sunset_note or '无说明'}")
        metrics_resolved.append({"ref": ref, "metric": metric, "mv": mv, "fields": fields})

    dims_resolved: list[dict[str, Any]] = []
    for key in definition.dimensions:
        dim = get_dimension(db, key)
        field = db.get(Field, dim.field_id)
        if dim.status == "retired" or field.status == "retired":
            msg = f"维度 {key} 已下线"
            if not allow_retired:
                raise ValidationFailed(msg)
        elif dim.status == "deprecated" or field.status == "deprecated":
            warnings.append(f"维度 {dim.name}（{key}）已弃用，建议替换")
        dims_resolved.append({"dim": dim, "field": field})

    filters_resolved: list[dict[str, Any]] = []
    valid_ops = {"eq", "ne", "gt", "gte", "lt", "lte", "in"}
    for spec in definition.filters:
        if spec.op not in valid_ops:
            raise ValidationFailed(f"unsupported filter op '{spec.op}'")
        field = get_field_by_name(db, spec.field)
        if field.status == "retired" and not allow_retired:
            raise ValidationFailed(f"筛选字段 {spec.field} 已下线")
        filters_resolved.append({"spec": spec, "field": field})

    scope = get_scope(db, definition.scope_key)

    return {
        "metrics": metrics_resolved,
        "dimensions": dims_resolved,
        "filters": filters_resolved,
        "scope": scope,
        "warnings": warnings,
        "known_fields": known_fields,
    }


# ------------------------------------------------------------------ lineage ---

def _field_ref(field: Field) -> dict[str, Any]:
    source = field.source
    return {
        "kind": "field",
        "id": field.id,
        "name": field.name,
        "physical_column": field.column_name,
        "status": field.status,
        "version": f"f{field.id}@{field.status}",
        "data_source": source.name if source else None,
    }


def build_snapshot_lineage(db: Session, resolved: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the frozen lineage graph embedded in every snapshot."""
    nodes: list[dict[str, Any]] = []
    sources_seen: set[int] = set()

    for item in resolved["metrics"]:
        metric, mv = item["metric"], item["mv"]
        field_refs = [_field_ref(f) for f in item["fields"].values()]
        for f in item["fields"].values():
            if f.source_id not in sources_seen:
                sources_seen.add(f.source_id)
        nodes.append({
            "kind": "metric_version",
            "id": mv.id,
            "metric_key": metric.key,
            "metric_name": metric.name,
            "version_label": f"v{mv.version}",
            "status": mv.status,
            "expression": mv.expression,
            "fields": field_refs,
        })

    for item in resolved["dimensions"]:
        dim, field = item["dim"], item["field"]
        nodes.append({
            "kind": "dimension",
            "id": dim.id,
            "key": dim.key,
            "name": dim.name,
            "status": dim.status,
            "field": _field_ref(field),
        })

    for source_id in sources_seen:
        ds = db.get(DataSource, source_id)
        if ds:
            nodes.append({
                "kind": "data_source",
                "id": ds.id,
                "name": ds.name,
                "table": ds.table_name,
                "version": f"ds{ds.id}",
            })

    scope = resolved["scope"]
    if scope is not None:
        nodes.append({
            "kind": "scope",
            "id": scope.id,
            "key": scope.key,
            "name": scope.name,
            "status": scope.status,
            "version": f"scope{scope.id}@{scope.status}",
        })
    return nodes


def persist_snapshot_edges(db: Session, snapshot_id: int, lineage: list[dict[str, Any]]) -> None:
    """Mirror the lineage into the relational LineageEdge table for SQL-level queries."""
    for node in lineage:
        if node["kind"] == "metric_version":
            db.add(LineageEdge(
                from_kind="report_snapshot", from_version_id=snapshot_id,
                to_kind="metric_version", to_id=node["id"],
                to_version=node["version_label"], label=node["metric_key"],
            ))
            for fref in node["fields"]:
                db.add(LineageEdge(
                    from_kind="report_snapshot", from_version_id=snapshot_id,
                    to_kind="field", to_id=fref["id"],
                    to_version=fref["version"], label=f"{node['metric_key']}->{fref['name']}",
                ))
        elif node["kind"] == "dimension":
            db.add(LineageEdge(
                from_kind="report_snapshot", from_version_id=snapshot_id,
                to_kind="dimension", to_id=node["id"],
                to_version=f"dim{node['id']}@{node['status']}", label=node["key"],
            ))
            db.add(LineageEdge(
                from_kind="report_snapshot", from_version_id=snapshot_id,
                to_kind="field", to_id=node["field"]["id"],
                to_version=node["field"]["version"], label=f"{node['key']}->{node['field']['name']}",
            ))
        elif node["kind"] == "data_source":
            db.add(LineageEdge(
                from_kind="report_snapshot", from_version_id=snapshot_id,
                to_kind="data_source", to_id=node["id"], to_version=node["version"],
                label=node["table"],
            ))
        elif node["kind"] == "scope":
            db.add(LineageEdge(
                from_kind="report_snapshot", from_version_id=snapshot_id,
                to_kind="scope", to_id=node["id"], to_version=node["version"],
                label=node["key"],
            ))


def persist_metric_edges(db: Session, mv: MetricVersion, fields: dict[str, Field]) -> None:
    existed = db.query(LineageEdge).filter(
        LineageEdge.from_kind == "metric_version",
        LineageEdge.from_version_id == mv.id,
    ).count()
    if existed:
        return
    for field in fields.values():
        db.add(LineageEdge(
            from_kind="metric_version", from_version_id=mv.id,
            to_kind="field", to_id=field.id,
            to_version=f"f{field.id}@{field.status}", label=field.name,
        ))


# ------------------------------------------------------------- impact scan ---

def analyze_impact(db: Session, definition: ReportDefIn) -> dict[str, Any]:
    """Compare a saved (pinned) definition against the *current* catalog.

    Returns health (ok/impacted/broken), concrete issues and available version
    switches. Runs without raising so the UI can always render the report's
    health; the execution path raises BrokenSnapshot separately.
    """
    issues: list[dict[str, Any]] = []
    switch_options: list[dict[str, Any]] = []
    known_fields = all_fields_map(db)

    for ref in definition.metrics:
        metric = db.query(Metric).filter(Metric.key == ref.metric_key).first()
        if metric is None:
            issues.append({"severity": "broken", "type": "metric_missing",
                           "message": f"指标 {ref.metric_key} 已不存在"})
            continue
        mv = db.get(MetricVersion, ref.metric_version_id) if ref.metric_version_id else None
        if mv is None or mv.metric_id != metric.id:
            issues.append({"severity": "broken", "type": "metric_version_missing",
                           "message": f"指标 {metric.key} 固定的版本已不存在"})
            continue
        if mv.status == "deprecated":
            issues.append({"severity": "impacted", "type": "metric_deprecated",
                           "metric_key": metric.key, "metric_version_id": mv.id,
                           "message": f"指标 {metric.name} v{mv.version} 已弃用"})
        for token in _IDENT_RE.findall(mv.expression):
            if token.lower() in _SQL_WORDS or token not in known_fields:
                continue
            field = known_fields[token]
            if field.status == "retired":
                issues.append({"severity": "broken", "type": "field_retired",
                               "field": field.name, "metric_key": metric.key,
                               "message": f"字段 {field.name} 已下线，{metric.key} v{mv.version} 无法执行"})
            elif field.status == "deprecated":
                issues.append({"severity": "impacted", "type": "field_deprecated",
                               "field": field.name, "metric_key": metric.key,
                               "message": f"字段 {field.name} 已弃用（{field.sunset_note or '计划下线'}）"})
        options = [
            {"id": v.id, "version": v.version, "status": v.status,
             "expression": v.expression, "change_note": v.change_note}
            for v in metric.versions if v.id != mv.id and v.status != "draft"
        ]
        if options:
            switch_options.append({"metric_key": metric.key, "current_version_id": mv.id,
                                   "current_version": mv.version, "options": options})

    for key in definition.dimensions:
        dim = db.query(Dimension).filter(Dimension.key == key).first()
        if dim is None:
            issues.append({"severity": "broken", "type": "dimension_missing",
                           "message": f"维度 {key} 已不存在"})
            continue
        field = db.get(Field, dim.field_id)
        if dim.status == "retired" or field.status == "retired":
            issues.append({"severity": "broken", "type": "dimension_retired",
                           "dimension": key, "message": f"维度 {dim.name} 已下线"})
        elif dim.status == "deprecated" or field.status == "deprecated":
            issues.append({"severity": "impacted", "type": "dimension_deprecated",
                           "dimension": key, "message": f"维度 {dim.name} 已弃用"})

    for spec in definition.filters:
        field = known_fields.get(spec.field)
        if field is None:
            issues.append({"severity": "broken", "type": "filter_field_missing",
                           "message": f"筛选字段 {spec.field} 不存在"})
        elif field.status == "retired":
            issues.append({"severity": "broken", "type": "filter_field_retired",
                           "field": field.name, "message": f"筛选字段 {field.name} 已下线"})
        elif field.status == "deprecated":
            issues.append({"severity": "impacted", "type": "filter_field_deprecated",
                           "field": field.name, "message": f"筛选字段 {field.name} 已弃用"})

    if definition.scope_key:
        scope = db.query(Scope).filter(Scope.key == definition.scope_key).first()
        if scope is None:
            issues.append({"severity": "broken", "type": "scope_missing",
                           "message": f"权限范围 {definition.scope_key} 已不存在"})
        elif scope.status == "revoked":
            issues.append({"severity": "broken", "type": "scope_revoked",
                           "scope": scope.key, "message": f"权限范围 {scope.name} 已被撤销"})

    if any(i["severity"] == "broken" for i in issues):
        health = "broken"
    elif issues:
        health = "impacted"
    else:
        health = "ok"
    return {"health": health, "issues": issues, "switch_options": switch_options}


def assert_executable(impact: dict[str, Any]) -> None:
    if impact["health"] == "broken":
        messages = "; ".join(i["message"] for i in impact["issues"] if i["severity"] == "broken")
        raise BrokenSnapshot(f"报表快照已失效：{messages}")
