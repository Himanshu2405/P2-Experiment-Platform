"""One contract, four implementations: every test runs against MemoryStore, the snapshot store over it and (marked bq) BigQueryStore
with and without the snapshot on top."""
import uuid
from datetime import date, datetime, timezone

import pytest

from p2.store.base import AlreadyExists, Col, Conflict, NotFound, TableDef
from p2.store.memory import MemoryStore
from p2.store.snapshot import SnapshotStore

KV = TableDef("kv", (Col("id", "STRING"), Col("n", "INT64", True), Col("x", "FLOAT64", True), Col("flag", "BOOL", True),
                     Col("ts", "TIMESTAMP", True), Col("d", "DATE", True), Col("doc", "JSON", True),
                     Col("label", "STRING", True), Col("updated_at", "TIMESTAMP")), ("id",))
KV2 = TableDef("kv2", (Col("a", "STRING"), Col("b", "STRING"), Col("v", "INT64", True)), ("a", "b"))
TABLES = {"kv": KV, "kv2": KV2}
NOW = datetime(2026, 1, 5, 12, 30, 15, 123456, tzinfo=timezone.utc)


@pytest.fixture(params=["memory", "snapshot", pytest.param("bigquery", marks=pytest.mark.bq), pytest.param("snapshot_bq", marks=pytest.mark.bq)])
def store(request):
    if request.param == "memory":
        yield MemoryStore(TABLES)
        return
    if request.param == "snapshot":
        backend = MemoryStore(TABLES)
    else:
        from p2.store.bigquery import BigQueryStore
        backend = BigQueryStore(request.getfixturevalue("wh"), TABLES)
        backend.ensure()
        if request.param == "bigquery":
            yield backend
            return
    snap = SnapshotStore(backend, refresh_seconds=None, flush_seconds=None)
    yield snap
    snap.flush()                      # whatever the test did must also be accepted by the real store (a rejection fails the test here)
    assert snap.pending() == 0


def rid() -> str:
    return uuid.uuid4().hex[:10]


def row(**over):
    base = dict(id=rid(), n=7, x=2.5, flag=True, ts=NOW, d=date(2026, 1, 5), doc={"a": [1, 2], "b": {"c": None}},
                label="hello", updated_at=NOW)
    return {**base, **over}


def test_round_trip_every_type(store):
    r = row()
    store.insert("kv", r)
    assert store.get("kv", {"id": r["id"]}) == r


def test_nullable_columns_default_to_none(store):
    i = rid()
    store.insert("kv", {"id": i, "updated_at": NOW})
    got = store.get("kv", {"id": i})
    assert got["n"] is None and got["doc"] is None and got["ts"] is None


def test_get_missing_returns_none(store):
    assert store.get("kv", {"id": rid()}) is None


def test_duplicate_key_rejected(store):
    r = row()
    store.insert("kv", r)
    with pytest.raises(AlreadyExists):
        store.insert("kv", row(id=r["id"], label="other"))
    assert store.get("kv", {"id": r["id"]})["label"] == "hello"


def test_composite_keys(store):
    a = rid()
    store.insert("kv2", {"a": a, "b": "1", "v": 1})
    store.insert("kv2", {"a": a, "b": "2", "v": 2})
    with pytest.raises(AlreadyExists):
        store.insert("kv2", {"a": a, "b": "1", "v": 9})
    assert store.get("kv2", {"a": a, "b": "2"})["v"] == 2
    assert len(store.select("kv2", {"a": a})) == 2


def test_insert_many_is_atomic(store):
    taken = row()
    store.insert("kv", taken)
    fresh = row()
    with pytest.raises(AlreadyExists):
        store.insert_many("kv", [fresh, row(id=taken["id"])])
    assert store.get("kv", {"id": fresh["id"]}) is None
    with pytest.raises(AlreadyExists):
        dup = rid()
        store.insert_many("kv", [row(id=dup), row(id=dup)])
    ok = [row(), row()]
    store.insert_many("kv", ok)
    assert all(store.get("kv", {"id": r["id"]}) == r for r in ok)
    store.insert_many("kv", [])


def test_insert_missing_skips_existing_and_reports_how_many(store):
    keep = row(label="original")
    store.insert("kv", keep)
    fresh = [row(), row(n=None, doc=None)]
    assert store.insert_missing("kv", [row(id=keep["id"], label="overwrite?"), *fresh]) == 2
    assert store.get("kv", {"id": keep["id"]})["label"] == "original"  # untouched
    assert store.insert_missing("kv", fresh) == 0                        # second run inserts nothing
    assert all(store.get("kv", {"id": r["id"]}) == r for r in fresh)
    assert store.insert_missing("kv", []) == 0
    with pytest.raises(AlreadyExists):
        dup = rid()
        store.insert_missing("kv", [row(id=dup), row(id=dup)])
    with pytest.raises(ValueError):
        store.insert_missing("kv", [row(n="x")])


