"""Report execution engine.

Invariant: preview and export call the same :func:`execute_resolved` /
:func:`execute_snapshot` entry points and therefore cannot diverge — an export
freezes the exact rows (and their checksum) returned by the preview path.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..errors import PermissionDenied
from ..models import DataSource, Field, ReportSnapshot
from ..schemas import ReportDefIn
from . import catalog
from .permissions import EffectiveScope, resolve_effective_scope

_SQL_OP = {"eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
_REDACTED = "🔒 无权限"


# ------------------------------------------------------------- SQL building ---

def _where_clause(conds: list[dict[str, Any]], fields: dict[str, Field]) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    for i, cond in enumerate(conds):
        fname, op, value = cond["field"], cond["op"], cond["value"]
        field = fields[fname]
        col = field.column_name
        base = f"f{i}"
        if op == "in":
            if not isinstance(value, list) or not value:
                raise ValueError("'in' filter requires a non-empty value list")
            placeholders = ", ".join(f":{base}_{j}" for j in range(len(value)))
            clauses.append(f'"{col}" IN ({placeholders})')
            for j, v in enumerate(value):
                params[f"{base}_{j}"] = v
        else:
            clauses.append(f'"{col}" {_SQL_OP[op]} :{base}')
            params[base] = value
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _build_sql(resolved: dict[str, Any], eff: EffectiveScope) -> tuple[str, dict[str, Any], str]:
    tables = set()
    fields = resolved["known_fields"]
    for item in resolved["metrics"]:
        for f in item["fields"].values():
            tables.add(f.source.table_name)
    for item in resolved["dimensions"]:
        tables.add(item["field"].source.table_name)
    for item in resolved["filters"]:
        tables.add(item["field"].source.table_name)
    for rf in eff.row_filters:
        if rf["field"] not in fields:
            raise PermissionDenied(f"行级权限引用了不存在的字段 {rf['field']}")
        tables.add(fields[rf["field"]].source.table_name)
    if len(tables) != 1:
        raise PermissionDenied(f"当前引擎仅支持单表查询，报表涉及: {sorted(tables)}")
    table = next(iter(tables))

    select_parts: list[str] = []
    aliases: list[str] = []

    for item in resolved["dimensions"]:
        col = item["field"].column_name
        alias = f"dim_{item['dim'].key}"
        select_parts.append(f'"{col}" AS "{alias}"')
        aliases.append(alias)

    metric_aliases: list[str] = []
    for idx, item in enumerate(resolved["metrics"]):
        mv = item["mv"]
        alias = f"metric_{idx}_{item['metric'].key}"
        # Expression references logical field names; translate to physical columns.
        expr = mv.expression
        for fname, f in item["fields"].items():
            expr = expr.replace(fname, f'"{f.column_name}"')
        select_parts.append(f"{expr} AS \"{alias}\"")
        metric_aliases.append(alias)

    group_by = ""
    if resolved["dimensions"]:
        group_cols = ", ".join(f'"{item["field"].column_name}"' for item in resolved["dimensions"])
        group_by = f" GROUP BY {group_cols}"

    conds = [{"field": c["spec"].field, "op": c["spec"].op, "value": c["spec"].value}
             for c in resolved["filters"]]
    # Scope row filters are appended last; they can only narrow the result.
    conds.extend(eff.row_filters)
    where, params = _where_clause(conds, fields)

    sql = f"SELECT {', '.join(select_parts)} FROM \"{table}\"{where}{group_by}"
    return sql, params, table


# ------------------------------------------------------------ redaction ------

def _redacted_columns(resolved: dict[str, Any], eff: EffectiveScope) -> tuple[set[str], list[str]]:
    """Return output aliases that must be redacted plus human warnings."""
    if eff.column_allow is None:
        return set(), []
    blocked: set[str] = set()
    warnings: list[str] = []
    for item in resolved["dimensions"]:
        logical = item["field"].name
        if logical not in eff.column_allow:
            blocked.add(f"dim_{item['dim'].key}")
            warnings.append(f"列级权限：维度 {item['dim'].name}（{logical}）已脱敏")
    for idx, item in enumerate(resolved["metrics"]):
        alias = f"metric_{idx}_{item['metric'].key}"
        touched = set(item["fields"].keys())
        if touched - eff.column_allow:
            blocked.add(alias)
            warnings.append(
                f"列级权限：指标 {item['metric'].name} 依赖受限字段 "
                f"{sorted(touched - eff.column_allow)}，结果已脱敏"
            )
    return blocked, warnings


def _output_columns(resolved: dict[str, Any]) -> list[str]:
    cols = [f"维度·{item['dim'].name}" for item in resolved["dimensions"]]
    cols += [f"指标·{item['metric'].name} (v{item['mv'].version})" for item in resolved["metrics"]]
    return cols


# -------------------------------------------------------------- execution -----

def execute_resolved(
    db: Session,
    resolved: dict[str, Any],
    eff: EffectiveScope,
    *,
    basis: str,
) -> dict[str, Any]:
    sql, params, table = _build_sql(resolved, eff)
    result = db.execute(text(sql), params)
    raw_rows = [list(r) for r in result.all()]

    aliases = [f"dim_{d['dim'].key}" for d in resolved["dimensions"]]
    aliases += [f"metric_{i}_{m['metric'].key}" for i, m in enumerate(resolved["metrics"])]
    blocked, redact_warnings = _redacted_columns(resolved, eff)

    rows: list[list[Any]] = []
    for raw in raw_rows:
        row = []
        for alias, value in zip(aliases, raw):
            if isinstance(value, float):
                value = round(value, 4)
            row.append(_REDACTED if alias in blocked else value)
        rows.append(row)

    columns = _output_columns(resolved)
    warnings = list(resolved.get("warnings", [])) + redact_warnings
    checksum = checksum_payload(columns, rows)
    return {
        "columns": columns,
        "rows": rows,
        "warnings": warnings,
        "checksum": checksum,
        "basis": basis,
        "table": table,
        "sql": sql,
    }


def execute_adhoc(db: Session, definition: ReportDefIn, user, *, audit_scope_key: Optional[str] = None) -> dict[str, Any]:
    """Preview path for an unsaved definition (builder canvas)."""
    resolved = catalog.resolve_definition(db, definition)
    eff = resolve_effective_scope(db, user, definition.scope_key)
    result = execute_resolved(db, resolved, eff, basis="ad-hoc")
    result["lineage"] = catalog.build_snapshot_lineage(db, resolved)
    return result


def execute_snapshot(db: Session, snapshot: ReportSnapshot, user) -> dict[str, Any]:
    """Execution path for a saved snapshot.

    The snapshot definition is re-resolved against the *current* catalog so
    sunset/retirement and live scope revocation are enforced; the snapshot's
    frozen lineage is still returned for display. Re-resolution of metric
    versions is pinned by id, so a new metric version never silently changes
    an existing report.
    """
    payload = json.loads(snapshot.definition)
    definition = ReportDefIn(**payload["definition"])
    impact = catalog.analyze_impact(db, definition)

    # Enforce live permissions first: a revoked scope / missing grant is a
    # 403 for the caller even when the snapshot is also broken for other
    # reasons; the impact endpoint still surfaces the full broken/impact list.
    eff = resolve_effective_scope(db, user, definition.scope_key)
    catalog.assert_executable(impact)

    resolved = catalog.resolve_definition(db, definition, allow_retired=True)
    result = execute_resolved(
        db, resolved, eff, basis=f"snapshot:{snapshot.id}#rev{snapshot.revision}"
    )
    result["lineage"] = json.loads(snapshot.lineage)
    result["impact"] = impact
    result["warnings"] = result["warnings"] + [
        i["message"] for i in impact["issues"] if i["severity"] == "impacted"
    ]
    return result


# ------------------------------------------------------------- serialization ---

def to_csv(columns: list[str], rows: list[list[Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    writer.writerows(rows)
    return buf.getvalue()


def checksum_payload(columns: list[str], rows: list[list[Any]]) -> str:
    """Stable sha256 over the canonical (columns, rows) payload.

    Cells are stringified exactly the way the CSV serializer writes them, so
    the checksum computed from a JSON preview equals the one recomputed from
    the exported CSV text — page and file are provably the same data.
    """
    norm_rows = [[("" if v is None else str(v)) for v in row] for row in rows]
    canonical = json.dumps({"columns": columns, "rows": norm_rows},
                           ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def checksum_csv(csv_text: str) -> str:
    return hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
