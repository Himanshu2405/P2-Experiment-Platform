"""Source registry: load and validate source definitions, generate raw DDL and staging views,
and resolve logical names (`{{ checkout_events }}`) in author SQL to the staging views."""
import re
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from jinja2 import Environment, StrictUndefined, TemplateSyntaxError, UndefinedError
from pydantic import BaseModel, Field, model_validator

NAME = r"^[a-z][a-z0-9_]*$"


class Column(BaseModel, frozen=True):
    name: str = Field(pattern=NAME)
    type: Literal["STRING", "DATE", "TIMESTAMP", "FLOAT64", "INT64"]
    nullable: bool = False
    description: str = ""  # the data dictionary entry; SCHEMA.md is generated from it


class SourceDef(BaseModel, frozen=True):
    source_id: str = Field(pattern=NAME)
    product_id: Literal["shared", "checkout", "email", "onboarding"]
    description: str = Field(min_length=1)
    user_column: str
    time_column: str
    dedupe_key: tuple[str, ...] = Field(min_length=1)
    dedupe_order: str = Field(min_length=1)
    partition: str | None = None
    cluster: tuple[str, ...] = ()
    columns: tuple[Column, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self):
        names = [c.name for c in self.columns]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.source_id}: duplicate column names")
        if "ingested_at" in names:
            raise ValueError(f"{self.source_id}: ingested_at is added automatically")
        known = set(names) | {"ingested_at"}
        for label, cols in (("user_column", [self.user_column]), ("time_column", [self.time_column]),
                            ("dedupe_key", self.dedupe_key), ("cluster", self.cluster)):
            missing = [c for c in cols if c not in known]
            if missing:
                raise ValueError(f"{self.source_id}: {label} refers to unknown columns {missing}")
        if len(self.cluster) > 4:
            raise ValueError(f"{self.source_id}: BigQuery allows at most 4 clustering columns")
        return self

    @property
    def staging_name(self) -> str:
        return f"stg_{self.source_id}"

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns] + ["ingested_at"]


class SourceRegistry:
    def __init__(self, sources: list[SourceDef]):
        ids = [s.source_id for s in sources]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate source_id")
        self._by_id = {s.source_id: s for s in sources}

    def __iter__(self):
        return iter(self._by_id.values())

    def ids(self) -> list[str]:
        return list(self._by_id)

    def get(self, source_id: str) -> SourceDef:
        try:
            return self._by_id[source_id]
        except KeyError:
            raise KeyError(f"unknown source: {source_id}") from None

    def for_product(self, product_id: str) -> list[SourceDef]:
        """Sources an author in this product may query: the product's own plus the shared ones."""
        return [s for s in self if s.product_id in (product_id, "shared")]


def load_sources(path: str | Path | None = None) -> SourceRegistry:
    text = Path(path).read_text() if path else resources.files("p2.warehouse").joinpath("sources.yaml").read_text()
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict) or not isinstance(raw.get("sources"), list):
        raise ValueError("source file must contain a top-level 'sources' list")
    return SourceRegistry([SourceDef(**s) for s in raw["sources"]])


def create_table_sql(src: SourceDef, raw: str) -> str:
    cols = [f"  {c.name} {c.type}{'' if c.nullable else ' NOT NULL'}" for c in src.columns]
    cols.append("  ingested_at TIMESTAMP NOT NULL")
    sql = f"CREATE TABLE IF NOT EXISTS `{raw}.{src.source_id}` (\n" + ",\n".join(cols) + "\n)"
    if src.partition:
        sql += f"\nPARTITION BY {src.partition}"
    if src.cluster:
        sql += f"\nCLUSTER BY {', '.join(src.cluster)}"
    return sql


def staging_view_sql(src: SourceDef, raw: str, staging: str) -> str:
    return (
        f"CREATE OR REPLACE VIEW `{staging}.{src.staging_name}` AS\n"
        f"SELECT * EXCEPT (rn) FROM (\n"
        f"  SELECT t.*, ROW_NUMBER() OVER (PARTITION BY {', '.join(src.dedupe_key)} ORDER BY {src.dedupe_order}) AS rn\n"
        f"  FROM `{raw}.{src.source_id}` t\n"
        f") WHERE rn = 1"
    )


_env = Environment(undefined=StrictUndefined)


def resolve_logical_names(sql: str, registry: SourceRegistry, staging: str, allowed: set[str] | None = None) -> str:
    """Replace `{{ source_id }}` with the staging view. Only plain names are allowed; anything else is rejected."""
    if "{%" in sql or "{#" in sql:
        raise ValueError("only {{ source_name }} placeholders are allowed in SQL")
    allowed = set(registry.ids()) if allowed is None else allowed
    used = set(re.findall(r"\{\{\s*([^}]*?)\s*\}\}", sql))
    bad = [u for u in used if not re.fullmatch(NAME[1:-1], u)]
    if bad:
        raise ValueError(f"invalid placeholder(s): {sorted(bad)}")
    unknown = sorted(u for u in used if u not in registry.ids())
    if unknown:
        raise ValueError(f"unknown source(s): {unknown}")
    forbidden = sorted(u for u in used if u not in allowed)
    if forbidden:
        raise ValueError(f"source(s) not available here: {forbidden}")
    mapping = {s.source_id: f"`{staging}.{s.staging_name}`" for s in registry}
    try:
        return _env.from_string(sql).render(**mapping)
    except (TemplateSyntaxError, UndefinedError) as e:  # defensive; the checks above should catch these
        raise ValueError(str(e)) from e
