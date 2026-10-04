"""The frozen copy of the app tables behind the read-only public demo, and the demo mode of the app."""
import json
import sys
from datetime import date
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from p2.services.permissions import Actor
from p2.services.platform import Platform
from p2.store import frozen
from p2.store.memory import MemoryStore
from p2.store.schema import APP_TABLES
from p2.testing import FakeChecker, FakeRunner
from p2.warehouse.sources import load_sources

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
sys.path.insert(0, str(APP))
EXAMPLES = sorted(p.name for p in (ROOT / "experiments").iterdir() if (p / "results.json").exists())


def test_every_column_type_survives_the_round_trip(tmp_path):
    plat = Platform(MemoryStore(APP_TABLES), load_sources(), checker=FakeChecker(), runner=FakeRunner(), inline_runs=True)
    plat.bootstrap()
    plat.create_experiment(Actor("admin", "Platform admin", "admin", None), "exp-f", "checkout", "Banner", "More orders.",
                           date(2026, 9, 1), 14, None, "priya")
    path = tmp_path / "tables.json.gz"
    counts = frozen.dump(plat.store, path)
    copy = frozen.load(path)
    assert counts["experiments"] == 1 and counts["catalog_items"] > 0
    for table in APP_TABLES:
        assert copy.select(table) == plat.store.select(table), table     # dates, timestamps, JSON and nulls come back the same


def test_the_committed_demo_data_holds_every_example_page_with_its_final_results():
    store = frozen.load()
    ids = {e["experiment_id"] for e in store.select("experiments")}
    assert set(EXAMPLES) <= ids and "demo-preview" in EXAMPLES
    for eid in EXAMPLES:
        saved = {r["item_id"]: r for r in store.select("results", {"experiment_id": eid}) if r["kind"] == "final"}
        page = json.loads((ROOT / "experiments" / eid / "results.json").read_text())
        primary = next(m for m in page["metrics"] if m["role"] == "primary")
        assert saved[primary["item_id"]]["n_control"] == primary["n_control"], eid   # the demo shows the same numbers as the page


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.setenv("P2_STORE", "demo")
    st.cache_resource.clear()
    st.cache_data.clear()
    yield
    st.cache_resource.clear()
    st.cache_data.clear()


def test_the_demo_shows_results_but_no_edit_or_run_buttons(demo):
    at = AppTest.from_file(str(APP / "views" / "experiments.py"), default_timeout=60)
    at.session_state["search_pick"] = "demo-preview"
    at.run()
    assert not at.exception
    assert any("Variant is the safer choice" in m.value for m in at.markdown)
    assert not [b for b in at.button if b.key in ("edit_plan", "monitor_go", "run_go")]

    at = AppTest.from_file(str(APP / "views" / "catalog.py"), default_timeout=60).run()
    assert not at.exception and any(b.key == "cat_open_demo-preview" for b in at.button)
    assert not [b for b in at.button if (b.key or "").startswith("cat_edit_")]


def test_the_demo_entry_point_starts_without_bigquery_and_says_it_is_read_only(demo):
    at = AppTest.from_file(str(APP / "demo_app.py"), default_timeout=60).run()
    assert not at.exception
    assert any("Read-only demo" in i.value for i in at.info)
