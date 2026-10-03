"""Runs in the background: a small queue with a cap on how many run at once, one Run per experiment, retries for temporary BigQuery
problems, and a Run that always ends cleanly."""
import threading
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from p2.pipeline.runner import DataQualityError
from p2.services import errors
from p2.services.platform import Platform
from p2.services.runs import RunManager
from p2.stats.plan import MetricPlan
from p2.store.memory import MemoryStore
from p2.store.schema import APP_TABLES
from p2.testing import FakeChecker, FakeRunner
from p2.warehouse.sources import load_sources


class Gate:
    """Work that blocks until released, so a test can look at the queue while Runs are in flight."""

    def __init__(self):
        self.release = threading.Event()
        self.started, self.finished, self.live, self.peak = [], [], 0, 0
        self.lock = threading.Lock()

    def __call__(self, actor, experiment_id, final):
        with self.lock:
            self.started.append(experiment_id)
            self.live += 1
            self.peak = max(self.peak, self.live)
        self.release.wait(5)
        with self.lock:
            self.live -= 1
            self.finished.append(experiment_id)
        return f"done {experiment_id}"


def until(condition, timeout=3.0):
    end = time.time() + timeout
    while not condition() and time.time() < end:
        time.sleep(0.01)
    return condition()


def test_at_most_the_allowed_number_of_runs_go_at_once_and_the_rest_wait_in_order():
    gate = Gate()
    runs = RunManager(gate, max_concurrent=2)
    for i in range(5):
        runs.submit("a", f"exp-{i}", final=False)
    assert until(lambda: gate.live == 2)
    time.sleep(0.1)
    assert gate.live == 2 and runs.status("exp-0")["state"] == "running"
    assert [(s["experiment_id"], s["position"]) for s in runs.active() if s["state"] == "queued"] == [("exp-2", 1), ("exp-3", 2), ("exp-4", 3)]
    assert runs.status("exp-4")["position"] == 3
    gate.release.set()
    assert runs.wait_all(5)
    assert gate.peak == 2 and gate.started == ["exp-0", "exp-1", "exp-2", "exp-3", "exp-4"]          # never more than two together, first come first served
    assert all(runs.status(f"exp-{i}")["state"] == "succeeded" for i in range(5))


def test_asking_again_while_a_run_is_waiting_or_running_returns_that_run_and_starts_no_second_one():
    gate = Gate()
    runs = RunManager(gate, max_concurrent=1)
    first = runs.submit("a", "exp-1", final=False)
    assert until(lambda: gate.live == 1)
    twin = runs.submit("b", "exp-1", final=True)                                   # while it is running, even for a different kind
    runs.submit("a", "exp-2", final=False)
    waiting_twin = runs.submit("b", "exp-2", final=False)                          # while it is waiting
    assert twin.duplicate and waiting_twin.duplicate and not first.duplicate
    assert twin.kind == "monitor"                                                  # it is the Run already in flight, not a new request
    gate.release.set()
    runs.wait_all(5)
    assert gate.started == ["exp-1", "exp-2"]


def test_a_temporary_problem_is_retried_with_growing_pauses_and_then_succeeds():
    calls, pauses = [], []

    def flaky(actor, experiment_id, final):
        calls.append(1)
        if len(calls) < 3:
            raise errors.TransientError("429 rate limit")
        return "ok"

    runs = RunManager(flaky, retry_pauses=(5.0, 15.0, 45.0), sleep=pauses.append)
    runs.submit("a", "exp-1", final=False)
    assert runs.wait_all(5)
    status = runs.status("exp-1")
    assert (status["state"], status["attempt"], status["result"]) == ("succeeded", 3, "ok") and pauses == [5.0, 15.0]


def test_when_bigquery_stays_busy_the_run_gives_up_with_a_plain_message():
    runs = RunManager(lambda *a: (_ for _ in ()).throw(errors.TransientError("503 unavailable")), retry_pauses=(0.0, 0.0), sleep=lambda s: None)
    runs.submit("a", "exp-1", final=False)
    runs.wait_all(5)
    status = runs.status("exp-1")
    assert status["state"] == "failed" and status["attempt"] == 3 and "stayed busy after 3 tries" in status["error"]


