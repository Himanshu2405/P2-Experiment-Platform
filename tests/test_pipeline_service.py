"""refresh_monitor and run_analysis (the build step), job logging and the simulator hook in the service layer, using a fake runner (no warehouse)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from p2.services import errors
from p2.services.platform import Platform
from p2.stats.plan import MetricPlan
from p2.store.memory import MemoryStore
from p2.store.schema import APP_TABLES
from p2.testing import FakeChecker, FakeRunner
from p2.warehouse.sources import load_sources


class Clock:
    def __init__(self):
        self.t = datetime(2026, 3, 1, tzinfo=timezone.utc)

    def __call__(self):
        self.t += timedelta(seconds=1)
        return self.t


@pytest.fixture
def plat():
    clock = Clock()
    p = Platform(MemoryStore(APP_TABLES, clock), load_sources(), clock, checker=FakeChecker(), runner=FakeRunner())
    p.bootstrap()
    return p


AFTER = datetime(2026, 2, 1, tzinfo=timezone.utc)   # a day after the 14-day runtime that starts 2026-01-01


def who(p, u):
    return p.get_actor(u)


def designed(p, eid="exp-1", user="priya", product="checkout", **kw):
    p.create_experiment(who(p, user), eid, product, "n", "h", date(2026, 1, 1), 14)
    m = p.metric_registry().get(kw.pop("metric", "conversion_rate"))
    p.save_design(who(p, user), eid, MetricPlan(m, "primary", 0.0374, 0.1, "relative", 0.05, 0.8, "two-sided"), [],
                  ["add_to_cart_rate"], **kw)


def test_success_builds_logs_a_job_and_moves_status(plat):
    designed(plat, dimension_ids=["plan_tier"], filter_ids=["existing_user"])
    res = plat.run_analysis(who(plat, "priya"), "exp-1", as_of=AFTER)
    assert res.audience_users == 900
    build = plat.runner.builds[0]
    assert build == {"experiment_id": "exp-1", "metrics": ["conversion_rate", "add_to_cart_rate"],
                     "dimensions": ["plan_tier"], "filters": ["existing_user"],
                     "launch_date": date(2026, 1, 1), "end_date": date(2026, 1, 14), "final": True}
    assert plat.get_experiment("exp-1")["status"] == "Analyzed"
    job = plat.list_jobs("exp-1")[0]
    assert job["status"] == "succeeded" and job["error"] is None and job["detail"]["audience_users"] == 900
    steps = {s["step"]: s for s in plat.job_steps(job["job_id"])}
    assert set(steps) == {"gates_before", "cohort", "m_conversion_rate", "m_add_to_cart_rate", "stats", "daily", "segments"} and steps["cohort"]["row_count"] == 1000
    assert [a["action"] for a in plat.list_audit(who(plat, "admin"), "exp-1")][:1] == ["analysis.run"]


def test_rebuild_is_allowed_and_each_run_is_logged(plat):
    designed(plat)
    plat.run_analysis(who(plat, "priya"), "exp-1", as_of=AFTER)
    plat.run_analysis(who(plat, "priya"), "exp-1", as_of=AFTER)
    assert len(plat.list_jobs("exp-1")) == 2 and plat.get_experiment("exp-1")["status"] == "Analyzed"


def test_a_failed_gate_is_logged_and_leaves_the_status(plat):
    designed(plat, "exp-bad-1")
    with pytest.raises(errors.InvalidInput, match="Data-quality gate failed: final table"):
        plat.run_analysis(who(plat, "priya"), "exp-bad-1", as_of=AFTER)
    assert plat.get_experiment("exp-bad-1")["status"] == "Designed"
    job = plat.list_jobs("exp-bad-1")[0]
    assert job["status"] == "failed" and "audience has 2 users" in job["error"] and job["detail"] is None
    assert {s["step"]: s["status"] for s in plat.job_steps(job["job_id"])}["gates_after"] == "failed"
    assert plat.list_audit(who(plat, "admin"), "exp-bad-1")[0]["action"] == "pipeline.failed"


def test_permissions_and_state_rules(plat):
    designed(plat)
    for user in ("marcus", "viewer"):
        with pytest.raises(errors.PermissionDenied):
            plat.run_analysis(who(plat, user), "exp-1", as_of=AFTER)
    plat.create_experiment(who(plat, "priya"), "exp-2", "checkout", "n", "h", date(2026, 1, 1), 14)
    with pytest.raises(errors.InvalidInput, match="save its plan before running it"):
        plat.run_analysis(who(plat, "priya"), "exp-2", as_of=AFTER)
    plat.run_analysis(who(plat, "admin"), "exp-1", as_of=AFTER)
    plat.set_status(who(plat, "priya"), "exp-1", "Decided")
    plat.run_analysis(who(plat, "priya"), "exp-1", as_of=AFTER)          # a decided experiment can be re-run; the decision stays
    assert plat.get_experiment("exp-1")["status"] == "Decided"


def test_pinned_versions_are_what_gets_built(plat):
    designed(plat)
    v2 = plat.new_version(who(plat, "priya"), "conversion_rate")
    plat.update_draft(who(plat, "priya"), "conversion_rate", 2, description="Changed after the experiment was designed")
    plat.run_checks(who(plat, "priya"), "conversion_rate", 2)
    plat.submit_for_review(who(plat, "priya"), "conversion_rate", 2)
    plat.certify(who(plat, "admin"), "conversion_rate", 2)
    plat.run_analysis(who(plat, "priya"), "exp-1", as_of=AFTER)
    assert plat.get_design("exp-1")[0]["item_version"] == 1 and v2["version"] == 2  # still the pinned version


def test_no_runner_means_a_clear_error_and_no_job(plat):
    designed(plat)
    plat.runner = None
    with pytest.raises(errors.InvalidInput, match="no warehouse"):
        plat.run_analysis(who(plat, "priya"), "exp-1", as_of=AFTER)
    with pytest.raises(errors.InvalidInput, match="no warehouse"):
        plat.simulate_data(who(plat, "priya"), "exp-1", {})
    assert plat.list_jobs("exp-1") == []


def test_experiment_frame_passes_through(plat):
    designed(plat)
    plat.run_analysis(who(plat, "priya"), "exp-1", as_of=AFTER)
    assert list(plat.experiment_frame("exp-1").columns) == ["user_id", "arm", "assigned_date", "conversion_rate", "add_to_cart_rate"]
