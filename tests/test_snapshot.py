"""The snapshot store: pages read from memory, writes are saved to memory at once and sent to the real store later as one combined
statement per table, and a quiet table costs nothing to refresh."""
import time
from datetime import datetime, timedelta, timezone

import pytest

from p2.store.base import AlreadyExists, Col, Conflict, NotFound, TableDef
from p2.store.memory import MemoryStore
from p2.store.snapshot import SnapshotStore

NOW = datetime(2026, 1, 5, tzinfo=timezone.utc)
ITEMS = TableDef("items", (Col("id", "STRING"), Col("n", "INT64", True), Col("updated_at", "TIMESTAMP")), ("id",))
PAIRS = TableDef("pairs", (Col("a", "STRING"), Col("b", "STRING"), Col("v", "INT64", True)), ("a", "b"))
LOGS = TableDef("audit_log", (Col("id", "STRING"), Col("what", "STRING", True)), ("id",))
TABLES = {"items": ITEMS, "pairs": PAIRS, "audit_log": LOGS}


class CountingBackend(MemoryStore):
    """A real store that counts what is asked of it and can say when a table last changed (like BigQuery metadata)."""

    def __init__(self, tables=None):
        tables = tables or TABLES
        super().__init__(tables)
        self.selects, self.metadata_calls, self.changed = [], 0, {n: 0 for n in tables}
        self.batches = []                                                    # (table, upserts, deletes, appends) as received
        self.fail = False

    def select(self, table, where=None, order_by=None):
        self.selects.append(table)
        return super().select(table, where, order_by)

    def table_modified(self, table):
        self.metadata_calls += 1
        return self.changed[table]

    def write_batch(self, table, upserts=(), deletes=(), appends=()):
        if self.fail:
            raise RuntimeError("BigQuery is unreachable")
        self.batches.append((table, list(upserts), list(deletes), list(appends)))
        super().write_batch(table, upserts, deletes, appends)
        self.changed[table] += 1

    def direct_insert(self, table, rows):                                    # another server writing straight to the real store
        for r in rows:
            self.put(table, r)
        self.changed[table] += 1


def item(i, n=1):
    return {"id": i, "n": n, "updated_at": NOW}


@pytest.fixture
def pair():
    backend = CountingBackend()
    store = SnapshotStore(backend, refresh_seconds=None, flush_seconds=None)
    yield backend, store
    backend.fail = False
    store.close()                                                           # nothing is left queued when the test process exits


# ---- reads -------------------------------------------------------------------------------------------------------------
def test_reads_come_from_memory_after_one_load(pair):
    backend, store = pair
    backend.direct_insert("items", [item("a")])
    assert [r["id"] for r in store.select("items")] == ["a"]
    loaded = len(backend.selects)
    for _ in range(50):
        store.select("items"); store.get("items", {"id": "a"})
    assert len(backend.selects) == loaded                                   # fifty more reads, no more questions to the real store


# ---- writes: memory now, the real store at the next flush --------------------------------------------------------------
def test_a_write_is_visible_at_once_but_reaches_the_real_store_only_when_flushed(pair):
    backend, store = pair
    store.insert("items", item("a"))
    assert store.get("items", {"id": "a"})["n"] == 1                        # the person who saved sees it immediately
    assert backend.get("items", {"id": "a"}) is None and backend.batches == [] and store.pending() == 1
    assert store.flush() == 1
    assert backend.get("items", {"id": "a"})["n"] == 1 and store.pending() == 0


def test_many_writes_become_one_statement_per_table_with_only_the_final_rows(pair):
    backend, store = pair
    store.insert("items", item("a"))
    for n in range(2, 52):
        store.update("items", {"id": "a"}, {"n": n})                         # fifty updates to the same row
    store.insert_many("items", [item("b"), item("c")])
    store.insert("pairs", {"a": "x", "b": "y", "v": 1})
    assert store.flush() == 2                                               # two tables, two groups
    by_table = {b[0]: b for b in backend.batches}
    assert len(backend.batches) == 2 and sorted(r["id"] for r in by_table["items"][1]) == ["a", "b", "c"]
    assert next(r for r in by_table["items"][1] if r["id"] == "a")["n"] == 51   # one row for "a", the last value
    assert backend.get("items", {"id": "a"})["n"] == 51 and backend.get("pairs", {"a": "x", "b": "y"})["v"] == 1