def test_a_real_failure_is_shown_and_never_retried():
    calls = []

    def broken(actor, experiment_id, final):
        calls.append(1)
        raise errors.InvalidInput("Data-quality gate failed: no assignments landed")

    runs = RunManager(broken, sleep=lambda s: pytest.fail("a real failure must not wait and retry"))
    runs.submit("a", "exp-1", final=False)
    runs.wait_all(5)
    assert len(calls) == 1 and runs.status("exp-1")["state"] == "failed" and "no assignments landed" in runs.status("exp-1")["error"]


def test_a_finished_run_is_remembered_until_acknowledged_and_a_new_request_replaces_it():
    runs = RunManager(lambda *a: "ok")
    runs.submit("a", "exp-1", final=True)
    runs.wait_all(5)
    assert runs.status("exp-1")["state"] == "succeeded" and runs.status("exp-1")["kind"] == "final"
    runs.acknowledge("exp-1")
    assert runs.status("exp-1") is None and runs.active() == []
    runs.submit("a", "exp-1", final=False)
    runs.wait_all(5)
    assert runs.status("exp-1")["kind"] == "monitor"


def test_too_long_a_queue_is_refused(monkeypatch):
    import p2.services.runs as module
    monkeypatch.setattr(module, "MAX_WAITING", 2)
    gate = Gate()
    runs = RunManager(gate, max_concurrent=1)
    runs.submit("a", "exp-0", final=False)
    assert until(lambda: gate.live == 1)                                            # one running
    for i in (1, 2):
        runs.submit("a", f"exp-{i}", final=False)                                   # two waiting
    with pytest.raises(errors.InvalidInput, match="already waiting"):
        runs.submit("a", "exp-9", final=False)
    gate.release.set()
    runs.wait_all(5)


def test_inline_mode_runs_in_the_callers_thread_before_returning():
    ran = []
    runs = RunManager(lambda a, e, f: ran.append(threading.current_thread().name) or "x", inline=True)
    ticket = runs.submit("a", "exp-1", final=False)
    assert ticket.state == "succeeded" and ran == [threading.current_thread().name] and runs.status("exp-1") is None


def test_which_failures_count_as_temporary():
    class Reasoned(Exception):
        errors = [{"reason": "rateLimitExceeded"}]

    class Coded(Exception):
        code = 503

    assert errors.is_transient(Reasoned("x")) and errors.is_transient(Coded("x"))
    assert errors.is_transient("step m_conversion_rate failed: 429 Exceeded rate limits: too many concurrent queries")
    assert errors.is_transient(RuntimeError("Connection reset by peer")) and errors.is_transient("Resources exceeded during query execution")
    assert not errors.is_transient("no assignments landed for exp-1") and not errors.is_transient(ValueError("bad plan"))
    assert not errors.is_transient("final table has 1 rows but the audience has 2 users") and not errors.is_transient(None)


# ---- through the platform -------------------------------------------------------------------------------------------------
class SlowRunner(FakeRunner):
    def __init__(self):
        super().__init__()
        self.gate = threading.Event()
        self.gate.set()
        self.fail_first = 0
        self.attempts = 0

    def build(self, *a, **k):
        self.attempts += 1
        self.gate.wait(5)
        if self.attempts <= self.fail_first:
            raise DataQualityError("step m_conversion_rate failed: 429 Exceeded rate limits: too many concurrent queries")
        return super().build(*a, **k)


@pytest.fixture
def plat():
    runner = SlowRunner()
    p = Platform(MemoryStore(APP_TABLES), load_sources(), checker=FakeChecker(), runner=runner, retry_pauses=(0.0, 0.0, 0.0))
    p.bootstrap()
    return p


def planned(p, eid="exp-001", launch=date(2026, 1, 1), runtime=14, user="priya"):
    reg = p.metric_registry()
    p.create_experiment(p.get_actor(user), eid, "checkout", "Banner", "h", launch, runtime)
    p.save_design(p.get_actor(user), eid, MetricPlan(reg.get("conversion_rate"), "primary", 0.04, 0.1, "relative", 0.05, 0.8, "two-sided"), [], [])
    return p.get_actor(user)


def test_start_run_returns_at_once_and_the_numbers_appear_when_it_finishes(plat):
    who = planned(plat)
    plat.runner.gate.clear()
    t = time.time()
    status = plat.start_run(who, "exp-001")
    assert time.time() - t < 0.5 and status["state"] in ("queued", "running") and status["kind"] == "final"      # January is over: final analysis
    assert plat.latest_results("exp-001") == [] and plat.run_status("exp-001")["state"] in ("queued", "running")
    plat.runner.gate.set()
    assert plat.runs.wait_all(5)
    assert plat.run_status("exp-001")["state"] == "succeeded" and plat.get_experiment("exp-001")["status"] == "Analyzed"
    assert plat.latest_results("exp-001")[0]["verdict"]


