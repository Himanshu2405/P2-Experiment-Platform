"""Generate SCHEMA.md: the schema and data dictionary for every table the platform uses.

Raw sources come from warehouse/sources.yaml, application tables from store/schema.py and store/docs.py.
Run `python -m p2.schema_doc` after changing any of them; a test fails when SCHEMA.md is out of date.
"""
from pathlib import Path

from p2.store.base import TableDef
from p2.store.docs import APP_TABLE_DOCS
from p2.store.schema import APP_TABLES
from p2.warehouse.sources import SourceDef, load_sources

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "SCHEMA.md"
INGESTED_AT = "When this row landed in the warehouse (UTC); breaks ties when de-duplicating."


def _e(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _table(rows: list[tuple[str, ...]], header: tuple[str, ...]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(_e(c) for c in r) + " |" for r in rows]
    return out


def _raw_section(src: SourceDef) -> list[str]:
    facts = [
        f"- Product: {src.product_id}",
        f"- Time column: `{src.time_column}`",
        f"- Partitioned by: {'`' + src.partition + '`' if src.partition else 'not partitioned (small table)'}",
        f"- Clustered by: {', '.join('`' + c + '`' for c in src.cluster) if src.cluster else 'none'}",
        f"- One logical row per: {', '.join('`' + c + '`' for c in src.dedupe_key)}; the winner is the first row by `{src.dedupe_order}`",
        f"- Staging view: `{src.staging_name}` (what `{{{{ {src.source_id} }}}}` resolves to in SQL)",
    ]
    cols = [(f"`{c.name}`", c.type, "yes" if c.nullable else "no", c.description) for c in src.columns]
    cols.append(("`ingested_at`", "TIMESTAMP", "no", INGESTED_AT))
    return [f"### `{src.source_id}`", "", src.description, "", *facts, "", *_table(cols, ("Column", "Type", "Null", "Description")), ""]


def _app_section(t: TableDef) -> list[str]:
    summary, cols = APP_TABLE_DOCS[t.name]
    key = ", ".join(f"`{k}`" for k in t.key)
    rows = [(f"`{c.name}`", c.type, "yes" if c.nullable else "no", cols[c.name]) for c in t.columns]
    return [f"### `{t.name}`", "", summary, "", f"- Key: {key}", "", *_table(rows, ("Column", "Type", "Null", "Description")), ""]


def render() -> str:
    sources = list(load_sources())
    L: list[str] = [
        "# Schema and Data Dictionary",
        "",
        "Generated file. Do not edit by hand. Change `src/p2/warehouse/sources.yaml` (raw tables) or `src/p2/store/docs.py` and "
        "`src/p2/store/schema.py` (application tables), then run `python -m p2.schema_doc`. A test fails if this file is out of date "
        "or any column lacks a description.",
        "",
        "Related: [DATA_SOURCES.md](DATA_SOURCES.md) explains how the sources are used; [TECH_SPEC.md](TECH_SPEC.md) explains the platform.",
        "",
        "## 1. How the data is organised",
        "",
        "Everything lives in BigQuery, project `master-chariot-413216`, location US. One environment is four datasets, named "
        "`p2_<env>_<layer>` (for example `p2_dev_raw`). Tests create and drop `p2_test_*` environments.",
        "",
        *_table([
            ("`raw`", "The nine source tables exactly as the company's systems land them, including duplicates and late updates.", "Simulator (plays the company's systems)"),
            ("`staging`", "One de-duplicated view per raw table. All SQL written in the catalog reads these, never raw.", "Generated from `sources.yaml`"),
            ("`analytics`", "Per-experiment build tables and baseline tables created by the pipeline.", "Pipeline"),
            ("`app`", "Application state: team, products, catalog, experiments, audit log.", "Service layer"),
        ], ("Dataset", "What it holds", "Written by")),
        "",
        "Flow: raw tables, then staging views, then catalog item SQL reads the views, then the pipeline writes analytics tables, "
        "then the app reads them. All timestamps are UTC.",
        "",
        "## 2. Raw sources (9 tables)",
        "",
        *_table([(f"[`{s.source_id}`](#{s.source_id})", s.product_id, s.description) for s in sources],
                ("Table", "Product", "What it includes")),
        "",
    ]
    for s in sources:
        L += _raw_section(s)
    L += [
        "## 3. Staging views (9 views)",
        "",
        "Each view keeps one row per logical key. The winner is decided by the table's de-duplication order, so retries and "
        "superseded versions disappear before any metric sees them.",
        "",
        *_table([(f"`{s.staging_name}`", f"`{s.source_id}`", ", ".join(f"`{k}`" for k in s.dedupe_key), f"`{s.dedupe_order}`") for s in sources],
                ("View", "Built from", "One row per", "Winner is the first by")),
        "",
        "## 4. Analytics tables (generated per experiment)",
        "",
        "Created by the pipeline in the `analytics` dataset. Names use the experiment id with hyphens turned into underscores. "
        "Intermediate tables expire after 7 days; the final table does not. There is no fixed schema for the final table: it is "
        "generated from the items an experiment selected.",
        "",
        *_table([
            ("`exp_<id>__cohort`", "`user_id`, `arm`, `assigned_date`", "Everyone assigned to the experiment, one row per user (first assignment wins)."),
            ("`exp_<id>__attr_<item>`", "`user_id`, `value`", "One filter or dimension resolved as of each user's assignment date, with its default when no row applies."),
            ("`exp_<id>__audience`", "`user_id`, `arm`, `assigned_date`", "Cohort users who pass every selected filter."),
            ("`exp_<id>__m_<item>`", "`user_id`, `value`", "One metric per user over assigned date through six days later; 0 when there is no activity."),
            ("`exp_<id>__final`", "`user_id`, `arm`, `assigned_date`, one column per dimension, one column per metric", "What analysis reads. Columns are named by catalog item id; dimensions are STRING, binary metrics INT64, continuous metrics FLOAT64."),
            ("`base_<product>__cohort`, `__m_<item>`, `__final`", "same shapes, arm is `baseline`", "Historic cohort used to compute baselines; expires after 1 day."),
        ], ("Table", "Columns", "What it is")),
        "",
        "## 5. Query contracts for catalog items",
        "",
        "Every metric, dimension and filter is one SQL query with a fixed output shape.",
        "",
        *_table([
            ("metric", "`user_id` STRING, `metric_date` DATE, `value` number", "One row per user per day; days with no row count as 0. Binary metrics return 1 and aggregate with max; continuous are non-negative and aggregate with sum."),
            ("dimension", "`user_id` STRING, `effective_date` DATE, `value` STRING", "History rows; the latest on or before the assignment date applies, else the item's default. At most 20 distinct values."),
            ("filter", "`user_id` STRING, `effective_date` DATE, `value` INT64 (0 or 1)", "Same as a dimension; users with no row at entry count as 0 and are excluded."),
        ], ("Kind", "Columns returned", "Meaning")),
        "",
        f"## 6. Application tables ({len(APP_TABLES)} tables)",
        "",
        *_table([(f"[`{t.name}`](#{t.name})", APP_TABLE_DOCS[t.name][0]) for t in APP_TABLES.values()], ("Table", "What it includes")),
        "",
    ]
    for t in APP_TABLES.values():
        L += _app_section(t)
    L += [
        "## 7. Key relationships",
        "",
        "There are no enforced foreign keys in BigQuery; the service layer checks these.",
        "",
        "- `users.user_id` is the identity used by every source table and every pipeline table.",
        "- `email_events.send_id` refers to `email_sends.send_id`.",
        "- `exp_assignments.experiment_id` equals `experiments.experiment_id` in the app.",
        "- `experiment_items (item_id, item_version)` refers to `catalog_items (item_id, version)`.",
        "- `catalog_items.product_id` and `experiments.product_id` refer to `products.product_id`; `products.owner_user_id` and "
        "`experiments.owner_user_id` refer to `team_members.user_id`.",
        "- `job_runs.experiment_id` refers to `experiments`; `job_steps.job_id` refers to `job_runs`.",
        "- `results`, `daily_stats`, `segment_results` and `sim_ground_truth` are keyed by experiment and metric.",
        "",
        "## 8. Conventions and simulated data",
        "",
        "- Ids: experiment ids use lowercase letters, digits and hyphens; catalog item ids use lowercase letters, digits and underscores "
        "and become column names, so `user_id`, `arm`, `assigned_date`, `value`, `metric_date` and `effective_date` are reserved.",
        "- The data is synthetic. Row ids carry a prefix so a simulation can be replaced exactly: `uni-` for the shared universe, "
        "`hist-<product>-` for baseline-period activity, `<experiment id>-` for an experiment's activity.",
        "- Planted mess in raw tables, all removed by the staging views or the whole-day window: duplicate rows, orders first landed as "
        "completed then updated to cancelled or refunded, activity before assignment or after the 7-day window, duplicated assignments, "
        "and missing attributes (country, region, industry, plan history).",
        "- The measurement window is whole days: the assignment date and the six days after it.",
        "",
    ]
    return "\n".join(L)


def main() -> None:
    SCHEMA_PATH.write_text(render())
    print(f"wrote {SCHEMA_PATH}")


if __name__ == "__main__":
    main()
