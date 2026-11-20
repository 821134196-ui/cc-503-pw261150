"""Idempotent demo seed: catalog, fact data, permission scopes and users.

Run once after tables are created::

    python -m app.seed
"""
from __future__ import annotations

import json

from sqlalchemy import text

from .db import Base, SessionLocal, engine
from .models import (
    DataSource,
    Dimension,
    Field,
    Metric,
    MetricVersion,
    Scope,
    User,
    UserScope,
)
from .services import catalog as catalog_svc

FACT_ROWS = [
    # order_id, region, channel, amount_usd, cost_usd, is_membership, order_date
    (1001, "华东", "线上", 1200.0, 700.0, 1, "2026-10-01"),
    (1002, "华东", "门店", 860.0, 520.0, 0, "2026-10-02"),
    (1003, "华北", "线上", 2300.0, 1500.0, 1, "2026-10-03"),
    (1004, "华南", "线上", 450.0, 300.0, 0, "2026-10-04"),
    (1005, "华北", "门店", 1750.0, 1100.0, 1, "2026-10-05"),
    (1006, "华南", "门店", 640.0, 410.0, 0, "2026-10-06"),
    (1007, "华东", "线上", 990.0, 610.0, 0, "2026-10-07"),
    (1008, "华北", "线上", 3100.0, 2050.0, 1, "2026-10-08"),
    (1009, "华南", "线上", 720.0, 480.0, 0, "2026-10-09"),
    (1010, "华东", "门店", 1380.0, 830.0, 1, "2026-10-10"),
    (1011, "华北", "门店", 560.0, 360.0, 0, "2026-10-11"),
    (1012, "华南", "线上", 1890.0, 1180.0, 1, "2026-10-12"),
]


