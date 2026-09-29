"""On-demand "Find matches": the run that chains discovery → careers pages → job reading.
(Its API side — activity in /status, one heavy run at a time — is tested in test_jobscout_api.py.)"""

from jobscout import db, discovery, find, pipeline, runs, sweep


def test_find_runs_every_step_in_order(data_dir, monkeypatch):
    calls = []

    def fake_discovery(run, options=None, profile=None, fetcher=None, start_pipeline=True):
        calls.append(("discover", start_pipeline))
        run.progress(3, 3, "discover")
        return {"queries": 3, "candidates": 20, "new_companies": 4}

    def fake_pipeline(run, fetcher=None, **_):
        calls.append(("pipeline",))
        return {"companies": 4, "jobs_new": 5, "matching_new": 2, "errors": 0}

    def fake_sweep(run, fetcher=None, skip_swept_since=None, **_):
        calls.append(("sweep", bool(skip_swept_since)))
        return {"companies": 30, "jobs_seen": 400, "jobs_new": 7, "matching_new": 3, "score_tasks": 5, "errors": 1}

    monkeypatch.setattr(discovery, "run_discovery", fake_discovery)
    monkeypatch.setattr(pipeline, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(sweep, "run_sweep", fake_sweep)
    run = runs.run_inline("find", find.run_find, options={})
    assert calls == [("discover", False), ("pipeline",), ("sweep", True)]
    s = run.stats
    assert (s["new_companies"], s["companies_checked"], s["companies_read"]) == (4, 4, 30)
    assert (s["jobs_new"], s["matching_new"], s["score_tasks"], s["errors"]) == (12, 5, 5, 1)
    with db.session() as conn:
        log = conn.execute("SELECT log FROM runs WHERE id=?", (run.id,)).fetchone()["log"]
    assert "step 1 of 3" in log and "step 3 of 3" in log and "No job categories" in log

    calls.clear()
    runs.run_inline("find", find.run_find, options={"skip_discovery": True})
    assert [c[0] for c in calls] == ["pipeline", "sweep"]
