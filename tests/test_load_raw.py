"""Loading NDJSON into the raw layer (ALD-23).

The load is an upsert on the natural key (PRD §8.8): running the same day
twice must not duplicate anything, and reprocessing a month must not dirty the
history. Replacing whole partitions would be simpler but wrong — the sweep
writes one market at a time, so truncating a day to load market 210 would drop
the other 48 already in it.

These tests are offline: they check the plan and the SQL. The live upsert is
in tests/test_load_raw_live.py.
"""

from datetime import date

import pytest

from scraper.pipelines.ndjson import RAW_SCHEMA
from transform.load import (
    RAW_TABLE,
    build_merge_sql,
    partition_dates,
    partition_files,
)

# --- what gets loaded ---


def test_only_ndjson_partitions_are_picked_up(tmp_path):
    (tmp_path / "fecha=2026-08-03").mkdir()
    (tmp_path / "fecha=2026-08-03" / "destino=210_precio=kilogramo_calculado.ndjson").touch()
    (tmp_path / "fecha=2026-08-03" / "notas.txt").touch()

    assert [path.name for path in partition_files(tmp_path)] == [
        "destino=210_precio=kilogramo_calculado.ndjson"
    ]


def test_the_dates_come_from_the_partition_directories(tmp_path):
    for day in ("2026-08-03", "2026-08-05"):
        (tmp_path / f"fecha={day}").mkdir()
        (tmp_path / f"fecha={day}" / "destino=210_precio=kilogramo_calculado.ndjson").touch()

    assert partition_dates(partition_files(tmp_path)) == [
        date(2026, 8, 3),
        date(2026, 8, 5),
    ]


def test_a_file_outside_a_dated_partition_stops_the_load(tmp_path):
    """Without a date there is nothing to prune by, and a full-table MERGE on
    15 million rows is not something to do by accident."""
    (tmp_path / "suelto.ndjson").touch()

    with pytest.raises(ValueError, match="fecha="):
        partition_dates([tmp_path / "suelto.ndjson"])


# --- the MERGE ---


@pytest.fixture
def merge_sql() -> str:
    return build_merge_sql(
        target=RAW_TABLE,
        staging="proyecto.crudo._paso_abc",
        start=date(2026, 8, 3),
        end=date(2026, 8, 5),
    )


def test_the_merge_matches_on_the_row_key(merge_sql):
    assert "T.llave_fila = S.llave_fila" in merge_sql


def test_the_merge_is_pruned_to_the_dates_being_loaded(merge_sql):
    """Without this the MERGE scans every partition of the whole history."""
    assert "T.fecha BETWEEN DATE '2026-08-03' AND DATE '2026-08-05'" in merge_sql


def test_the_merge_writes_every_column_of_the_schema(merge_sql):
    updates, inserts = merge_sql.split("WHEN NOT MATCHED", 1)

    for column in RAW_SCHEMA:
        assert f"{column} = S.{column}" in updates, column
        assert column in inserts, column


def test_a_row_already_loaded_is_updated_and_not_inserted_again(merge_sql):
    assert "WHEN MATCHED THEN UPDATE" in merge_sql
    assert "WHEN NOT MATCHED THEN INSERT" in merge_sql


def test_the_merge_targets_the_raw_table(merge_sql):
    assert f"MERGE `{RAW_TABLE}` T" in merge_sql
