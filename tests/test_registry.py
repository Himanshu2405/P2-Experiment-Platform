import pytest
from pydantic import ValidationError

from p2.catalog.seed import seed_registry
from p2.registry.registry import MetricDef, Registry


def make(**over):
    base = dict(
        metric_id="m1", version=1, product_id="checkout", display_name="M1", description="d", type="binary",
        window_aggregation="max", good_direction="higher",
    )
    return MetricDef(**{**base, **over})


@pytest.fixture(scope="module")
def reg():
    return seed_registry()


def test_provisional_registry_has_five_metrics_per_product(reg):
    by = {}
    for mid in reg.ids():
        by.setdefault(reg.get(mid).product_id, []).append(mid)
    assert {p: len(v) for p, v in by.items()} == {"checkout": 5, "email": 5, "onboarding": 5}
    assert len(reg.ids()) == len(set(reg.ids())) == 15  # ids are globally unique


def test_every_metric_is_offered_for_every_role(reg):
    offered = {m.metric_id for m in reg.all_metrics()}
    assert offered == set(reg.ids()) and len(offered) == 15
    assert {m.product_id for m in reg.all_metrics()} == {"checkout", "email", "onboarding"}  # metrics from every product, not one


def test_unknown_metric_and_version(reg):
    with pytest.raises(KeyError):
        reg.get("nope")
    with pytest.raises(KeyError):
        reg.get("conversion_rate", 99)


def test_latest_and_pinned_versions():
    r = Registry([make(version=1, description="old"), make(version=2, description="new")])
    assert r.get("m1").version == 2
    assert r.get("m1", 1).description == "old"
    assert [m.version for m in r.all_metrics()] == [2]


def test_duplicate_version_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        Registry([make(), make()])


@pytest.mark.parametrize("over", [
    dict(metric_id="Bad Id"), dict(version=0), dict(type="ratio"), dict(window_aggregation="mean"), dict(status="Draft"),
    dict(good_direction="up"), dict(product_id="billing"),
])
def test_invalid_definitions_rejected(over):
    with pytest.raises(ValidationError):
        make(**over)


def test_selection_valid(reg):
    reg.validate_selection("conversion_rate", ["refund_rate"], ["add_to_cart_rate", "orders_per_user"])


def test_any_metric_can_play_any_role_across_products(reg):
    reg.validate_selection("refund_rate", ["unsubscribe_rate"], ["conversion_rate"])  # was refused when roles and products were fixed


def test_selection_needs_known_metrics(reg):
    with pytest.raises((ValueError, KeyError), match="nope"):
        reg.validate_selection("nope", [], [])


def test_selection_metric_in_two_roles(reg):
    with pytest.raises(ValueError, match="more than once"):
        reg.validate_selection("conversion_rate", [], ["conversion_rate"])


def test_deprecated_versions_stay_pinnable_but_leave_the_dropdowns():
    r = Registry([make(version=1, status="Deprecated"), make(version=2, status="Certified"), make(metric_id="old", status="Deprecated")])
    assert r.get("m1").version == 2            # latest certified
    assert r.get("m1", 1).status == "Deprecated"  # still pinnable
    assert r.ids() == ["m1"]                    # "old" has no certified version
    with pytest.raises(KeyError, match="no certified version"):
        r.get("old")
    assert r.get("old", 1).metric_id == "old"