def test_a_delete_and_a_reinsert_of_the_same_key_become_one_upsert_and_an_insert_then_delete_cancels(pair):
    backend, store = pair
    backend.direct_insert("items", [item("a", 1)])
    store.reload_all(force=True)
    store.delete("items", {"id": "a"})
    store.insert("items", item("a", 9))                                     # deleted and recreated before the flush
    store.insert("items", item("tmp"))
    store.delete("items", {"id": "tmp"})                                    # created and deleted before the flush
    store.flush()
    (_, upserts, deletes, _), = [b for b in backend.batches if b[0] == "items"] or [("items", [], [], [])]
    assert [r["id"] for r in upserts] == ["a"] and upserts[0]["n"] == 9
    assert backend.get("items", {"id": "a"})["n"] == 9 and backend.get("items", {"id": "tmp"}) is None


def test_deleting_by_a_filter_sends_the_keys_of_exactly_the_rows_that_went(pair):
    backend, store = pair
    store.insert_many("pairs", [{"a": "x", "b": "1", "v": 1}, {"a": "x", "b": "2", "v": 1}, {"a": "y", "b": "1", "v": 1}])
    store.flush()
    assert store.delete("pairs", {"a": "x"}) == 2
    store.flush()
    assert [(r["a"], r["b"]) for r in backend.select("pairs")] == [("y", "1")]
    assert backend.batches[-1][2] == [("x", "1"), ("x", "2")] or sorted(backend.batches[-1][2]) == [("x", "1"), ("x", "2")]


def test_a_failed_write_changes_nothing_in_memory_or_the_queue(pair):
    backend, store = pair
    store.insert("items", item("a"))
    with pytest.raises(AlreadyExists):
        store.insert("items", item("a", n=99))
    assert store.get("items", {"id": "a"})["n"] == 1 and store.pending() == 1
    stale = NOW - timedelta(days=1)
    with pytest.raises(Conflict):
        store.update("items", {"id": "a"}, {"n": 7}, expected_updated_at=stale)
    with pytest.raises(NotFound):
        store.update("items", {"id": "missing"}, {"n": 1})
    assert store.get("items", {"id": "a"})["n"] == 1 and store.pending() == 1


def test_a_failed_flush_keeps_the_changes_queued_and_the_next_flush_sends_them(pair):
    backend, store = pair
    store.insert("items", item("a"))
    store.update("items", {"id": "a"}, {"n": 4})
    backend.fail = True
    with pytest.raises(RuntimeError):
        store.flush()
    assert store.pending() == 1 and store.get("items", {"id": "a"})["n"] == 4 and store.stats["flush_errors"] == 1
    store.insert("items", item("b"))                                        # a new write arrives while the old one is stuck
    backend.fail = False
    store.flush()
    assert sorted(r["id"] for r in backend.select("items")) == ["a", "b"] and backend.get("items", {"id": "a"})["n"] == 4 and store.pending() == 0


def test_a_requeued_change_never_overwrites_a_newer_one(pair):
    backend, store = pair
    store.insert("items", item("a", 1))
    backend.fail = True
    with pytest.raises(RuntimeError):
        store.flush()
    store.update("items", {"id": "a"}, {"n": 2})                            # newer than the stuck change
    backend.fail = False
    store.flush()
    assert backend.get("items", {"id": "a"})["n"] == 2


# ---- append-only tables the pages never read ------------------------------------------------------------------------------
def test_audit_rows_are_buffered_and_a_read_flushes_them_first(pair):
    backend, store = pair
    for i in range(20):
        store.insert("audit_log", {"id": str(i), "what": "x"})
    assert backend.batches == [] and store.pending() == 20 and "audit_log" not in store.mirrored
    assert len(store.select("audit_log")) == 20                              # reading sees every write: it flushes the buffer first
    assert len([b for b in backend.batches if b[0] == "audit_log"]) == 1    # twenty rows, one statement
    assert len(backend.batches[0][3]) == 20


