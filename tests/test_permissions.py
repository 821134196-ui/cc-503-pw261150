"""Permission scopes: row filters, column redaction, live revocation."""


def _sales_report(client, user, scope, key, dims=("by_region",)):
    return client.post("/api/reports", headers={"X-Username": user}, json={
        "key": key, "title": key,
        "definition": {"metrics": [{"metric_key": "revenue"}],
                       "dimensions": list(dims), "filters": [], "scope_key": scope},
    })


def test_row_scope_narrows_result_rows(client, h):
    # Same shape, different scope: scope_full sees all regions, scope_east only 华东
    full = client.post("/api/reports/preview", headers=h("alice"), json={"definition": {
        "metrics": [{"metric_key": "revenue"}],
        "dimensions": ["by_region"], "filters": [], "scope_key": "scope_full"}}).json()
    east = client.post("/api/reports/preview", headers=h("alice"), json={"definition": {
        "metrics": [{"metric_key": "revenue"}],
        "dimensions": ["by_region"], "filters": [], "scope_key": "scope_east"}}).json()
    assert len(full["rows"]) == 3
    assert len(east["rows"]) == 1
    assert east["rows"][0][0] == "华东"

    # bob only holds scope_east: he gets the narrowed result, and scope_full is denied
    bob = client.post("/api/reports/preview", headers=h("bob"), json={"definition": {
        "metrics": [{"metric_key": "revenue"}],
        "dimensions": ["by_region"], "filters": [], "scope_key": "scope_east"}}).json()
    assert bob["rows"] == east["rows"]


def test_column_scope_redacts_metric_depending_on_blocked_field(client, h):
    payload = {"definition": {
        "metrics": [{"metric_key": "revenue"}, {"metric_key": "gross_margin"}],
        "dimensions": ["by_region"], "filters": [], "scope_key": "scope_masked"}}
    carol = client.post("/api/reports/preview", headers=h("carol"), json=payload).json()
    for row in carol["rows"]:
        assert row[1] != "🔒 无权限"          # revenue (amount_usd allowed) visible
        assert row[2] == "🔒 无权限"          # margin touches cost_usd -> redacted
    assert any("脱敏" in w for w in carol["warnings"])


def test_user_cannot_preview_scope_they_do_not_hold(client, h):
    r = client.post("/api/reports/preview", headers=h("bob"), json={"definition": {
        "metrics": [{"metric_key": "revenue"}], "dimensions": [],
        "filters": [], "scope_key": "scope_masked"}})
    assert r.status_code == 403


def test_scope_revocation_takes_effect_on_existing_reports(client, h):
    # carol holds scope_demo_revoked (seeded already revoked) — saved report is broken
    r = client.post("/api/reports", headers=h("carol"), json={
        "key": "t_perm_rev1", "title": "将被撤销",
        "definition": {"metrics": [{"metric_key": "revenue"}], "dimensions": ["by_region"],
                       "filters": [], "scope_key": "scope_demo_revoked"}})
    assert r.status_code == 403  # already revoked at save time


def test_revoking_live_scope_blocks_previously_working_report(client, h):
    # bob builds a working report on scope_east, then admin revokes the scope
    r = _sales_report(client, "bob", "scope_east", "t_perm_rev2")
    assert r.status_code == 201
    snap = r.json()["current_snapshot_id"]
    ok = client.post("/api/reports/preview", headers=h("bob"),
                     json={"snapshot_id": snap})
    assert ok.status_code == 200

    rev = client.post("/api/admin/scopes/scope_east/revoke", headers=h("admin"))
    assert rev.status_code == 200

    impact = client.get("/api/reports/t_perm_rev2/impact", headers=h("bob")).json()
    assert impact["health"] == "broken"
    blocked = client.post("/api/reports/preview", headers=h("bob"),
                          json={"snapshot_id": snap})
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "permission_denied"

    # switching to a still-valid scope restores the report
    client.post("/api/admin/grants", headers=h("admin"),
                json={"username": "bob", "scope_key": "scope_full"})
    report = client.get("/api/reports/t_perm_rev2", headers=h("bob")).json()
    fixed = client.post("/api/reports/t_perm_rev2/switch-scope", headers=h("bob"), json={
        "scope_key": "scope_full", "expected_revision": report["revision"]})
    assert fixed.status_code == 200
    again = client.post("/api/reports/preview", headers=h("bob"),
                        json={"snapshot_id": fixed.json()["current_snapshot_id"]})
    assert again.status_code == 200
