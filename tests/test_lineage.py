"""Lineage: every snapshot carries full version-pinned upstream graph."""


def test_lineage_contains_metric_version_fields_source_and_scope(client, h, basic_def):
    r = client.post("/api/reports", headers=h("alice"),
                    json={"key": "t_lin_1", "title": "血缘", "definition": basic_def})
    snap_id = r.json()["current_snapshot_id"]
    data = client.get("/api/reports/t_lin_1/lineage", headers=h("alice")).json()

    kinds = {n["kind"] for n in data["nodes"]}
    assert {"metric_version", "dimension", "data_source", "scope"} <= kinds

    mv = next(n for n in data["nodes"] if n["kind"] == "metric_version")
    assert mv["version_label"].startswith("v")
    assert mv["fields"][0]["physical_column"] == "amount_usd"
    assert mv["fields"][0]["version"].startswith("f")

    ds = next(n for n in data["nodes"] if n["kind"] == "data_source")
    assert ds["table"] == "fact_sales"

    # same graph is embedded in the snapshot payload itself
    snap = client.get("/api/reports/t_lin_1/snapshots", headers=h("alice")).json()[0]
    assert {n["kind"] for n in snap["lineage"]} == kinds
    assert snap_id == snap["id"]


def test_lineage_records_exact_pinned_versions_after_switch(client, h):
    client.post("/api/reports", headers=h("alice"), json={
        "key": "t_lin_2", "title": "血缘切换",
        "definition": {"metrics": [{"metric_key": "revenue"}],
                       "dimensions": ["by_region"], "filters": [], "scope_key": "scope_full"}})
    metrics = client.get("/api/catalog/metrics").json()
    rev = next(m for m in metrics if m["key"] == "revenue")
    v1 = next(v for v in rev["versions"] if v["version"] == 1)["id"]
    report = client.get("/api/reports/t_lin_2", headers=h("alice")).json()
    client.post("/api/reports/t_lin_2/switch-version", headers=h("alice"), json={
        "metric_key": "revenue", "target_version_id": v1,
        "expected_revision": report["revision"]})

    snaps = client.get("/api/reports/t_lin_2/snapshots", headers=h("alice")).json()
    v2_node = next(n for n in snaps[0]["lineage"] if n["kind"] == "metric_version")
    v1_node = next(n for n in snaps[1]["lineage"] if n["kind"] == "metric_version")
    assert v2_node["version_label"] == "v2"
    assert v1_node["version_label"] == "v1"
    assert v1_node["status"] == "deprecated"


def test_relational_lineage_edges_are_written(client, h, basic_def):
    from app.db import SessionLocal
    from app.models import LineageEdge

    client.post("/api/reports", headers=h("alice"),
                json={"key": "t_lin_3", "title": "边", "definition": basic_def})
    db = SessionLocal()
    try:
        edges = db.query(LineageEdge).filter(LineageEdge.from_kind == "report_snapshot").all()
        targets = {e.to_kind for e in edges}
        assert {"metric_version", "field", "dimension", "data_source", "scope"} <= targets
    finally:
        db.close()