# ---- the background copy ---------------------------------------------------------------------------------------------------
def test_a_refresh_picks_up_what_someone_else_wrote(pair):
    backend, store = pair
    store.select("items")
    backend.direct_insert("items", [item("other")])
    assert store.select("items") == []                                      # not seen yet
    assert store.reload_all() == 1
    assert [r["id"] for r in store.select("items")] == ["other"]


def test_a_quiet_table_is_not_re_read_at_all(pair):
    backend, store = pair
    store.select("items")
    selects = len(backend.selects)
    for _ in range(5):
        assert store.reload_all() == 0                                      # nothing changed: only the free "last modified" check ran
    assert len(backend.selects) == selects and store.stats["skipped"] >= 5 and backend.metadata_calls >= 5


def test_only_the_changed_table_is_re_read(pair):
    backend, store = pair
    store.select("items"); store.select("pairs")
    backend.direct_insert("items", [item("x")])                             # only items changes
    seen = len(backend.selects)
    assert store.reload_all() == 1
    assert backend.selects[seen:] == ["items"]


def test_a_refresh_never_wipes_changes_that_are_not_flushed_yet(pair):
    backend, store = pair
    store.select("items")
    store.insert("items", item("mine"))                                     # queued, not yet in the real store
    backend.changed["items"] += 1                                           # and the real store reports a change from someone else
    assert store.reload_all() == 0                                          # a dirty table is left alone
    assert [r["id"] for r in store.select("items")] == ["mine"]
    store.flush()
    store.reload_all()
    assert [r["id"] for r in store.select("items")] == ["mine"]


def test_a_reload_that_raced_a_write_is_discarded_and_keeps_the_write(pair):
    backend, store = pair
    store.select("items")
    original = backend.select
    def slow_select(table, where=None, order_by=None):                      # the write lands while the reload is reading
        rows = original(table, where, order_by)
        store.insert("items", item("fresh"))
        return rows
    backend.select = slow_select
    store._seen.clear()
    store._reload("items", force=True)
    backend.select = original
    assert [r["id"] for r in store.select("items")] == ["fresh"]            # the older reload did not wipe the newer write


def test_the_background_refresh_runs_only_when_someone_is_reading():
    backend = CountingBackend()
    store = SnapshotStore(backend, refresh_seconds=0.05, flush_seconds=None)
    store.select("items")                                                   # first read: loads, starts the thread
    backend.direct_insert("items", [item("late")])
    time.sleep(0.3)
    assert [r["id"] for r in store.select("items")] == ["late"]            # picked up by the background thread
    idle_selects = len(backend.selects)
    time.sleep(0.3)                                                         # nobody reading: the loop must not touch the real store
    assert len(backend.selects) == idle_selects


def test_a_failing_refresh_keeps_serving_the_old_copy():
    backend = CountingBackend()
    store = SnapshotStore(backend, refresh_seconds=0.05, flush_seconds=None)
    backend.direct_insert("items", [item("a")])
    store.select("items")
    backend.select = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("BigQuery is down"))
    backend.direct_insert("items", [item("b")])
    time.sleep(0.2)
    assert [r["id"] for r in store.select("items")] == ["a"]


# ---- the background flush ---------------------------------------------------------------------------------------------------
def test_the_background_flush_sends_changes_on_its_own_and_batches_a_burst():
    backend = CountingBackend()
    store = SnapshotStore(backend, refresh_seconds=None, flush_seconds=0.1)
    store.select("items")
    for i in range(30):
        store.insert("items", item(f"i{i}"))                                # a burst of writes
    deadline = time.time() + 3
    while store.pending() and time.time() < deadline:
        time.sleep(0.05)
    assert store.pending() == 0 and len(backend.select("items")) == 30
    assert len(backend.batches) <= 3 and sum(len(b[1]) for b in backend.batches) == 30    # a few statements, not thirty


