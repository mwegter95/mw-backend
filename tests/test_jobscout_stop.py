"""A run can't hang forever: browser searches have time limits, and Stop ends a run even when it is stuck."""
import asyncio
import contextlib
import threading
import time

import pytest

from jobscout import db, discovery, pipeline, runs

Q = [{"source": "search", "query": q, "town": "Hugo, MN", "industry": None} for q in ("a", "b", "c", "d", "e")]


@pytest.fixture
def fast(monkeypatch):
    """Short limits so the tests take a fraction of a second, and no pacing between searches."""
    monkeypatch.setattr(discovery, "QUERY_TIMEOUT", 0.2)
    monkeypatch.setattr(discovery, "CLOSE_TIMEOUT", 0.2)
    monkeypatch.setattr(discovery, "STALL_TIMEOUT", 0.6)
    monkeypatch.setattr(discovery, "_POLL_SECONDS", 0.05)
    monkeypatch.setattr(discovery.random, "uniform", lambda a, b: 0)


class FakeBrowsers:
    """Stands in for Playwright: counts launches and closes; `answer(q)` decides what each search does."""

    def __init__(self, answer):
        self.answer, self.launched, self.closed = answer, 0, []

    @contextlib.asynccontextmanager
    async def playwright(self):
        yield object()

    async def launch(self, pw):
        self.launched += 1
        n = self.launched

        class Browser:
            async def close(inner):
                self.closed.append(n)
        return Browser()

    async def one_query(self, browser, q):
        return await self.answer(q)

    def kwargs(self):
        return {"launch": self.launch, "one_query": self.one_query, "playwright": self.playwright}


async def _never():
    await asyncio.sleep(5)  # far past QUERY_TIMEOUT; long enough to count as "never" here
    return []


def test_a_search_that_never_answers_is_skipped_and_the_browser_replaced(fast):
    async def answer(q):
        return await _never() if q["query"] == "b" else [{"name": q["query"].upper(), "website": f"{q['query']}.example"}]

    fake, seen = FakeBrowsers(answer), []
    error = discovery.browser_search(Q[:3], lambda q, found, note: seen.append((q["query"], len(found), note)),
                                     **fake.kwargs())
    assert error is None
    assert [s[:2] for s in seen] == [("a", 1), ("b", 0), ("c", 1)]
    assert seen[1][2] == "no answer in 0.2 s, skipped" and seen[0][2] is None
    assert fake.launched == 2 and fake.closed == [1, 2]  # the stuck browser was closed, a fresh one finished


def test_browser_searches_stop_after_several_stuck_in_a_row(fast):
    fake, seen = FakeBrowsers(lambda q: _never()), []
    error = discovery.browser_search(Q, lambda q, found, note: seen.append(q["query"]), **fake.kwargs())
    assert error == "3 searches in a row got no answer, so the rest were skipped"
    assert seen == ["a", "b", "c"]


def test_a_browser_thread_that_goes_silent_is_abandoned(fast):
    fake = FakeBrowsers(lambda q: _never())

    async def launch_that_hangs(pw):
        await asyncio.sleep(3)  # not covered by QUERY_TIMEOUT: only the watchdog can end this
        return await fake.launch(pw)

    kwargs = dict(fake.kwargs(), launch=launch_that_hangs)
    started = time.monotonic()
    error = discovery.browser_search(Q, lambda *a: pytest.fail("no results expected"), **kwargs)
    assert error.startswith("the browser stopped answering") and time.monotonic() - started < 2


def test_browser_search_returns_when_the_run_is_stopped(fast):
    async def slow(q):
        await asyncio.sleep(0.1)
        return []

    fake, seen = FakeBrowsers(slow), []
    error = discovery.browser_search(Q, lambda q, found, note: seen.append(q["query"]),
                                     should_stop=lambda: len(seen) >= 1, **fake.kwargs())
    assert error == "stopped" and len(seen) < len(Q)


# ── Stop ────────────────────────────────────────────────────────────────────

def _wait(cond, seconds=3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_stop_ends_a_stuck_run_at_once_and_frees_its_slot(data_dir):
    release, exited = threading.Event(), threading.Event()

    def stuck(run, **_):
        try:
            run.log("step 1 of 3: looking for employers you haven't seen yet")
            run.progress(39, 60, "discover")
            release.wait(5)                   # stuck in a network call
            run.log("a line from after Stop")  # dropped
            run.progress(40, 60, "discover")   # checkpoint → Cancelled
            return {"never": 1}
        finally:
            exited.set()

    run = runs.start("find", stuck)
    assert _wait(lambda: run.current)
    assert runs.cancel(run.id)
    assert run.status == "cancelled" and "find" not in runs.active_ids()

    second = runs.start("find", lambda r: {"new_companies": 0})  # Find matches works again right away
    assert _wait(lambda: second.status == "done")

    release.set()
    assert exited.wait(3) and _wait(lambda: not any(t.name == f"jobs-run-{run.id}" for t in threading.enumerate()))
    with db.session() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run.id,)).fetchone()
    assert row["status"] == "cancelled" and row["finished_at"]
    assert "stopped: Stop was pressed" in row["log"] and "after Stop" not in row["log"]
    assert "never" not in db.loads(row["stats"], {})
    assert run.events[-1]["type"] == "done" and run.events[-1]["status"] == "cancelled"
    assert not runs.cancel(run.id)  # already over
    assert not runs.active_ids()


def test_stop_skips_the_companies_not_started_yet(data_dir):
    handled = []

    def target(run):
        def one(cid):
            handled.append(cid)
            if len(handled) == 3:
                runs.cancel(run.id)
            time.sleep(0.01)
            return {"companies": 1}
        return pipeline._parallel(run, list(range(200)), one, "pipeline")

    run = runs.start("pipeline", target)
    assert _wait(lambda: not any(t.name == f"jobs-run-{run.id}" for t in threading.enumerate()))
    assert run.status == "cancelled" and len(handled) < 20