@pytest.mark.parametrize("bad", [
    {"n": "7"}, {"n": True}, {"x": "1.0"}, {"flag": 1}, {"ts": datetime(2026, 1, 1)}, {"d": datetime(2026, 1, 1, tzinfo=timezone.utc)},
    {"doc": {1, 2}}, {"label": 5}, {"nope": 1}, {"updated_at": None},
])
def test_invalid_rows_rejected(store, bad):
    with pytest.raises(ValueError):
        store.insert("kv", row(**bad))


def test_missing_key_rejected(store):
    with pytest.raises(ValueError):
        store.insert("kv", {"updated_at": NOW})
    with pytest.raises(ValueError):
        store.get("kv", {"nope": "x"})
    with pytest.raises(ValueError):
        store.insert("nope", {})


def test_select_filters_none_and_order(store):
    tag = rid()
    store.insert("kv", row(label=tag, n=3))
    store.insert("kv", row(label=tag, n=1))
    store.insert("kv", row(label=tag, n=None))
    store.insert("kv", row(label=rid(), n=9))
    mine = store.select("kv", {"label": tag}, order_by=["n ASC"])
    assert [r["n"] for r in mine] == [None, 1, 3]  # NULLs sort first ascending, last descending
    assert [r["n"] for r in store.select("kv", {"label": tag}, order_by=["n DESC"])] == [3, 1, None]
    assert [r["n"] for r in store.select("kv", {"label": tag, "n": None})] == [None]
    assert len(store.select("kv", {"label": tag, "n": 3})) == 1
    assert len(store.select("kv", {"label": tag}, order_by=["n", "id DESC"])) == 3
    with pytest.raises(ValueError):
        store.select("kv", order_by=["n SIDEWAYS"])
    with pytest.raises(ValueError):
        store.select("kv", {"nope": 1})


def test_update_changes_and_sets_updated_at(store):
    r = row(updated_at=NOW)
    store.insert("kv", r)
    got = store.update("kv", {"id": r["id"]}, {"n": 8, "doc": {"z": 1}, "label": None})
    assert got["n"] == 8 and got["doc"] == {"z": 1} and got["label"] is None and got["x"] == 2.5
    assert got["updated_at"] > NOW
    assert store.get("kv", {"id": r["id"]}) == got


def test_update_validation(store):
    r = row()
    store.insert("kv", r)
    for bad in ({}, {"id": "new"}, {"n": "x"}, {"updated_at": None}, {"nope": 1}):
        with pytest.raises(ValueError):
            store.update("kv", {"id": r["id"]}, bad)


def test_update_missing_row(store):
    with pytest.raises(NotFound):
        store.update("kv", {"id": rid()}, {"n": 1})


def test_optimistic_concurrency(store):
    r = row()
    store.insert("kv", r)
    first = store.update("kv", {"id": r["id"]}, {"n": 1}, expected_updated_at=NOW)
    with pytest.raises(Conflict):
        store.update("kv", {"id": r["id"]}, {"n": 2}, expected_updated_at=NOW)  # stale
    assert store.get("kv", {"id": r["id"]})["n"] == 1
    store.update("kv", {"id": r["id"]}, {"n": 3}, expected_updated_at=first["updated_at"])
    assert store.get("kv", {"id": r["id"]})["n"] == 3


def test_delete(store):
    tag = rid()
    for _ in range(3):
        store.insert("kv", row(label=tag))
    keep = row(label=rid())
    store.insert("kv", keep)
    assert store.delete("kv", {"label": tag}) == 3
    assert store.select("kv", {"label": tag}) == []
    assert store.get("kv", {"id": keep["id"]}) is not None
    assert store.delete("kv", {"label": tag}) == 0
    with pytest.raises(ValueError):
        store.delete("kv", {})


def test_values_are_copied_not_shared():
    s = MemoryStore(TABLES)
    r = row()
    s.insert("kv", r)
    r["doc"]["b"]["c"] = "mutated"
    got = s.get("kv", {"id": r["id"]})
    got["doc"]["a"].append(99)
    assert s.get("kv", {"id": r["id"]})["doc"] == {"a": [1, 2], "b": {"c": None}}
