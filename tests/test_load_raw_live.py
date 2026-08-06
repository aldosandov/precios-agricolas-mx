"""Live upsert against BigQuery: the same file twice must not duplicate (ALD-23).

This is the criterion of the issue and it cannot be faked offline — the whole
question is what BigQuery does with a MERGE, not what the SQL string says. It
runs against a throwaway table in the real dataset, which it drops afterwards.

Skips when there are no credentials, so the offline suite stays runnable.
"""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from scraper.contract import QueryContext, build_records
from scraper.fixtures import FIXTURES_DIR
from scraper.parsers.results import parse_results
from scraper.pipelines.ndjson import to_raw_rows, write_partitions
from scraper.query import ALL, DateWindow
from transform.load import RAW_TABLE, load, partition_files

PROJECT, DATASET, _ = RAW_TABLE.split(".")


@pytest.fixture(scope="module")
def client():
    bigquery = pytest.importorskip("google.cloud.bigquery")
    try:
        return bigquery.Client(project=PROJECT)
    except Exception as error:  # missing or expired credentials
        pytest.skip(f"sin credenciales de BigQuery: {error}")


@pytest.fixture(scope="module")
def table(client):
    """A copy of the raw table's shape, dropped when the module finishes."""
    from google.cloud import bigquery

    table_id = f"{PROJECT}.{DATASET}._prueba_carga_{uuid.uuid4().hex[:8]}"
    definition = bigquery.Table(table_id, schema=client.get_table(RAW_TABLE).schema)
    definition.time_partitioning = bigquery.TimePartitioning(field="fecha")
    definition.expires = datetime.now(timezone.utc) + timedelta(hours=1)
    created = client.create_table(definition)
    yield str(created.reference)
    client.delete_table(table_id, not_found_ok=True)


@pytest.fixture(scope="module")
def partitions(tmp_path_factory):
    """Real rows from the ALD-13 fixture, written as the spider would."""
    html = (FIXTURES_DIR / "range_destination_fixed.html").read_bytes().decode("utf-8")
    records = build_records(
        parse_results(html),
        QueryContext(
            product_id=ALL,
            origin_id=ALL,
            destination_id="210",
            prices_per_id="2",
            window=DateWindow(date(2026, 7, 1), date(2026, 7, 3)),
            source_url="https://example.test/results",
            fetched_at=datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc),
        ),
    )
    out = tmp_path_factory.mktemp("raw")
    write_partitions(to_raw_rows(records), out)
    return partition_files(out)


def _count(client, table: str) -> tuple[int, int]:
    row = next(
        iter(
            client.query(
                f"SELECT COUNT(*) filas, COUNT(DISTINCT llave_fila) llaves "
                f"FROM `{table}`"
            ).result()
        )
    )
    return row.filas, row.llaves


def test_loading_a_days_ndjson_inserts_one_row_per_line(client, table, partitions):
    lines = sum(
        1 for path in partitions for line in path.read_text("utf-8").splitlines() if line
    )

    report = load(partitions, target=table, client=client)

    assert report.affected_rows == lines
    assert _count(client, table) == (lines, lines)


def test_loading_the_same_files_again_updates_instead_of_duplicating(
    client, table, partitions
):
    before = _count(client, table)

    load(partitions, target=table, client=client)

    assert _count(client, table) == before


def test_a_revised_price_overwrites_the_one_already_loaded(client, table, partitions):
    """§8.8 says insert *or update*: the SNIIM revises past prices, and a
    reload has to carry the correction instead of keeping the stale row."""
    key = next(
        iter(client.query(f"SELECT llave_fila FROM `{table}` LIMIT 1").result())
    ).llave_fila
    client.query(
        f"UPDATE `{table}` SET precio_frecuente = 999.99 WHERE llave_fila = '{key}'"
    ).result()

    load(partitions, target=table, client=client)

    revised = next(
        iter(
            client.query(
                f"SELECT precio_frecuente FROM `{table}` WHERE llave_fila = '{key}'"
            ).result()
        )
    ).precio_frecuente
    assert revised != 999.99


def test_no_staging_tables_are_left_behind(client, table):
    leftovers = [
        t.table_id
        for t in client.list_tables(f"{PROJECT}.{DATASET}")
        if t.table_id.startswith("_paso_")
    ]

    assert leftovers == []
