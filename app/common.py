"""Shared app plumbing. The app talks only to the service layer (p2.services); this file builds it once,
remembers who is acting, and caches read-only lists so a click does not cost a BigQuery round trip each time."""
import os

import streamlit as st

from p2.registry.registry import Registry
from p2.services.permissions import Actor
from p2.services.platform import Platform
from p2.store.memory import MemoryStore
from p2.store.schema import APP_TABLES
from p2.warehouse.sources import load_sources

def _key() -> tuple[str, str]:
    """Which store and environment: P2_STORE (bigquery, memory for tests, or demo for the read-only public demo) and P2_ENV."""
    return os.environ.get("P2_STORE", "bigquery"), os.environ.get("P2_ENV", "dev")


@st.cache_resource(show_spinner="Connecting to BigQuery...")
def _platform(kind: str, env: str) -> Platform:
    sources = load_sources()
    if kind == "memory":
        from p2.testing import FakeChecker, FakeRunner
        platform = Platform(MemoryStore(APP_TABLES), sources, checker=FakeChecker(), runner=FakeRunner(), inline_runs=True)
    elif kind == "demo":     # a frozen copy of the app tables: no BigQuery, no credentials, nothing can be run
        from p2.store import frozen
        platform = Platform(frozen.load(), sources)
    else:
        from p2.catalog.checks import SqlChecker
        from p2.pipeline.runner import PipelineRunner
        from p2.store.bigquery import BigQueryStore
        from p2.store.snapshot import SnapshotStore
        from p2.warehouse.bq import Warehouse
        wh = Warehouse(env=env)  # the app creates only its own dataset; landing and staging are created by the simulator
        store = SnapshotStore(BigQueryStore(wh), refresh_seconds=float(os.environ.get("P2_REFRESH_SECONDS", "60")),
                              flush_seconds=float(os.environ.get("P2_FLUSH_SECONDS", "2")))
        store.ensure()
        platform = Platform(store, sources, checker=SqlChecker(wh), runner=PipelineRunner(wh), max_runs=int(os.environ.get("P2_MAX_RUNS", "4")))
    platform.bootstrap()
    platform.recover_interrupted_runs()
    return platform


def read_only() -> bool:
    """The public demo: everything can be looked at, nothing can be added, edited or run."""
    return _key()[0] == "demo"


def get_platform() -> Platform:
    return _platform(*_key())


def get_registry() -> Registry:
    return read("metric_registry")


WAREHOUSE_READS = {"assignment_check"}   # these scan the raw warehouse (billed), so they stay cached; everything else is read from memory


@st.cache_data(ttl=120, show_spinner="Checking the experiment tool's log...")
def _cached(method: str, key: tuple, args: tuple):
    return getattr(get_platform(), method)(*args)


def read(method: str, *args):
    """A read-only service call (positional arguments only). Application data comes from the in-memory copy the store keeps, so it is
    instant and free; calls that scan the warehouse are cached for two minutes."""
    if method in WAREHOUSE_READS:
        return _cached(method, _key(), args)
    return getattr(get_platform(), method)(*args)


def save_backlog() -> tuple[int, float]:
    """(changes still waiting to be written to BigQuery, seconds the oldest has waited). Zero unless BigQuery is slow or unreachable."""
    store = get_platform().store
    return (store.pending(), store.pending_age()) if hasattr(store, "pending") else (0, 0.0)


def refresh() -> None:
    st.cache_data.clear()


UI_ACTOR = Actor("admin", "Platform admin", "admin", None)


def current_actor() -> Actor:
    """The tool has no sign-in and no permissions: everyone can add, edit and run experiments (trust the data scientists). Every
    action runs as this one identity; the experiment's owner is just a field the person fills in."""
    return UI_ACTOR