def test_the_kind_follows_the_dates_and_obvious_mistakes_are_refused_before_queueing(plat):
    who = planned(plat, launch=date.today() - timedelta(days=3))
    assert plat.start_run(who, "exp-001")["kind"] == "monitor"                                                  # still running: a live refresh
    plat.runs.wait_all(5)
    with pytest.raises(errors.InvalidInput, match="still running until"):
        plat.start_run(who, "exp-001", final=True)
    planned(plat, "exp-002", launch=date.today() + timedelta(days=4))
    with pytest.raises(errors.InvalidInput, match="has not launched yet"):
        plat.start_run(who, "exp-002")
    plat.create_experiment(who, "exp-003", "checkout", "No plan", "h", date(2026, 1, 1), 14)
    with pytest.raises(errors.InvalidInput, match="save its plan"):
        plat.start_run(who, "exp-003")
    assert plat.runs.active() == [] and plat.run_status("exp-002") is None and plat.run_status("exp-003") is None   # nothing was queued
    plat.runner = None
    with pytest.raises(errors.InvalidInput, match="no warehouse"):
        plat.start_run(who, "exp-001")


def test_only_one_run_per_experiment_even_if_two_people_click(plat):
    who = planned(plat)
    plat.runner.gate.clear()
    first = plat.start_run(who, "exp-001")
    second = plat.start_run(plat.get_actor("admin"), "exp-001")
    assert second["duplicate"] is True and first["duplicate"] is False
    plat.runner.gate.set()
    plat.runs.wait_all(5)
    assert plat.runner.attempts == 1 and len(plat.list_jobs("exp-001")) == 1


def test_a_temporary_bigquery_problem_during_a_run_is_retried_and_the_job_history_shows_each_try(plat):
    who = planned(plat)
    plat.runner.fail_first = 2
    plat.start_run(who, "exp-001")
    plat.runs.wait_all(5)
    status = plat.run_status("exp-001")
    assert (status["state"], status["attempt"]) == ("succeeded", 3) and plat.runner.attempts == 3
    assert sorted(j["status"] for j in plat.list_jobs("exp-001")) == ["failed", "failed", "succeeded"]


def test_a_data_problem_fails_the_run_without_retrying_and_the_job_is_closed(plat):
    who = planned(plat, "exp-bad-1")                                                                               # the fake runner fails this id's gate
    plat.start_run(who, "exp-bad-1")
    plat.runs.wait_all(5)
    status = plat.run_status("exp-bad-1")
    assert status["state"] == "failed" and status["attempt"] == 1 and "Data-quality gate failed" in status["error"]
    assert [j["status"] for j in plat.list_jobs("exp-bad-1")] == ["failed"]


def test_an_unexpected_crash_still_closes_the_job_instead_of_leaving_it_running(plat):
    who = planned(plat)
    plat.runner.build = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("something odd"))
    with pytest.raises(errors.InvalidInput, match="The run failed: RuntimeError: something odd"):
        plat.run_analysis(who, "exp-001", as_of=datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert [j["status"] for j in plat.list_jobs("exp-001")] == ["failed"] and plat.get_experiment("exp-001")["status"] == "Designed"


def test_runs_left_running_by_a_stopped_server_are_marked_failed_on_start(plat):
    who = planned(plat)
    old = plat.clock() - timedelta(minutes=30)
    plat.store.insert("job_runs", {"job_id": "stale", "experiment_id": "exp-001", "type": "monitor", "status": "running", "started_at": old,
                                   "finished_at": None, "error": None, "detail": None})
    plat.store.insert("job_runs", {"job_id": "fresh", "experiment_id": "exp-001", "type": "monitor", "status": "running",
                                   "started_at": plat.clock(), "finished_at": None, "error": None, "detail": None})
    assert plat.recover_interrupted_runs() == 1
    jobs = {j["job_id"]: j for j in plat.list_jobs("exp-001")}
    assert jobs["stale"]["status"] == "failed" and "interrupted" in jobs["stale"]["error"] and jobs["fresh"]["status"] == "running"
