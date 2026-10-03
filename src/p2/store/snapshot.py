"""A Store that keeps the application tables in memory so pages open instantly, and sends changes to the real store in the background.

Reads come from an in-memory copy. A write is applied to the copy at once, so the person who saved sees it immediately and never waits
for BigQuery (each BigQuery write takes about 2 to 3 seconds). The net effect of the writes since the last flush is then sent to the
real store, which stays the source of truth, as one combined statement per table: fifty updates to the same row become one row sent.
Only one flush runs at a time, so a crowd of people cannot overrun the real store's limit on simultaneous writes.

A background thread also re-reads tables now and then to pick up other people's changes, only while someone is using the app. Before
reading a table it asks the real store whether the table changed (a free metadata call on BigQuery), so a quiet table costs nothing.
Append-only tables that pages do not read (the audit log and job steps) are not copied: their new rows are buffered and flushed, and a
read of one of them flushes its buffer first so it still sees every write.

If a flush fails (for example BigQuery is unreachable) the changes stay queued and are retried with a growing pause; `pending()` says
how many are waiting. Whatever is still queued is flushed when the process exits normally.
"""
import atexit
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from p2.store.base import Store
from p2.store.memory import MemoryStore

log = logging.getLogger(__name__)
PASS_THROUGH = ("audit_log", "job_steps")      # append-only and not read by pages: buffered, not copied