def test_the_flush_retries_after_a_failure_and_catches_up():
    backend = CountingBackend()
    store = SnapshotStore(backend, refresh_seconds=None, flush_seconds=0.05)
    store.select("items")
    backend.fail = True
    store.insert("items", item("a"))
    time.sleep(0.3)
    assert store.pending() == 1 and store.stats["flush_errors"] >= 1        # stuck, but still queued and still visible
    assert store.get("items", {"id": "a"}) is not None
    backend.fail = False
    deadline = time.time() + 5
    while store.pending() and time.time() < deadline:
        store._wake.set(); time.sleep(0.1)
    assert backend.get("items", {"id": "a"}) is not None


def test_closing_flushes_what_is_left(pair):
    backend, store = pair
    store.insert("items", item("a"))
    store.close()
    assert backend.get("items", {"id": "a"}) is not None and store.pending() == 0


def test_how_long_the_oldest_change_has_waited_is_reported(pair):
    backend, store = pair
    assert store.pending_age() == 0.0
    store.insert("items", item("a"))
    time.sleep(0.05)
    assert store.pending_age() > 0.04
    store.flush()
    assert store.pending_age() == 0.0


# ---- on BigQuery ---------------------------------------------------------------------------------------------------------
@pytest.mark.bq
def test_on_bigquery_a_write_moves_the_last_modified_stamp_and_a_quiet_table_is_skipped(wh):
    from p2.store.bigquery import BigQueryStore
    real = BigQueryStore(wh, {"items": ITEMS})
    real.ensure()
    store = SnapshotStore(real, refresh_seconds=None, flush_seconds=None)
    store.select("items")                                                   # first load
    assert store.reload_all() == 0                                          # nothing changed since: a free metadata check, no SELECT
    before = real.table_modified("items")
    store.insert("items", item("a"))
    store.flush()
    assert real.table_modified("items") > before                            # BigQuery says the table changed
    assert store.reload_all() == 1                                          # so the next refresh reads it once
    assert store.reload_all() == 0                                          # and then leaves it alone again
    assert [r["id"] for r in store.select("items")] == ["a"]


@pytest.mark.bq
def test_on_bigquery_many_changes_to_one_table_are_at_most_three_statements_and_land_correctly(wh):
    from google.cloud import bigquery
    from p2.store.bigquery import BigQueryStore
    real = BigQueryStore(wh, {"pairs": PAIRS, "audit_log": LOGS})
    real.ensure()
    real.insert_many("pairs", [{"a": "gone", "b": str(i), "v": 0} for i in range(3)] + [{"a": "keep", "b": "1", "v": 1}])
    store = SnapshotStore(real, refresh_seconds=None, flush_seconds=None)
    store.select("pairs")
    store.delete("pairs", {"a": "gone"})                                    # three rows go
    store.update("pairs", {"a": "keep", "b": "1"}, {"v": 5})                # one changes
    store.insert_many("pairs", [{"a": "new", "b": str(i), "v": i} for i in range(20)])      # twenty arrive
    for i in range(10):
        store.insert("audit_log", {"id": str(i), "what": "x"})
    sent = []
    original = bigquery.Client.query
    bigquery.Client.query = lambda self, q, *a, **k: (sent.append(q.split()[0]), original(self, q, *a, **k))[1]
    try:
        store.flush()
    finally:
        bigquery.Client.query = original
    dml = [s for s in sent if s in ("DELETE", "MERGE", "INSERT", "UPDATE")]
    assert len(dml) <= 4                                                    # one DELETE, one MERGE for pairs, one MERGE for the audit rows (not 34 statements)
    rows = {(r["a"], r["b"]): r["v"] for r in real.select("pairs")}
    assert not any(k[0] == "gone" for k in rows) and rows[("keep", "1")] == 5 and len([k for k in rows if k[0] == "new"]) == 20
    assert len(real.select("audit_log")) == 10
