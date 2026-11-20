"""Concurrent modification: optimistic locking on revision."""

import threading


def test_stale_update_gets_409(client, h, basic_def):
    client.post("/api/reports", headers=h("alice"),
                json={"key": "t_conc_1", "title": "并发", "definition": basic_def})
    stale = {
        "title": "stale",
        "definition": basic_def,
        "expected_revision": 1,
    }
    r1 = client.put("/api/reports/t_conc_1", headers=h("alice"), json=stale)
    r2 = client.put("/api/reports/t_conc_1", headers=h("alice"), json=stale)
    assert r1.status_code == 200
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "revision_conflict"


def test_parallel_writes_exactly_one_wins_per_revision(client, h, basic_def):
    """N threads racing on the same revision: exactly one commit, rest get 409."""
    client.post("/api/reports", headers=h("alice"),
                json={"key": "t_conc_2", "title": "竞速", "definition": basic_def})

    results: list[int] = []
    barrier = threading.Barrier(6)

    def worker():
        barrier.wait()
        r = client.put("/api/reports/t_conc_2", headers=h("alice"), json={
            "title": "race",
            "definition": {**basic_def, "dimensions": ["by_channel"]},
            "expected_revision": 1,
        })
        results.append(r.status_code)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results).count(200) == 1
    assert sorted(results).count(409) == 5
    final = client.get("/api/reports/t_conc_2", headers=h("alice")).json()
    assert final["revision"] == 2  # only one revision bump landed


def test_version_switch_also_guarded_by_revision(client, h, basic_def):
    client.post("/api/reports", headers=h("alice"),
                json={"key": "t_conc_3", "title": "切换竞速", "definition": basic_def})
    metrics = client.get("/api/catalog/metrics").json()
    rev = next(m for m in metrics if m["key"] == "revenue")
    v1 = next(v for v in rev["versions"] if v["version"] == 1)["id"]

    payload = {"metric_key": "revenue", "target_version_id": v1, "expected_revision": 1}
    r1 = client.post("/api/reports/t_conc_3/switch-version", headers=h("alice"), json=payload)
    r2 = client.post("/api/reports/t_conc_3/switch-version", headers=h("alice"), json=payload)
    assert r1.status_code == 200
    assert r2.status_code == 409