class SnapshotStore(Store):
    def __init__(self, backend: Store, refresh_seconds: float | None = 60.0, flush_seconds: float | None = 2.0,
                 pass_through: tuple[str, ...] = PASS_THROUGH, workers: int = 3):
        """`refresh_seconds` None turns the background re-read off and `flush_seconds` None turns the background flush off; tests then call
        reload_all() and flush() themselves. With the flush off, nothing reaches the real store until flush() is called."""
        super().__init__(backend.tables, backend.clock)
        self.backend = backend
        self.refresh_seconds = refresh_seconds
        self.flush_seconds = flush_seconds
        self.pass_through = set(pass_through)
        self.mirrored = [n for n in self.tables if n not in self.pass_through]
        self.workers = workers
        self.mirror = MemoryStore(self.tables, backend.clock)
        self._lock = threading.RLock()                   # guards the copy and the queues
        self._load_lock = threading.Lock()
        self._flush_lock = threading.Lock()              # one flush at a time
        self._version = {n: 0 for n in self.mirrored}    # bumped by every write, so a reload that raced a write is thrown away
        self._seen = {}                                  # table -> the "last modified" stamp of the copy we hold
        self._up: dict[str, dict[tuple, dict]] = {n: {} for n in self.mirrored}      # rows to write: the latest full row by key
        self._del: dict[str, set[tuple]] = {n: set() for n in self.mirrored}         # keys to delete
        self._append: dict[str, list[dict]] = {n: [] for n in self.pass_through}     # new rows of append-only tables
        self._flushing: set[str] = set()
        self._queued_since: float | None = None
        self._loaded = False
        self._active = False                             # set by reads, cleared by the refresh loop: no readers, no refresh
        self._refresh_thread: threading.Thread | None = None
        self._flush_thread: threading.Thread | None = None
        self._wake = threading.Event()
        self.stats = {"reloads": 0, "skipped": 0, "flushes": 0, "statements": 0, "flush_errors": 0}
        atexit.register(self.close)

    # ---- loading ----------------------------------------------------------------------------
    def ensure(self) -> None:
        self.backend.ensure()

    def _stamp(self, table: str):
        fn = getattr(self.backend, "table_modified", None)
        if fn is None:
            return None
        try:
            return fn(table)
        except Exception:                                # cannot tell: fall back to reading the table (correct, just not free)
            log.warning("could not read the last-modified time of %s", table, exc_info=True)
            return None

    def _dirty(self, table: str) -> bool:
        return bool(self._up[table] or self._del[table] or table in self._flushing)

    def _reload(self, table: str, force: bool = False) -> bool:
        with self._lock:
            if self._dirty(table):                       # our own changes are not in the real store yet; they win until flushed
                return False
        stamp = self._stamp(table)
        if not force and stamp is not None and stamp == self._seen.get(table):
            self.stats["skipped"] += 1
            return False
        before = self._version[table]
        rows = self.backend.select(table)
        with self._lock:
            if self._version[table] != before or self._dirty(table):   # someone wrote while we were reading: keep their newer copy
                return False
            self.mirror.replace_table(table, rows)
            self._seen[table] = stamp
        self.stats["reloads"] += 1
        return True

    def reload_all(self, force: bool = False) -> int:
        """Re-read every copied table that changed (all of them when `force`). Returns how many were re-read."""
        with ThreadPoolExecutor(self.workers) as pool:
            return sum(pool.map(lambda n: self._reload(n, force), self.mirrored))

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            self.reload_all(force=True)
            self._loaded = True
            if self.refresh_seconds and self._refresh_thread is None:
                self._refresh_thread = threading.Thread(target=self._refresh_loop, name="snapshot-refresh", daemon=True)
                self._refresh_thread.start()
            if self.flush_seconds and self._flush_thread is None:
                self._flush_thread = threading.Thread(target=self._flush_loop, name="snapshot-flush", daemon=True)
                self._flush_thread.start()

    def _refresh_loop(self) -> None:
        while True:
            time.sleep(self.refresh_seconds)
            if not self._active:
                continue
            self._active = False
            try:
                self.reload_all()
            except Exception:                            # a failed refresh keeps serving the old copy and tries again next time
                log.exception("snapshot refresh failed")

    # ---- flushing: net effect, one statement per table -----------------------------------------------
    def pending(self) -> int:
        """How many changes are queued and not yet in the real store."""
        with self._lock:
            return (sum(len(v) for v in self._up.values()) + sum(len(v) for v in self._del.values())
                    + sum(len(v) for v in self._append.values()) + len(self._flushing))

    def pending_age(self) -> float:
        """Seconds the oldest queued change has been waiting (0 when nothing is queued)."""
        with self._lock:
            return 0.0 if self._queued_since is None or not self.pending() else time.time() - self._queued_since

    def _take(self, table: str):
        if table in self.pass_through:
            rows, self._append[table] = self._append[table], []
            return [], [], rows
        up, self._up[table] = self._up[table], {}
        dele, self._del[table] = self._del[table], set()
        return list(up.values()), list(dele), []

    def _requeue(self, table: str, upserts, deletes, appends) -> None:
        """A flush failed: put the changes back, unless newer changes to the same key arrived meanwhile."""
        with self._lock:
            if table in self.pass_through:
                self._append[table] = list(appends) + self._append[table]
                return
            t = self.table(table)
            for r in upserts:
                key = tuple(r[k] for k in t.key)
                if key not in self._up[table] and key not in self._del[table]:
                    self._up[table][key] = r
            for key in deletes:
                if key not in self._up[table] and key not in self._del[table]:
                    self._del[table].add(key)

    def flush(self, tables: list[str] | None = None, parallel: bool = True) -> int:
        """Send the queued changes (of the given tables, or all) to the real store now. Returns how many tables were sent; raises the
        first error after re-queuing what failed."""
        with self._flush_lock:
            with self._lock:
                work = {}
                for table in tables or [*self.mirrored, *self.pass_through]:
                    taken = self._take(table)
                    if any(taken):
                        work[table] = taken
                        self._flushing.add(table)
                if not work:
                    self._queued_since = None
                    return 0
            errors = []

            def send(item):
                table, (up, dele, app) = item
                try:
                    self.backend.write_batch(table, upserts=up, deletes=dele, appends=app)
                    with self._lock:
                        self.stats["statements"] += (1 if up else 0) + (1 if dele else 0) + (1 if app else 0)
                except Exception as e:
                    errors.append(e)
                    self._requeue(table, up, dele, app)
                finally:
                    with self._lock:
                        self._flushing.discard(table)

            if parallel and len(work) > 1:
                with ThreadPoolExecutor(self.workers) as pool:
                    list(pool.map(send, work.items()))
            else:                                        # one at a time (also at process exit, when new threads can no longer be started)
                for item in work.items():
                    send(item)
            with self._lock:
                self.stats["flushes"] += 1
                if not self.pending():
                    self._queued_since = None
            if errors:
                self.stats["flush_errors"] += 1
                raise errors[0]
            return len(work)

    def _flush_loop(self) -> None:
        pause = self.flush_seconds
        while True:
            self._wake.wait(pause)
            self._wake.clear()
            if not self.pending():
                pause = self.flush_seconds
                continue
            try:
                self.flush()
                pause = self.flush_seconds
            except Exception:                            # keep the changes queued and try again, waiting longer each time up to a minute
                log.exception("flush to the real store failed; %d changes are queued", self.pending())
                pause = min(60.0, pause * 2)

    def _flush_table(self, table: str) -> None:
        """Make sure a table's changes are in the real store before reading it from there."""
        if self._append.get(table) or table in self._flushing:
            try:
                self.flush([table])
            except Exception:
                log.exception("flush before reading %s failed", table)

    def close(self) -> None:
        """Flush whatever is queued (also runs when the process exits normally)."""
        try:
            if self.pending():
                self.flush(parallel=False)
        except Exception:
            log.exception("final flush failed; %d changes were not saved", self.pending())

    def _queued(self) -> None:
        if self._queued_since is None:
            self._queued_since = time.time()
        self._wake.set()

    # ---- reads: from memory ---------------------------------------------------------------------
    def get(self, table: str, key: dict) -> dict | None:
        if table in self.pass_through:
            self._flush_table(table)
            return self.backend.get(table, key)
        self._ensure_loaded()
        self._active = True
        with self._lock:
            return self.mirror.get(table, key)

    def select(self, table: str, where: dict | None = None, order_by: list[str] | None = None) -> list[dict]:
        if table in self.pass_through:
            self._flush_table(table)
            return self.backend.select(table, where, order_by)
        self._ensure_loaded()
        self._active = True
        with self._lock:
            return self.mirror.select(table, where, order_by)

    # ---- writes: into the copy now, into the real store on the next flush ---------------------------------
    def _keys(self, table: str, rows: list[dict]) -> list[tuple]:
        t = self.table(table)
        return [tuple(r[k] for k in t.key) for r in rows]

    def _put(self, table: str, rows: list[dict]) -> None:
        for key, r in zip(self._keys(table, rows), rows):
            self._del[table].discard(key)
            self._up[table][key] = self.mirror.get(table, dict(zip(self.table(table).key, key)))   # the validated row as stored

    def insert(self, table: str, row: dict) -> None:
        self.insert_many(table, [row])

    def insert_many(self, table: str, rows: list[dict]) -> None:
        if not rows:
            return
        if table in self.pass_through:
            t = self.table(table)
            clean = [self._validate_row(t, r) for r in rows]
            with self._lock:
                self._append[table].extend(clean)
                self._queued()
            return
        self._ensure_loaded()
        with self._lock:
            self.mirror.insert_many(table, rows)         # raises AlreadyExists, changing nothing
            self._version[table] += 1
            self._put(table, rows)
            self._queued()

    def insert_missing(self, table: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        if table in self.pass_through:
            self.insert_many(table, rows)
            return len(rows)
        self._ensure_loaded()
        with self._lock:
            keys = self._keys(table, rows)
            fresh = [r for k, r in zip(keys, rows) if self.mirror.get(table, dict(zip(self.table(table).key, k))) is None]
            added = self.mirror.insert_missing(table, rows)
            if added:
                self._version[table] += 1
                self._put(table, fresh)
                self._queued()
            return added

    def update(self, table: str, key: dict, changes: dict, expected_updated_at: datetime | None = None) -> dict:
        self._ensure_loaded()
        with self._lock:
            row = self.mirror.update(table, key, changes, expected_updated_at)   # raises NotFound or Conflict, changing nothing
            self._version[table] += 1
            self._put(table, [row])
            self._queued()
            return row

    def delete(self, table: str, where: dict) -> int:
        self._ensure_loaded()
        with self._lock:
            t = self.table(table)
            gone = self._keys(table, self.mirror.select(table, where))
            count = self.mirror.delete(table, where)
            if count:
                self._version[table] += 1
                for key in gone:
                    self._up[table].pop(key, None)
                    self._del[table].add(key)
                self._queued()
            return count
