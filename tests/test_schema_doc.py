"""SCHEMA.md is generated; these tests keep it complete and in sync with the code."""
import re

from p2.schema_doc import SCHEMA_PATH, render
from p2.store.docs import APP_TABLE_DOCS
from p2.store.schema import APP_TABLES
from p2.warehouse.sources import load_sources


def test_the_committed_file_is_up_to_date():
    assert SCHEMA_PATH.read_text() == render(), "SCHEMA.md is stale; run: python -m p2.schema_doc"


def test_every_raw_source_and_column_has_a_description():
    for s in load_sources():
        assert len(s.description) > 20, s.source_id
        for c in s.columns:
            assert c.description.strip(), f"{s.source_id}.{c.name} has no description"


def test_every_application_table_and_column_is_documented_exactly():
    assert set(APP_TABLE_DOCS) == set(APP_TABLES)
    for name, t in APP_TABLES.items():
        summary, cols = APP_TABLE_DOCS[name]
        assert summary.strip(), name
        assert set(cols) == set(t.names), f"{name}: documented columns differ from the schema"
        assert all(d.strip() for d in cols.values()), name


def test_the_file_has_a_section_and_every_column_for_each_table():
    text = SCHEMA_PATH.read_text()
    for s in load_sources():
        section = text.split(f"### `{s.source_id}`")[1].split("\n### ")[0]
        assert all(f"| `{c.name}` |" in section for c in s.columns) and "| `ingested_at` |" in section
    for name, t in APP_TABLES.items():
        section = text.split(f"### `{name}`")[1].split("\n### ")[0].split("\n## ")[0]
        assert all(f"| `{c}` |" in section for c in t.names), name
    assert len(re.findall(r"^### `", text, re.M)) == 9 + len(APP_TABLES)


def test_generated_text_avoids_em_dashes():
    assert "—" not in render()