def seed() -> None:
    # Cyclic FK (reports.current_snapshot_id -> report_snapshots.report_id)
    # makes DROP ordering ambiguous; disable enforcement for the reseed.
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        Base.metadata.drop_all(bind=conn)
        Base.metadata.create_all(bind=conn)
        conn.execute(text("PRAGMA foreign_keys=ON"))
    db = SessionLocal()
    try:
        # ---------------------------------------------------------- fact table
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS fact_sales (
                order_id INTEGER PRIMARY KEY,
                region TEXT, channel TEXT, amount_usd FLOAT, cost_usd FLOAT,
                is_membership INTEGER, order_date TEXT
            )
        """))
        db.execute(text("DELETE FROM fact_sales"))
        db.execute(
            text(
                "INSERT INTO fact_sales (order_id, region, channel, amount_usd, "
                "cost_usd, is_membership, order_date) VALUES "
                + ", ".join(f"(:o{i},:r{i},:c{i},:a{i},:k{i},:m{i},:d{i})" for i in range(len(FACT_ROWS)))
            ),
            {k: v for i, row in enumerate(FACT_ROWS)
             for k, v in zip((f"o{i}", f"r{i}", f"c{i}", f"a{i}", f"k{i}", f"m{i}", f"d{i}"), row)},
        )

        ds = DataSource(name="销售明细数据源", table_name="fact_sales",
                        description="订单粒度的销售事实表")
        db.add(ds)
        db.flush()

        def field(name, col, dtype, status="active", note=""):
            f = Field(source_id=ds.id, name=name, column_name=col, data_type=dtype,
                      status=status, sunset_note=note)
            db.add(f)
            return f

        f_region = field("region", "region", "text")
        f_channel = field("channel", "channel", "text")
        f_amount = field("amount_usd", "amount_usd", "number")
        f_cost = field("cost_usd", "cost_usd", "number", status="deprecated",
                       note="成本口径迁移中，计划 2026-12-31 下线，请改用 cost_usd_v2")
        f_membership = field("is_membership", "is_membership", "integer")
        f_date = field("order_date", "order_date", "date")
        # A retired field: still present physically, but catalog says dead.
        f_legacy = field("legacy_discount", "legacy_discount", "number",
                         status="retired", note="旧折扣字段已于 2026-06 下线")
        db.flush()

        # ------------------------------------------------------------- metrics
        m_rev = Metric(key="revenue", name="营收", description="订单金额合计")
        m_aov = Metric(key="avg_order_value", name="客单价", description="平均每单金额")
        m_margin = Metric(key="gross_margin", name="毛利额", description="营收-成本（含口径迁移）")
        db.add_all([m_rev, m_aov, m_margin])
        db.flush()

        rev_v1 = MetricVersion(metric_id=m_rev.id, version=1, status="deprecated",
                               expression="SUM(amount_usd)", change_note="初始口径（含税）")
        rev_v2 = MetricVersion(metric_id=m_rev.id, version=2, status="active",
                               expression="SUM(amount_usd)",
                               change_note="口径文档对齐，计算不变；血缘元数据升级")
        aov_v1 = MetricVersion(metric_id=m_aov.id, version=1, status="active",
                               expression="AVG(amount_usd)", change_note="初始版本")
        margin_v1 = MetricVersion(metric_id=m_margin.id, version=1, status="deprecated",
                                  expression="SUM(amount_usd) - SUM(cost_usd)",
                                  change_note="使用旧成本字段 cost_usd")
        margin_v2 = MetricVersion(metric_id=m_margin.id, version=2, status="active",
                                  expression="SUM(amount_usd) - SUM(cost_usd)",
                                  change_note="迁移到新成本口径（本演示沿用同名列）")
        db.add_all([rev_v1, rev_v2, aov_v1, margin_v1, margin_v2])
        db.flush()
        for mv in (rev_v1, rev_v2, aov_v1, margin_v1, margin_v2):
            catalog_svc.persist_metric_edges(
                db, mv, catalog_svc.expression_fields(mv.expression, catalog_svc.all_fields_map(db))
            )

        # ---------------------------------------------------------- dimensions
        db.add_all([
            Dimension(key="by_region", name="区域", field_id=f_region.id),
            Dimension(key="by_channel", name="渠道", field_id=f_channel.id),
            Dimension(key="by_membership", name="是否会员", field_id=f_membership.id),
            Dimension(key="by_date", name="下单日期", field_id=f_date.id),
        ])

        # -------------------------------------------------------------- scopes
        full = Scope(key="scope_full", name="全量数据范围",
                     row_filters="[]", column_allow="null")
        east = Scope(key="scope_east", name="华东区域范围",
                     row_filters=json.dumps([{"field": "region", "op": "eq", "value": "华东"}]),
                     column_allow="null")
        masked = Scope(
            key="scope_masked", name="脱敏范围（不可见成本）",
            row_filters="[]",
            column_allow=json.dumps(
                ["region", "channel", "amount_usd", "is_membership", "order_date"]
            ),
        )
        demo_revoked = Scope(key="scope_demo_revoked", name="演示用-已撤销范围",
                             row_filters="[]", column_allow="null", status="revoked")
        db.add_all([full, east, masked, demo_revoked])
        db.flush()

        # --------------------------------------------------------------- users
        alice = User(username="alice", display_name="Alice（全量分析师）", role="analyst")
        bob = User(username="bob", display_name="Bob（华东分析师）", role="analyst")
        carol = User(username="carol", display_name="Carol（受限分析师）", role="analyst")
        admin = User(username="admin", display_name="管理员", role="admin")
        db.add_all([alice, bob, carol, admin])
        db.flush()
        db.add_all([
            UserScope(user_id=alice.id, scope_id=full.id),
            UserScope(user_id=alice.id, scope_id=east.id),
            UserScope(user_id=bob.id, scope_id=east.id),
            UserScope(user_id=carol.id, scope_id=masked.id),
            UserScope(user_id=admin.id, scope_id=full.id),
            # Carol used to hold this scope; it is now revoked — demonstrates
            # a saved report breaking when access is pulled.
            UserScope(user_id=carol.id, scope_id=demo_revoked.id),
        ])

        db.commit()
        print("seed complete: 1 data source, 7 fields, 3 metrics / 5 versions,",
              "4 dimensions, 4 scopes, 4 users,", len(FACT_ROWS), "fact rows")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
