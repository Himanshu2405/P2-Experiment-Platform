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


def test_bayesian_inputs_convert_to_fractions_and_back():
    from p2.design_form import describe_bayes, to_bayes_plan, to_bayes_ui
    p = to_bayes_plan(reg.get("conversion_rate"), "primary", 0.05, 7, None, "relative", None)
    assert p.threshold == pytest.approx(0.0005) and p.min_days == 7 and p.threshold_kind == "absolute"
    avg = to_bayes_plan(reg.get("revenue_per_user"), "primary", 0.5, 7, None, "relative", None)
    assert avg.threshold == pytest.approx(0.005) and avg.threshold_kind == "relative"
    g = to_bayes_plan(reg.get("refund_rate"), "guardrail", None, None, 25.0, "relative", 1.0)
    assert (g.margin, g.margin_kind, g.harm_limit) == (pytest.approx(0.25), "relative", pytest.approx(0.01))
    a = to_bayes_plan(reg.get("refund_rate"), "guardrail", None, None, 0.2, "absolute", 1.0)
    assert a.margin == pytest.approx(0.002)                                          # a rate's absolute margin is in percentage points
    assert describe_bayes(p).startswith("Verdict after 7 days") and "0.05 percentage points" in describe_bayes(p)
    assert describe_bayes(g) == "Guardrail passes when the chance it got worse by more than 25% is below 1%."
    row = {"role": "primary", "loss_threshold": 0.0005, "min_days": 7}
    assert to_bayes_ui(reg.get("conversion_rate"), row) == {"threshold": pytest.approx(0.05), "min_days": 7}
    grow = {"role": "guardrail", "effect": 0.002, "effect_kind": "absolute", "harm_limit": 0.01}
    assert to_bayes_ui(reg.get("refund_rate"), grow) == {"kind": "absolute", "margin": pytest.approx(0.2), "harm_limit": pytest.approx(1.0)}


def test_bayesian_inputs_are_validated_with_a_clear_message():
    from p2.design_form import to_bayes_plan
    with pytest.raises(ValueError, match="risk threshold"):
        to_bayes_plan(reg.get("conversion_rate"), "primary", 0, 7, None, "relative", None)
    with pytest.raises(ValueError, match="minimum days"):
        to_bayes_plan(reg.get("conversion_rate"), "primary", 0.05, None, None, "relative", None)
    with pytest.raises(ValueError, match="harm limit"):
        to_bayes_plan(reg.get("refund_rate"), "guardrail", None, None, 25.0, "relative", 60.0)
