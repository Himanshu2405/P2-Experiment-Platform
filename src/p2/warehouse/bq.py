"""Thin BigQuery wrapper: four datasets per environment, parameterised queries, landing loads."""
import os
import re
from datetime import date, datetime

import pandas as pd
from google.cloud import bigquery

from p2.warehouse.sources import SourceRegistry, create_table_sql, load_sources, resolve_logical_names, staging_view_sql

DEFAULT_PROJECT = "master-chariot-413216"
LOCATION = "US"


def _param(name: str, value) -> bigquery.ScalarQueryParameter:
    if isinstance(value, bool):
        return bigquery.ScalarQueryParameter(name, "BOOL", value)
    if isinstance(value, int):
        return bigquery.ScalarQueryParameter(name, "INT64", value)
    if isinstance(value, float):
        return bigquery.ScalarQueryParameter(name, "FLOAT64", value)
    if isinstance(value, datetime):
        return bigquery.ScalarQueryParameter(name, "TIMESTAMP", value)
    if isinstance(value, date):
        return bigquery.ScalarQueryParameter(name, "DATE", value)
    return bigquery.ScalarQueryParameter(name, "STRING", str(value))


def _to_utc(df: pd.DataFrame) -> pd.DataFrame:
    """Naive datetime columns are UTC by convention in the simulator; BigQuery needs them tz-aware and at
    microsecond precision."""
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[col]):
            out[col] = out[col].dt.floor("us").astype("datetime64[us]") if out[col].dt.tz is None else out[col].dt.floor("us")
            if out[col].dt.tz is None:
                out[col] = out[col].dt.tz_localize("UTC")
    return out


class Warehouse:
    """One environment (dev, test_xxx, prod) = four datasets: raw (landed), staging (cleaned views),
    analytics (built per experiment), app (application state)."""

    def __init__(self, env: str | None = None, project: str | None = None, sources: SourceRegistry | None = None):
        self.env = env or os.environ.get("P2_ENV", "dev")
        if not re.fullmatch(r"[a-z0-9_]+", self.env):
            raise ValueError("env must be lowercase letters, digits and underscores")
        self.project = project or os.environ.get("P2_GCP_PROJECT", DEFAULT_PROJECT)
        self.client = bigquery.Client(project=self.project)
        self.sources = sources or load_sources()
        base = f"{self.project}.p2_{self.env}"
        self.raw, self.staging, self.analytics, self.app = (f"{base}_{x}" for x in ("raw", "staging", "analytics", "app"))

    def ensure_dataset(self, dataset: str) -> None:
        d = bigquery.Dataset(dataset)
        d.location = LOCATION
        self.client.create_dataset(d, exists_ok=True)

    def ensure(self) -> None:
        """Create datasets, raw tables, and staging views from the source registry. Safe to repeat."""
        for ds in (self.raw, self.staging, self.analytics, self.app):
            self.ensure_dataset(ds)
        for src in self.sources:
            self.execute(create_table_sql(src, self.raw))
        for src in self.sources:
            self.execute(staging_view_sql(src, self.raw, self.staging))

    def drop_all(self) -> None:
        for ds in (self.raw, self.staging, self.analytics, self.app):
            self.client.delete_dataset(ds, delete_contents=True, not_found_ok=True)

    def resolve(self, sql: str, allowed: set[str] | None = None) -> str:
        """Turn `{{ source }}` placeholders in author SQL into this environment's staging views."""
        return resolve_logical_names(sql, self.sources, self.staging, allowed)

    def _config(self, params: dict | None) -> bigquery.QueryJobConfig:
        return bigquery.QueryJobConfig(query_parameters=[_param(k, v) for k, v in (params or {}).items()])

    def execute(self, sql: str, params: dict | None = None) -> None:
        self.client.query(sql, job_config=self._config(params)).result()

    def query_df(self, sql: str, params: dict | None = None) -> pd.DataFrame:
        return self.client.query(sql, job_config=self._config(params)).to_dataframe()

    def replace_rows(self, table: str, df: pd.DataFrame, delete_where: str, params: dict | None = None) -> None:
        """Landing load that is safe to repeat: delete the slice being reloaded, then append."""
        src = self.sources.get(table)
        missing = set(src.column_names) - set(df.columns)
        if missing:
            raise ValueError(f"{table}: missing columns {sorted(missing)}")
        full = f"{self.raw}.{table}"
        self.execute(f"DELETE FROM `{full}` WHERE {delete_where}", params)
        if df.empty:
            return
        job = self.client.load_table_from_dataframe(
            _to_utc(df[src.column_names]), full, job_config=bigquery.LoadJobConfig(write_disposition="WRITE_APPEND"))
        job.result()
