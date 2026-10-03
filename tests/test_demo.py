"""The September demo data: offline checks (landing it in BigQuery is checked live on the dev app)."""
import pandas as pd


# ---- September demo data ---------------------------------------------------------------------------------------------
def test_cut_after_drops_activity_after_the_end_of_that_day_and_leaves_static_tables_alone():
    from datetime import date

    from p2.simulator.service import cut_after
    from p2.warehouse.sources import load_sources
    sources = load_sources()
    ts = pd.to_datetime(["2026-09-30 23:59:59", "2026-10-01 00:00:00", "2026-10-03 10:00:00"])
    orders = pd.DataFrame({"order_id": ["a", "b", "c"], "order_ts": ts})
    users = pd.DataFrame({"user_id": ["u"], "signup_date": [date(2026, 10, 5)]})
    out = cut_after({"checkout_orders": orders, "users": users}, sources, date(2026, 9, 30))
    assert list(out["checkout_orders"].order_id) == ["a"]
    assert len(out["users"]) == 1
    assert cut_after({"checkout_orders": orders}, sources, None)["checkout_orders"].equals(orders)


def test_the_september_demo_has_pairs_per_product_and_every_id_is_in_september():
    from p2.simulator.demo import DEMO, MONTH_END, MONTH_START
    assert (MONTH_START.isoformat(), MONTH_END.isoformat()) == ("2026-09-01", "2026-09-30")
    assert {p for _, p, _, _ in DEMO} == {"checkout", "email", "onboarding"}
    assert len({eid for eid, *_ in DEMO}) == len(DEMO)
    assert all(eid.startswith(f"sep-{p}-") for eid, p, _, _ in DEMO)


def test_the_dev_button_calls_the_platform_and_the_expander_goes_away_at_deploy(monkeypatch):
    from test_app import page, platform
    calls = []
    p = platform()
    p.land_september_demo = lambda actor, progress=None: calls.append(actor.user_id) or ["sep-checkout-1"]
    at = page("design")
    assert any("September demo data" in e.label for e in at.expander)
    at.button(key="land_sep").click().run()
    assert calls == ["admin"] and not at.exception
    monkeypatch.setenv("P2_DEV_TOOLS", "0")
    assert not any("September demo data" in e.label for e in page("design").expander)
