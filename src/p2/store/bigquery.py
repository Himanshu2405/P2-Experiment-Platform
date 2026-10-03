"""BigQuery Store: application state in the environment's `app` dataset.

Every statement is parameterised. Uniqueness is enforced with MERGE ... WHEN NOT MATCHED, because BigQuery
has no enforced primary keys. Each call costs a round trip (about a second), so callers keep writes few.
"""
import json
from datetime import datetime

from google.cloud import bigquery

from p2.store.base import AlreadyExists, Col, Conflict, NotFound, Store, TableDef, utcnow
from p2.store.schema import APP_TABLES
from p2.warehouse.bq import Warehouse

_BQ_TYPE = {"STRING": "STRING", "INT64": "INT64", "FLOAT64": "FLOAT64", "BOOL": "BOOL",
            "TIMESTAMP": "TIMESTAMP", "DATE": "DATE", "JSON": "STRING"}


def _to_bq(col: Col, value):
    if col.type == "JSON" and value is not None:
        return json.dumps(value)
    if col.type == "FLOAT64" and value is not None:
        return float(value)
    return value


def _from_bq(col: Col, value):
    if value is None:
        return None
    if col.type == "JSON":
        return json.loads(value)
    return value


class BigQueryStore(Store):
    def __init__(self, wh: Warehouse, tables: dict[str, TableDef] | None = None, clock=utcnow):
        super().__init__(tables if tables is not None else APP_TABLES, clock)
        self.wh = wh
        self.dataset = wh.app

    # ---- helpers ----------------------------------------------------------------------------
    def _full(self, t: TableDef) -> str:
        return f"`{self.dataset}.{t.name}`"

    def _param(self, col: Col, name: str, value) -> bigquery.ScalarQueryParameter:
        return bigquery.ScalarQueryParameter(name, _BQ_TYPE[col.type], _to_bq(col, value))

    def _run(self, sql: str, params: list) -> bigquery.QueryJob:
        job = self.wh.client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
        job.result()
        return job

    def _where(self, t: TableDef, where: dict, prefix: str = "w") -> tuple[str, list]:
        clauses, params = [], []
        for i, (k, v) in enumerate(where.items()):
            if v is None:
                clauses.append(f"{k} IS NULL")
            else:
                clauses.append(f"{k} = @{prefix}{i}")
                params.append(self._param(t.col(k), f"{prefix}{i}", v))
        return " AND ".join(clauses) or "TRUE", params

    def _row(self, t: TableDef, r) -> dict:
        return {c.name: _from_bq(c, r[c.name]) for c in t.columns}

    def write_batch(self, table: str, upserts=(), deletes=(), appends=()) -> None:
        """At most one DELETE and one MERGE for the table, however many rows changed, plus one MERGE for append-only rows. Each BigQuery
        DML statement takes about 2 to 3 seconds and counts against the table's write limits, so fewer is better."""
        t = self.table(table)
        full = self._full(t)
        if deletes:
            keys = [bigquery.StructQueryParameter(None, *[self._param(t.col(k), k, v) for k, v in zip(t.key, key)]) for key in deletes]
            cond = " AND ".join(f"T.{k} = K.{k}" for k in t.key)
            self._run(f"DELETE FROM {full} T WHERE EXISTS (SELECT 1 FROM UNNEST(@keys) K WHERE {cond})",
                      [bigquery.ArrayQueryParameter("keys", "STRUCT", keys)])
        if upserts:
            rows = [self._validate_row(t, r) for r in upserts]
            structs = [bigquery.StructQueryParameter(None, *[self._param(c, c.name, r[c.name]) for c in t.columns]) for r in rows]
            on = " AND ".join(f"T.{k} = S.{k}" for k in t.key)
            sets = ", ".join(f"{c} = S.{c}" for c in t.names if c not in t.key)
            names = t.names
            matched = f"WHEN MATCHED THEN UPDATE SET {sets} " if sets else ""
            self._run(f"MERGE {full} T USING UNNEST(@rows) S ON {on} {matched}"
                      f"WHEN NOT MATCHED THEN INSERT ({', '.join(names)}) VALUES ({', '.join('S.' + n for n in names)})",
                      [bigquery.ArrayQueryParameter("rows", "STRUCT", structs)])
        if appends:
            self.insert_missing(table, list(appends))

    def table_modified(self, table: str):
        """When the table last changed, from table metadata. A metadata call is free and runs no query, so a snapshot can check it
        before deciding whether a (billed) SELECT is worth running."""
        return self.wh.client.get_table(self._full(self.table(table)).strip("`")).modified

    # ---- setup ------------------------------------------------------------------------------
    def ensure(self) -> None:
        """Create the application dataset and tables, and migrate existing tables to the current schema.

        Migration is additive and safe to repeat: a missing column is added (as nullable, the only mode BigQuery
        allows), and a column the code no longer writes has its NOT NULL dropped so inserts keep working.
        """
        self.wh.ensure_dataset(self.dataset)
        have = {t.table_id for t in self.wh.client.list_tables(self.dataset)}
        for t in self.tables.values():
            if t.name not in have:
                cols = ",\n  ".join(f"{c.name} {_BQ_TYPE[c.type]}{'' if c.nullable else ' NOT NULL'}" for c in t.columns)
                self.wh.execute(f"CREATE TABLE IF NOT EXISTS {self._full(t)} (\n  {cols}\n)")
                continue
            existing = {f.name: f for f in self.wh.client.get_table(f"{self.dataset}.{t.name}").schema}
            add = [c for c in t.columns if c.name not in existing]
            if add:
                self.wh.execute(f"ALTER TABLE {self._full(t)} " + ", ".join(
                    f"ADD COLUMN IF NOT EXISTS {c.name} {_BQ_TYPE[c.type]}" for c in add))
            for name, f in existing.items():
                if name not in t.names and f.mode == "REQUIRED":
                    self.wh.execute(f"ALTER TABLE {self._full(t)} ALTER COLUMN {name} DROP NOT NULL")

    # ---- the contract -----------------------------------------------------------------------
    def insert(self, table: str, row: dict) -> None:
        t = self.table(table)
        row = self._validate_row(t, row)
        names = t.names
        select = ", ".join(f"@p{i} AS {n}" for i, n in enumerate(names))
        on = " AND ".join(f"T.{k} = S.{k}" for k in t.key)
        sql = (f"MERGE {self._full(t)} T USING (SELECT {select}) S ON {on} "
               f"WHEN NOT MATCHED THEN INSERT ({', '.join(names)}) VALUES ({', '.join('S.' + n for n in names)})")
        job = self._run(sql, [self._param(t.col(n), f"p{i}", row[n]) for i, n in enumerate(names)])
        if not job.num_dml_affected_rows:
            raise AlreadyExists(f"{table}: key already exists: {tuple(row[k] for k in t.key)}")

    def insert_missing(self, table: str, rows: list[dict]) -> int:
        """One MERGE over an array of structs: rows whose key exists are skipped, the rest are inserted."""
        if not rows:
            return 0
        t = self.table(table)
        clean = [self._validate_row(t, r) for r in rows]
        keys = [tuple(r[k] for k in t.key) for r in clean]
        if len(set(keys)) != len(keys):
            raise AlreadyExists(f"{table}: repeated keys in the rows to insert")
        structs = [bigquery.StructQueryParameter(None, *[self._param(c, c.name, r[c.name]) for c in t.columns]) for r in clean]
        param = bigquery.ArrayQueryParameter("rows", "STRUCT", structs)
        names = t.names
        on = " AND ".join(f"T.{k} = S.{k}" for k in t.key)
        sql = (f"MERGE {self._full(t)} T USING UNNEST(@rows) S ON {on} "
               f"WHEN NOT MATCHED THEN INSERT ({', '.join(names)}) VALUES ({', '.join('S.' + n for n in names)})")
        return int(self._run(sql, [param]).num_dml_affected_rows or 0)

    def insert_many(self, table: str, rows: list[dict]) -> None:
        if not rows:
            return
        t = self.table(table)
        first = t.key[0]
        existing = self.select_in(t, first, sorted({r[first] for r in rows}))
        taken = [k for k in (tuple(r[c] for c in t.key) for r in rows) if k in existing]
        if taken:
            raise AlreadyExists(f"{table}: key already exists: {taken[0]}")
        if self.insert_missing(table, rows) != len(rows):  # only possible if someone inserted a key in the meantime
            raise AlreadyExists(f"{table}: a key was inserted by someone else while this insert ran")

    def select_in(self, t: TableDef, column: str, values: list) -> set[tuple]:
        """Keys of existing rows whose `column` is among `values` (used to pre-check bulk inserts)."""
        col = t.col(column)
        param = bigquery.ArrayQueryParameter("vals", _BQ_TYPE[col.type], [_to_bq(col, v) for v in values])
        rows = self.wh.client.query(
            f"SELECT {', '.join(t.key)} FROM {self._full(t)} WHERE {column} IN UNNEST(@vals)",
            job_config=bigquery.QueryJobConfig(query_parameters=[param])).result()
        return {tuple(r[k] for k in t.key) for r in rows}

    def get(self, table: str, key: dict) -> dict | None:
        t = self.table(table)
        self._key_of(t, key)
        rows = self.select(table, key)
        return rows[0] if rows else None

    def select(self, table: str, where: dict | None = None, order_by: list[str] | None = None) -> list[dict]:
        t = self.table(table)
        where = self._validate_where(t, where)
        clause, params = self._where(t, where)
        order = ", ".join(f"{c} {'DESC' if d else 'ASC'}" for c, d in self._parse_order(t, order_by))
        sql = f"SELECT * FROM {self._full(t)} WHERE {clause}" + (f" ORDER BY {order}" if order else "")
        rows = self.wh.client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()
        return [self._row(t, r) for r in rows]

    def update(self, table: str, key: dict, changes: dict, expected_updated_at: datetime | None = None) -> dict:
        t = self.table(table)
        changes = self._prepare_update(t, key, changes)
        sets, params = [], []
        for i, (k, v) in enumerate(changes.items()):
            sets.append(f"{k} = @s{i}")
            params.append(self._param(t.col(k), f"s{i}", v))
        clause, wparams = self._where(t, key, "k")
        params += wparams
        if expected_updated_at is not None:
            clause += " AND updated_at = @expected"
            params.append(bigquery.ScalarQueryParameter("expected", "TIMESTAMP", expected_updated_at))
        job = self._run(f"UPDATE {self._full(t)} SET {', '.join(sets)} WHERE {clause}", params)
        if not job.num_dml_affected_rows:
            if self.get(table, key) is None:
                raise NotFound(f"{table}: no row with key {tuple(key[k] for k in t.key)}")
            raise Conflict(f"{table}: the row was changed by someone else; reload and try again")
        return self.get(table, key)

    def delete(self, table: str, where: dict) -> int:
        t = self.table(table)
        if not where:
            raise ValueError("delete needs a non-empty filter")
        where = self._validate_where(t, where)
        clause, params = self._where(t, where)
        return int(self._run(f"DELETE FROM {self._full(t)} WHERE {clause}", params).num_dml_affected_rows or 0)
