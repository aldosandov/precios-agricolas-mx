"""The raw table's DDL has to match what the ingest writes (ALD-22).

The loader is `bq load` over NDJSON, so a column the pipeline emits and the
table does not declare fails the load, and a column declared but never written
lands silently as NULL for the whole history. Both are cheap to prevent here
and expensive to notice in BigQuery.

Reads the committed DDL as text: there is no BigQuery connection in the suite.
"""

import re
from pathlib import Path

import pytest

from scraper.pipelines.ndjson import RAW_SCHEMA

DDL_PATH = Path(__file__).resolve().parent.parent / "transform" / "raw" / "prices_table.sql"


@pytest.fixture(scope="module")
def ddl() -> str:
    return DDL_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def columns(ddl: str) -> list[tuple[str, str]]:
    """Every (name, type) declared in the CREATE TABLE body, in order."""
    body = ddl.split("CREATE TABLE", 1)[1].split("(", 1)[1].split("\nPARTITION BY", 1)[0]
    return re.findall(r"^\s{2}(\w+)\s+([A-Z0-9<>_ ]+?)(?:\s+NOT NULL)?(?:\s+OPTIONS|,|$)", body, re.M)


def test_the_table_declares_exactly_what_the_pipeline_writes(columns):
    assert [name for name, _ in columns] == list(RAW_SCHEMA)


def test_the_date_is_a_date_and_not_a_string(columns):
    """Partitioning and every window comparison downstream depend on it."""
    assert dict(columns)["fecha"] == "DATE"


def test_prices_are_exact_and_not_floating_point(columns):
    """Money in FLOAT64 drifts on aggregation; the source publishes two decimals."""
    types = dict(columns)
    assert types["precio_min"] == types["precio_max"] == types["precio_frecuente"] == "NUMERIC"


def test_quality_flags_stay_a_repeated_field(columns):
    assert dict(columns)["banderas_calidad"] == "ARRAY<STRING>"


def test_the_extraction_timestamp_is_a_timestamp(columns):
    assert dict(columns)["extraido_en"] == "TIMESTAMP"


# --- what §9.2 asks for: partitioned by day, clustered by product and market ---


def test_the_table_is_partitioned_by_day(ddl):
    assert re.search(r"^PARTITION BY fecha$", ddl, re.M)


def test_the_table_is_clustered_by_product_and_market(ddl):
    match = re.search(r"^CLUSTER BY (.+)$", ddl, re.M)

    assert match
    clustered = [name.strip() for name in match.group(1).split(",")]
    assert clustered[:2] == ["producto_raw", "destino_id"]


# --- what the contract guarantees, declared as such ---


def test_the_identifying_fields_are_declared_not_null(ddl):
    for field in ("fecha", "presentacion_raw", "tipo_precio", "llave_fila"):
        assert re.search(rf"^\s{{2}}{field}\s+\w+\s+NOT NULL", ddl, re.M), field


def test_the_labels_a_pinned_query_erases_stay_nullable(ddl):
    """The sweep pins the market, so destino_raw is filled from the catalog;
    pin something else and product or origin arrive empty instead."""
    for field in ("producto_raw", "calidad_raw", "origen_raw", "destino_raw"):
        assert not re.search(rf"^\s{{2}}{field}\s+\w+\s+NOT NULL", ddl, re.M), field
