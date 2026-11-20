"""Build/save path: validation, snapshot pinning, immutable snapshot history."""


def test_create_report_pins_concrete_metric_version(client, h, basic_def):
    r = client.post("/api/reports", headers=h("alice"),
                    json={"key": "t_build_1", "title": "构建测试", "definition": basic_def})
    assert r.status_code == 201
    body = r.json()
    assert body["revision"] == 1
    assert body["health"] == "ok"

    snaps = client.get("/api/reports/t_build_1/snapshots", headers=h("alice")).json()
    assert len(snaps) == 1
    pinned = snaps[0]["definition"]["definition"]["metrics"]
    assert pinned[0]["metric_version_id"] is not None  # concrete version, never null


def test_cannot_save_report_with_unknown_metric(client, h, basic_def):
    basic_def["metrics"] = [{"metric_key": "does_not_exist"}]
    r = client.post("/api/reports", headers=h("alice"),
                    json={"key": "t_build_2", "title": "x", "definition": basic_def})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_cannot_save_with_scope_not_granted(client, h, basic_def):
    basic_def["scope_key"] = "scope_full"  # bob only holds scope_east
    r = client.post("/api/reports", headers=h("bob"),
                    json={"key": "t_build_3", "title": "x", "definition": basic_def})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "permission_denied"


def test_each_save_appends_immutable_snapshot(client, h, basic_def):
    client.post("/api/reports", headers=h("alice"),
                json={"key": "t_build_4", "title": "v1", "definition": basic_def})
    r = client.put("/api/reports/t_build_4", headers=h("alice"), json={
        "title": "v2",
        "definition": {**basic_def, "dimensions": ["by_region", "by_channel"]},
        "expected_revision": 1,
    })
    assert r.status_code == 200
    assert r.json()["revision"] == 2
    snaps = client.get("/api/reports/t_build_4/snapshots", headers=h("alice")).json()
    assert [s["revision"] for s in snaps] == [1, 2]
    # snapshot #1 keeps its original single-dimension definition
    assert snaps[0]["definition"]["definition"]["dimensions"] == ["by_region"]


def test_saved_report_using_deprecated_metric_is_impacted(client, h):
    # revenue v1 is deprecated in the seed
    metrics = client.get("/api/catalog/metrics", headers=h("alice")).json()
    rev = next(m for m in metrics if m["key"] == "revenue")
    v1 = next(v for v in rev["versions"] if v["version"] == 1)["id"]
    r = client.post("/api/reports", headers=h("alice"), json={
        "key": "t_build_5", "title": "旧版本报表",
        "definition": {"metrics": [{"metric_key": "revenue", "metric_version_id": v1}],
                       "dimensions": ["by_region"], "filters": [], "scope_key": "scope_full"}})
    assert r.status_code == 201
    assert r.json()["health"] == "impacted"
    impact = client.get("/api/reports/t_build_5/impact", headers=h("alice")).json()
    assert any(i["type"] == "metric_deprecated" for i in impact["issues"])
