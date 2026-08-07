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
from types import SimpleNamespace

import pytest

from scraper.pipelines.ndjson import RAW_SCHEMA
from transform.load import (
    RAW_TABLE,
    LoadReport,
    build_merge_sql,
    by_year,
    count_rows,
    load,
    partition_dates,
    partition_files,
    purge,
)


def _partition(root, day: str, rows: int = 0, name: str = "destino=210_precio=k.ndjson"):
    directory = root / f"fecha={day}"
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_text(f'{{"fecha": "{day}"}}\n' * rows, encoding="utf-8")
    return path


def _report(staged: int) -> LoadReport:
    return LoadReport(
        files=0, staged_rows=staged, affected_rows=staged, table=RAW_TABLE, dates=()
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


# --- loading a slice of the disk, and clearing it (ALD-55) ---


def test_a_date_range_selects_only_its_partitions(tmp_path):
    """The backfill loads a quarter at a time so the MERGE prunes to that
    quarter instead of scanning the history."""
    for day in ("2011-06-30", "2011-07-01", "2011-09-30", "2011-10-01"):
        _partition(tmp_path, day)

    picked = partition_files(tmp_path, since=date(2011, 7, 1), until=date(2011, 9, 30))

    assert partition_dates(picked) == [date(2011, 7, 1), date(2011, 9, 30)]


def test_no_range_still_means_everything(tmp_path):
    _partition(tmp_path, "2011-06-30")
    _partition(tmp_path, "2011-10-01")

    assert len(partition_files(tmp_path)) == 2


def test_rows_are_counted_from_the_files_and_not_from_their_names(tmp_path):
    paths = [_partition(tmp_path, "2011-07-01", rows=3), _partition(tmp_path, "2011-07-04", rows=5)]

    assert count_rows(paths) == 8


def test_purging_removes_the_partitions_and_their_empty_days(tmp_path):
    """25.3 GB of NDJSON for the whole history, on a personal laptop. What is
    already in BigQuery does not stay here."""
    paths = [_partition(tmp_path, "2011-07-01", rows=4), _partition(tmp_path, "2011-07-04", rows=6)]

    assert purge(paths, _report(10)) == 2
    assert not any(path.exists() for path in paths)
    assert list(tmp_path.iterdir()) == []


def test_a_day_that_still_holds_something_else_is_not_removed(tmp_path):
    loaded = _partition(tmp_path, "2011-07-01", rows=2)
    other = _partition(tmp_path, "2011-07-01", rows=1, name="destino=100_precio=k.ndjson")

    purge([loaded], _report(2))

    assert other.exists()
    assert loaded.parent.exists()


def test_nothing_is_deleted_when_the_counts_do_not_match(tmp_path):
    """A load job that skipped rows and one that took them all look the same
    from outside. The difference is the data this would erase from the only
    other place it exists."""
    paths = [_partition(tmp_path, "2011-07-01", rows=9)]

    with pytest.raises(ValueError, match="no se purga"):
        purge(paths, _report(7))

    assert paths[0].exists()


# --- one load job per batch, not one per file ---


class FakeBigQuery:
    """Enough of a client to see how many load jobs a batch costs."""

    # Signatures match google-cloud-bigquery's, unused arguments and all: this
    # stands in for the real client, so it has to be callable the same way.

    def __init__(self, staged_rows: int):
        self.staged_rows = staged_rows
        self.loaded: list[bytes] = []

    def get_table(self, ref):  # noqa: ARG002
        return SimpleNamespace(schema=[], num_rows=self.staged_rows)

    def create_table(self, table):
        return table

    def load_table_from_file(self, handle, ref, job_config):  # noqa: ARG002
        self.loaded.append(handle.read())
        return SimpleNamespace(result=lambda: None)

    def query(self, sql):
        self.sql = sql
        return SimpleNamespace(result=lambda: None, num_dml_affected_rows=self.staged_rows)

    def delete_table(self, ref, not_found_ok=False):  # noqa: ARG002
        self.dropped = ref


def test_a_batch_costs_one_load_job_however_many_files_it_has(tmp_path):
    """BigQuery allows 1 500 load jobs per table per day. A quarter of the
    backfill is ~180 partitions, so per file the 116 quarters would be ~21 000
    jobs and would hit the quota on the first day. Measured before the fix:
    198 jobs for half a year of one market."""
    paths = [_partition(tmp_path, f"2011-07-{day:02d}", rows=2) for day in range(1, 11)]
    client = FakeBigQuery(staged_rows=20)

    report = load(paths, client=client, staging_dir=tmp_path)

    assert len(client.loaded) == 1
    assert report.files == 10


def test_the_batch_carries_every_line_of_every_file(tmp_path):
    paths = [_partition(tmp_path, "2011-07-01", rows=3), _partition(tmp_path, "2011-07-04", rows=4)]
    client = FakeBigQuery(staged_rows=7)

    load(paths, client=client, staging_dir=tmp_path)

    assert client.loaded[0].count(b"\n") == 7


def test_the_merge_is_pruned_to_the_dates_actually_loaded(tmp_path):
    paths = [_partition(tmp_path, "2011-07-01", rows=1), _partition(tmp_path, "2011-09-30", rows=1)]
    client = FakeBigQuery(staged_rows=2)

    load(paths, client=client, staging_dir=tmp_path)

    assert "DATE '2011-07-01' AND DATE '2011-09-30'" in client.sql


# --- the batch is a calendar year (ALD-57) ---


def test_partitions_are_grouped_by_year_oldest_first(tmp_path):
    """A block never crosses the year, so a year loaded is a set of blocks that
    can retire together."""
    for day in ("2012-01-02", "2011-12-31", "2011-01-01"):
        _partition(tmp_path, day)

    years = by_year(partition_files(tmp_path))

    assert list(years) == [2011, 2012]
    assert len(years[2011]) == 2
    assert len(years[2012]) == 1


def test_a_year_costs_one_load_job(tmp_path):
    """29 load jobs for the whole history, against 116 per quarter and ~21 000
    per file."""
    paths = [_partition(tmp_path, f"2011-{month:02d}-01", rows=2) for month in range(1, 13)]
    client = FakeBigQuery(staged_rows=24)

    load(by_year(paths)[2011], client=client, staging_dir=tmp_path)

    assert len(client.loaded) == 1
    assert "DATE '2011-01-01' AND DATE '2011-12-01'" in client.sql
