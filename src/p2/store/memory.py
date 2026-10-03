"""In-memory Store for fast offline tests. Same contract as the BigQuery store."""
import copy
from datetime import datetime

from p2.store.base import AlreadyExists, Conflict, NotFound, Store, TableDef, utcnow


class MemoryStore(Store):
    def __init__(self, tables: dict[str, TableDef], clock=utcnow):
        super().__init__(tables, clock)
        self._rows: dict[str, dict[tuple, dict]] = {name: {} for name in tables}

    def ensure(self) -> None:  # parity with BigQueryStore
        return None

    def replace_table(self, table: str, rows: list[dict]) -> None:
        """Swap the whole table for `rows` (used by the snapshot store when it reloads from the real store)."""
        t = self.table(table)
        fresh = {}
        for r in rows:
            clean = self._validate_row(t, r)
            fresh[tuple(clean[k] for k in t.key)] = copy.deepcopy(clean)
        self._rows[table] = fresh

    def put(self, table: str, row: dict) -> None:
        """Insert or overwrite one row by key (used by the snapshot store to mirror a write that already succeeded elsewhere)."""
        t = self.table(table)
        clean = self._validate_row(t, row)
        self._rows[table][tuple(clean[k] for k in t.key)] = copy.deepcopy(clean)

    def insert(self, table: str, row: dict) -> None:
        self.insert_many(table, [row])

    def insert_many(self, table: str, rows: list[dict]) -> None:
        t = self.table(table)
        clean = [self._validate_row(t, r) for r in rows]
        keys = [tuple(r[k] for k in t.key) for r in clean]
        if len(set(keys)) != len(keys):
            raise AlreadyExists(f"{table}: repeated keys in the rows to insert")
        taken = [k for k in keys if k in self._rows[table]]
        if taken:
            raise AlreadyExists(f"{table}: key already exists: {taken[0]}")
        for k, r in zip(keys, clean):
            self._rows[table][k] = copy.deepcopy(r)

    def insert_missing(self, table: str, rows: list[dict]) -> int:
        t = self.table(table)
        clean = [self._validate_row(t, r) for r in rows]
        keys = [tuple(r[c] for c in t.key) for r in clean]
        if len(set(keys)) != len(keys):
            raise AlreadyExists(f"{table}: repeated keys in the rows to insert")
        added = 0
        for r in clean:
            k = tuple(r[c] for c in t.key)
            if k not in self._rows[table]:
                self._rows[table][k] = copy.deepcopy(r)
                added += 1
        return added

    def get(self, table: str, key: dict) -> dict | None:
        t = self.table(table)
        row = self._rows[table].get(self._key_of(t, key))
        return copy.deepcopy(row) if row else None

    def select(self, table: str, where: dict | None = None, order_by: list[str] | None = None) -> list[dict]:
        t = self.table(table)
        where = self._validate_where(t, where)
        rows = [r for r in self._rows[table].values() if all(r[k] == v for k, v in where.items())]
        for col, desc in reversed(self._parse_order(t, order_by)):
            rows.sort(key=lambda r: (r[col] is not None, r[col] if r[col] is not None else 0), reverse=desc)  # NULLs are smallest, as in BigQuery
        return copy.deepcopy(rows)

    def update(self, table: str, key: dict, changes: dict, expected_updated_at: datetime | None = None) -> dict:
        t = self.table(table)
        changes = self._prepare_update(t, key, changes)
        k = self._key_of(t, key)
        row = self._rows[table].get(k)
        if row is None:
            raise NotFound(f"{table}: no row with key {k}")
        if expected_updated_at is not None and row.get("updated_at") != expected_updated_at:
            raise Conflict(f"{table}: the row was changed by someone else; reload and try again")
        row.update(copy.deepcopy(changes))
        return copy.deepcopy(row)

    def delete(self, table: str, where: dict) -> int:
        t = self.table(table)
        if not where:
            raise ValueError("delete needs a non-empty filter")
        where = self._validate_where(t, where)
        doomed = [k for k, r in self._rows[table].items() if all(r[c] == v for c, v in where.items())]
        for k in doomed:
            del self._rows[table][k]
        return len(doomed)
