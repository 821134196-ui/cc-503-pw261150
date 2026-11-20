"""Export/page consistency and audit trail."""


def _report(client, h, key="t_exp_1"):
    r = client.post("/api/reports", headers=h("alice"), json={
        "key": key, "title": "导出测试",
        "definition": {"metrics": [{"metric_key": "revenue"},
                                   {"metric_key": "avg_order_value"}],
                       "dimensions": ["by_region", "by_channel"],
                       "filters": [], "scope_key": "scope_full"}})
    return r.json()


def test_export_file_matches_preview_rows_and_checksum(client, h):
    report = _report(client, h)
    snap_id = report["current_snapshot_id"]

    preview = client.post("/api/reports/preview", headers=h("alice"),
                          json={"snapshot_id": snap_id}).json()
    export = client.post(f"/api/reports/{report['key']}/exports",
                         headers=h("alice")).json()
    download = client.get(
        f"/api/reports/{report['key']}/exports/{export['export_id']}/download",
        headers=h("alice"))

    assert download.status_code == 200
    assert download.headers["X-Export-Checksum"] == export["checksum"]

    # parse the CSV and compare cell-by-cell with the preview payload
    import csv
    import io
    rows = list(csv.reader(io.StringIO(download.text)))
    csv_header, csv_body = rows[0], rows[1:]
    assert csv_header == preview["columns"]
    assert len(csv_body) == len(preview["rows"]) == export["row_count"]
    for csv_row, json_row in zip(csv_body, preview["rows"]):
        assert csv_row == [str(v) for v in json_row]

    verify = client.get(
        f"/api/reports/{report['key']}/exports/{export['export_id']}/verify",
        headers=h("alice")).json()
    assert verify["consistent"] is True
    assert verify["stored_checksum_matches"] is True
    assert verify["payload_checksum"] == preview["checksum"]


def test_export_is_frozen_and_repeatable(client, h):
    report = _report(client, h, key="t_exp_2")
    e1 = client.post(f"/api/reports/{report['key']}/exports", headers=h("alice")).json()
    # re-downloading the same export record returns the identical bytes
    d1 = client.get(
        f"/api/reports/{report['key']}/exports/{e1['export_id']}/download",
        headers=h("alice")).text
    d2 = client.get(
        f"/api/reports/{report['key']}/exports/{e1['export_id']}/download",
        headers=h("alice")).text
    assert d1 == d2


def test_snapshot_based_export_survives_catalog_change(client, h):
    """An export taken before a change keeps its frozen body; a new export
    after breaking the snapshot is refused (no page/file divergence)."""
    report = _report(client, h, key="t_exp_3")
    export = client.post(f"/api/reports/{report['key']}/exports",
                         headers=h("alice")).json()
    before = client.get(
        f"/api/reports/{report['key']}/exports/{export['export_id']}/download",
        headers=h("alice")).text

    client.post("/api/admin/fields/amount_usd/status", headers=h("admin"),
                json={"status": "retired", "sunset_note": "下线"})
    # frozen file still downloadable, byte-identical
    after = client.get(
        f"/api/reports/{report['key']}/exports/{export['export_id']}/download",
        headers=h("alice")).text
    assert after == before

    # but generating a NEW export is blocked — engine refuses broken snapshot
    blocked = client.post(f"/api/reports/{report['key']}/exports", headers=h("alice"))
    assert blocked.status_code == 409


def test_audit_log_records_lifecycle(client, h):
    report = _report(client, h, key="t_exp_4")
    export = client.post(f"/api/reports/{report['key']}/exports",
                         headers=h("alice")).json()
    client.post("/api/reports/preview", headers=h("alice"),
                json={"snapshot_id": report["current_snapshot_id"]})
    # download the file so the export.download audit event exists
    client.get(
        f"/api/reports/{report['key']}/exports/{export['export_id']}/download",
        headers=h("alice"))

    events = client.get("/api/admin/audit?limit=200", headers=h("alice")).json()
    actions = {e["action"] for e in events}
    assert {"report.create", "export.create", "export.download", "report.preview"} <= actions
    export_events = [e for e in events if e["action"] == "export.create"]
    assert export_events[0]["detail"]["checksum"]
    # denied access is audited too
    client.post("/api/reports/preview", headers=h("bob"), json={"definition": {
        "metrics": [{"metric_key": "revenue"}], "dimensions": [],
        "filters": [], "scope_key": "scope_masked"}})
    events2 = client.get("/api/admin/audit?action=report.preview&limit=20",
                         headers=h("alice")).json()
    # denied access is audited by the global exception handler
    client.post("/api/reports/preview", headers=h("bob"), json={"definition": {
        "metrics": [{"metric_key": "revenue"}], "dimensions": [],
        "filters": [], "scope_key": "scope_masked"}})
    denied = client.get("/api/admin/audit?action=access.denied&limit=20",
                        headers=h("alice")).json()
    assert len(denied) >= 1
    assert denied[0]["result"] == "denied"
    assert "scope_masked" in denied[0]["detail"]["reason"]


def test_revision_conflict_is_audited(client, h, basic_def):
    client.post("/api/reports", headers=h("alice"),
                json={"key": "t_exp_5", "title": "冲突审计", "definition": basic_def})
    stale = {"definition": basic_def, "expected_revision": 1}
    client.put("/api/reports/t_exp_5", headers=h("alice"), json=stale)
    client.put("/api/reports/t_exp_5", headers=h("alice"), json=stale)
    conflicts = client.get("/api/admin/audit?limit=50", headers=h("alice")).json()
    conflict_events = [e for e in conflicts if e["result"] == "conflict"]
    assert len(conflict_events) == 1
    assert conflict_events[0]["action"] == "report.save"


def test_revoke_is_audited(client, h):
    client.post("/api/admin/scopes/scope_masked/revoke", headers=h("admin"))
    events = client.get("/api/admin/audit?action=scope.revoke", headers=h("alice")).json()
    assert len(events) >= 1
    assert events[0]["target_id"] == "scope_masked"
