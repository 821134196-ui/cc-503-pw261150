"""Metric re-releases and field sunsets: impact flags, blocking, version switch."""


def _make_margin_report(client, h, key="t_ver_1"):
    return client.post("/api/reports", headers=h("alice"), json={
        "key": key, "title": "毛利报表",
        "definition": {
            "metrics": [{"metric_key": "gross_margin"}],
            "dimensions": ["by_region"], "filters": [], "scope_key": "scope_full",
        },
    })


def test_deprecated_field_marks_report_impacted(client, h):
    # gross_margin v2 depends on cost_usd which is deprecated in the seed
    r = _make_margin_report(client, h)
    assert r.json()["health"] == "impacted"
    impact = client.get("/api/reports/t_ver_1/impact", headers=h("alice")).json()
    assert any(i["type"] == "field_deprecated" and i["field"] == "cost_usd"
               for i in impact["issues"])
    # impacted is still executable, with a warning
    snap = r.json()["current_snapshot_id"]
    preview = client.post("/api/reports/preview", headers=h("alice"),
                          json={"snapshot_id": snap}).json()
    assert any("cost_usd" in w for w in preview["warnings"])


def test_new_metric_version_does_not_change_saved_report(client, h):
    r = _make_margin_report(client, h, key="t_ver_2")
    snap_before = r.json()["current_snapshot_id"]
    pinned_before = client.get("/api/reports/t_ver_2/snapshots", headers=h("alice")).json()
    mv_before = pinned_before[0]["lineage"]
    mv_id_before = next(n for n in mv_before if n["kind"] == "metric_version")["id"]

    # admin publishes v3 — the saved snapshot must keep pointing at v2
    pub = client.post("/api/admin/metrics/gross_margin/versions", headers=h("admin"), json={
        "expression": "SUM(amount_usd) - SUM(cost_usd)",
        "change_note": "v3 演示发布", "deprecate_previous": True,
    })
    assert pub.status_code == 200
    report = client.get("/api/reports/t_ver_2", headers=h("alice")).json()
    assert report["current_snapshot_id"] == snap_before
    pinned_after = client.get("/api/reports/t_ver_2/snapshots", headers=h("alice")).json()
    mv_id_after = next(n for n in pinned_after[0]["lineage"]
                       if n["kind"] == "metric_version")["id"]
    assert mv_id_after == mv_id_before
    # impact scan now offers a switch to the new version
    impact = client.get("/api/reports/t_ver_2/impact", headers=h("alice")).json()
    opts = next(g for g in impact["switch_options"] if g["metric_key"] == "gross_margin")
    assert any(o["version"] == 3 for o in opts["options"])


def test_switch_version_creates_new_snapshot_and_clears_impact(client, h):
    _make_margin_report(client, h, key="t_ver_3")
    pub = client.post("/api/admin/metrics/gross_margin/versions", headers=h("admin"), json={
        "expression": "SUM(amount_usd) - SUM(cost_usd)",
        "change_note": "v3", "deprecate_previous": True,
    }).json()
    report = client.get("/api/reports/t_ver_3", headers=h("alice")).json()
    r = client.post("/api/reports/t_ver_3/switch-version", headers=h("alice"), json={
        "metric_key": "gross_margin",
        "target_version_id": pub["new_version_id"],
        "expected_revision": report["revision"],
    })
    assert r.status_code == 200
    assert r.json()["revision"] == 2
    snaps = client.get("/api/reports/t_ver_3/snapshots", headers=h("alice")).json()
    assert len(snaps) == 2
    current = next(n for n in snaps[-1]["lineage"] if n["kind"] == "metric_version")
    assert current["version_label"] == "v3"


def test_retired_field_breaks_snapshot_and_blocks_execution(client, h):
    r = _make_margin_report(client, h, key="t_ver_4")
    snap = r.json()["current_snapshot_id"]

    # sunset the field the metric depends on
    res = client.post("/api/admin/fields/cost_usd/status", headers=h("admin"),
                      json={"status": "retired", "sunset_note": "已到下线日期"})
    assert res.status_code == 200

    impact = client.get("/api/reports/t_ver_4/impact", headers=h("alice")).json()
    assert impact["health"] == "broken"
    assert any(i["type"] == "field_retired" for i in impact["issues"])

    # execution must be refused rather than returning wrong data
    blocked = client.post("/api/reports/preview", headers=h("alice"),
                          json={"snapshot_id": snap})
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "broken_snapshot"

    # and a brand-new build using the dead field is rejected at save time too
    blocked_save = client.post("/api/reports", headers=h("alice"), json={
        "key": "t_ver_4b", "title": "x",
        "definition": {"metrics": [{"metric_key": "gross_margin"}],
                       "dimensions": [], "filters": [], "scope_key": "scope_full"}})
    assert blocked_save.status_code == 422


def test_retired_filter_field_is_flagged(client, h, basic_def):
    client.post("/api/reports", headers=h("alice"),
                json={"key": "t_ver_5", "title": "筛选下线",
                      "definition": {**basic_def,
                                     "filters": [{"field": "cost_usd", "op": "gt", "value": 0}]}})
    client.post("/api/admin/fields/cost_usd/status", headers=h("admin"),
                json={"status": "retired"})
    impact = client.get("/api/reports/t_ver_5/impact", headers=h("alice")).json()
    assert impact["health"] == "broken"
    assert any(i["type"] == "filter_field_retired" for i in impact["issues"])
