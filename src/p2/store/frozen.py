"""A frozen copy of the app tables, for the read-only public demo.

It is dumped once from the real store and loaded into a MemoryStore, so the demo needs no BigQuery and no credentials. The file holds
only the app's own tables (experiments, plans, saved results, run logs): summary numbers on synthetic data, no user-level rows.

    python -m p2.store.frozen            # dump the dev dataset's app tables to demo/app_tables.json.gz
"""
import gzip
import json
import sys
from datetime import date, datetime
from pathlib import Path

from p2.store.base import Store
from p2.store.memory import MemoryStore
from p2.store.schema import APP_TABLES

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "demo" / "app_tables.json.gz"


def _encode(t: str, value):
    if value is None:
        return None
    return value.isoformat() if t in ("TIMESTAMP", "DATE") else value


def _decode(t: str, value):
    if value is None:
        return None
    if t == "TIMESTAMP":
        return datetime.fromisoformat(value)
    if t == "DATE":
        return date.fromisoformat(value)
    if t == "FLOAT64":
        return float(value)
    return value


def dump(store: Store, path: Path = DEFAULT_PATH) -> dict[str, int]:
    """Write every app table of `store` to a gzipped JSON file. Returns the row count per table."""
    out = {}
    for name, t in store.tables.items():
        out[name] = [{c.name: _encode(c.type, r.get(c.name)) for c in t.columns} for r in store.select(name)]
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(out, f, sort_keys=True)
    return {name: len(rows) for name, rows in out.items()}


def load(path: Path = DEFAULT_PATH) -> MemoryStore:
    """A MemoryStore filled from a file written by `dump`. Tables missing from the file start empty."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        data = json.load(f)
    store = MemoryStore(APP_TABLES)
    for name, t in APP_TABLES.items():
        types = {c.name: c.type for c in t.columns}
        store.replace_table(name, [{k: _decode(types[k], v) for k, v in r.items() if k in types} for r in data.get(name, [])])
    return store


if __name__ == "__main__":
    from p2.store.bigquery import BigQueryStore
    from p2.warehouse.bq import Warehouse

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH
    for table, n in dump(BigQueryStore(Warehouse(env="dev")), target).items():
        print(f"{table}: {n} rows")
    print(f"wrote {target}")
