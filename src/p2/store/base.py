"""The Store interface: small transactional tables for application state.

Two implementations share one contract (and one test suite): MemoryStore for fast offline tests and
BigQueryStore for real use. Only what the platform needs is here: insert, get, select by equality,
update with an optional optimistic check, and delete with a required filter.
"""
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable

TYPES = ("STRING", "INT64", "FLOAT64", "BOOL", "TIMESTAMP", "DATE", "JSON")


class StoreError(Exception):
    pass


class AlreadyExists(StoreError):
    pass


class NotFound(StoreError):
    pass


class Conflict(StoreError):
    """The row changed since the caller read it (optimistic concurrency check failed)."""


@dataclass(frozen=True)
class Col:
    name: str
    type: str
    nullable: bool = False

    def __post_init__(self):
        if self.type not in TYPES:
            raise ValueError(f"unknown column type {self.type}")


@dataclass(frozen=True)
class TableDef:
    name: str
    columns: tuple[Col, ...]
    key: tuple[str, ...]

    def __post_init__(self):
        names = [c.name for c in self.columns]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.name}: duplicate columns")
        if not self.key or any(k not in names for k in self.key):
            raise ValueError(f"{self.name}: key must name existing columns")
        if any(self.col(k).nullable for k in self.key):
            raise ValueError(f"{self.name}: key columns cannot be nullable")

    def col(self, name: str) -> Col:
        for c in self.columns:
            if c.name == name:
                return c
        raise ValueError(f"{self.name}: unknown column {name}")

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.columns]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _check(col: Col, value: Any) -> None:
    t = col.type
    ok = (
        (t == "STRING" and isinstance(value, str))
        or (t == "INT64" and isinstance(value, int) and not isinstance(value, bool))
        or (t == "FLOAT64" and isinstance(value, (int, float)) and not isinstance(value, bool))
        or (t == "BOOL" and isinstance(value, bool))
        or (t == "TIMESTAMP" and isinstance(value, datetime) and value.tzinfo is not None)
        or (t == "DATE" and isinstance(value, date) and not isinstance(value, datetime))
        or t == "JSON"
    )
    if not ok:
        hint = " (timestamps must be timezone-aware)" if t == "TIMESTAMP" else ""
        raise ValueError(f"column {col.name} expects {t}, got {type(value).__name__}{hint}")
    if t == "JSON":
        try:
            json.dumps(value)
        except TypeError as e:
            raise ValueError(f"column {col.name}: value is not JSON-serialisable") from e


class Store(ABC):
    def __init__(self, tables: dict[str, TableDef], clock: Callable[[], datetime] = utcnow):
        self.tables = tables
        self.clock = clock

    # ---- shared validation -------------------------------------------------------------------
    def table(self, name: str) -> TableDef:
        try:
            return self.tables[name]
        except KeyError:
            raise ValueError(f"unknown table: {name}") from None

    def _validate_row(self, t: TableDef, row: dict) -> dict:
        unknown = set(row) - set(t.names)
        if unknown:
            raise ValueError(f"{t.name}: unknown columns {sorted(unknown)}")
        out = {}
        for c in t.columns:
            v = row.get(c.name)
            if v is None:
                if not c.nullable:
                    raise ValueError(f"{t.name}.{c.name} is required")
                out[c.name] = None
            else:
                _check(c, v)
                out[c.name] = v
        return out

    def _key_of(self, t: TableDef, key: dict) -> tuple:
        if set(key) != set(t.key):
            raise ValueError(f"{t.name}: key must be exactly {list(t.key)}")
        for k in t.key:
            _check(t.col(k), key[k])
        return tuple(key[k] for k in t.key)

    def _validate_where(self, t: TableDef, where: dict | None) -> dict:
        where = where or {}
        for k, v in where.items():
            col = t.col(k)
            if v is not None:
                _check(col, v)
        return where

    def _prepare_update(self, t: TableDef, key: dict, changes: dict) -> dict:
        self._key_of(t, key)
        if not changes:
            raise ValueError("no changes given")
        if set(changes) & set(t.key):
            raise ValueError(f"{t.name}: key columns cannot be changed")
        out = dict(changes)
        if "updated_at" in t.names and "updated_at" not in out:
            out["updated_at"] = self.clock()
        for k, v in out.items():
            col = t.col(k)
            if v is None:
                if not col.nullable:
                    raise ValueError(f"{t.name}.{k} cannot be null")
            else:
                _check(col, v)
        return out

    def _parse_order(self, t: TableDef, order_by: list[str] | None) -> list[tuple[str, bool]]:
        parsed = []
        for item in order_by or []:
            parts = item.split()
            if len(parts) not in (1, 2) or (len(parts) == 2 and parts[1].upper() not in ("ASC", "DESC")):
                raise ValueError(f"bad order_by: {item}")
            t.col(parts[0])
            parsed.append((parts[0], len(parts) == 2 and parts[1].upper() == "DESC"))
        return parsed

    # ---- the contract ------------------------------------------------------------------------
    @abstractmethod
    def insert(self, table: str, row: dict) -> None:
        """Insert one row. Raises AlreadyExists if the key is taken."""

    @abstractmethod
    def insert_many(self, table: str, rows: list[dict]) -> None:
        """Insert several rows atomically: if any key is taken (or repeated), nothing is inserted."""

    @abstractmethod
    def insert_missing(self, table: str, rows: list[dict]) -> int:
        """Insert only the rows whose key is absent and return how many were inserted. Existing rows are left
        untouched. On BigQuery this is one MERGE, so two processes seeding at once cannot both insert."""

    @abstractmethod
    def get(self, table: str, key: dict) -> dict | None: ...

    @abstractmethod
    def select(self, table: str, where: dict | None = None, order_by: list[str] | None = None) -> list[dict]:
        """Rows matching all equality conditions (None means IS NULL), in the requested order."""

    @abstractmethod
    def update(self, table: str, key: dict, changes: dict, expected_updated_at: datetime | None = None) -> dict:
        """Change columns of one row and return it. `updated_at` is set automatically when the table has it.
        With `expected_updated_at`, raises Conflict if the stored value differs. Raises NotFound."""

    @abstractmethod
    def delete(self, table: str, where: dict) -> int:
        """Delete rows matching a non-empty filter; returns how many."""

    def write_batch(self, table: str, upserts=(), deletes=(), appends=()) -> None:
        """Apply the net effect of many writes to one table in as few statements as the store can manage: delete the rows with these key
        tuples, upsert these full rows (insert, or overwrite by key), and add these append-only rows (existing keys are left alone).
        The snapshot store sends its buffered writes this way. This default does it one row at a time; BigQueryStore overrides it."""
        t = self.table(table)
        for key in deletes:
            self.delete(table, dict(zip(t.key, key)))
        for r in upserts:
            key = {k: r[k] for k in t.key}
            if self.get(table, key) is None:
                self.insert(table, r)
            else:
                self.update(table, key, {c: v for c, v in r.items() if c not in t.key})
        if appends:
            self.insert_missing(table, list(appends))

