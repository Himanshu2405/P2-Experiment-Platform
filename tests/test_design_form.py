import pytest

from p2.catalog.seed import seed_registry
from p2.design_form import describe, to_plan, to_ui

reg = seed_registry()


def test_binary_percent_to_fraction():
    p = to_plan(reg.get("conversion_rate"), "primary", 3.5, None, 10.0, "relative", 0.05, 0.8, "two-sided")
    assert p.baseline == pytest.approx(0.035) and p.effect == pytest.approx(0.10) and p.std is None


def test_binary_absolute_is_percentage_points():
    p = to_plan(reg.get("conversion_rate"), "primary", 3.5, None, 0.5, "absolute", 0.05, 0.8, "two-sided")
    assert p.effect == pytest.approx(0.005) and p.effect_abs == pytest.approx(0.005)


def test_continuous_absolute_stays_in_units_relative_is_percent():
    a = to_plan(reg.get("revenue_per_user"), "primary", 3.5, 27.0, 0.35, "absolute", 0.05, 0.8, "two-sided")
    r = to_plan(reg.get("revenue_per_user"), "primary", 3.5, 27.0, 10.0, "relative", 0.05, 0.8, "two-sided")
    assert a.effect == pytest.approx(0.35) and r.effect == pytest.approx(0.10)
    assert a.effect_abs == pytest.approx(r.effect_abs)


def test_invalid_entries_raise_a_clear_error():
    with pytest.raises(ValueError, match="between 0 and 1"):
        to_plan(reg.get("conversion_rate"), "primary", 350, None, 10.0, "relative", 0.05, 0.8, "two-sided")  # 350 percent: a unit slip
    with pytest.raises(ValueError, match="standard deviation"):
        to_plan(reg.get("revenue_per_user"), "primary", 3.5, None, 10.0, "relative", 0.05, 0.8, "two-sided")


def test_describe_primary_and_guardrail():
    p = to_plan(reg.get("conversion_rate"), "primary", 3.5, None, 10.0, "relative", 0.05, 0.8, "two-sided")
    assert describe(p) == "Looking for an improvement from 3.500% to 3.850%."
    g = to_plan(reg.get("refund_rate"), "guardrail", 0.4, None, 25.0, "relative", 0.05, 0.8, "one-sided")
    assert describe(g) == "Guardrail fails if it worsens past 0.500% (from 0.400%)."


def test_to_ui_is_the_inverse_of_to_plan():
    for metric, baseline, std, effect, kind in [("conversion_rate", 3.74, None, 10.0, "relative"),
                                                ("conversion_rate", 3.74, None, 0.5, "absolute"),
                                                ("revenue_per_user", 3.85, 29.2, 10.0, "relative"),
                                                ("revenue_per_user", 3.85, 29.2, 0.4, "absolute")]:
        m = reg.get(metric)
        p = to_plan(m, "primary", baseline, std, effect, kind, 0.05, 0.8, "two-sided")
        row = {"baseline": p.baseline, "std": p.std, "effect": p.effect, "effect_kind": p.effect_kind,
               "alpha": p.alpha, "power": p.power, "sidedness": p.sidedness}
        ui = to_ui(m, row)
        assert ui["baseline"] == pytest.approx(baseline) and ui["effect"] == pytest.approx(effect) and ui["kind"] == kind
        assert (ui["std"], ui["alpha"], ui["power"], ui["sidedness"]) == (std, 0.05, 0.8, "two-sided")
