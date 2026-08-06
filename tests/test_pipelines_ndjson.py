"""The raw layer as it lands on disk: NDJSON partitioned by date (ALD-18).

Two things are pinned here. The line schema, because it becomes the column
list of the raw BigQuery table and §10 of the PRD is the contract. And the
partition layout, because `bq load` reads it and a rerun has to overwrite the
same file instead of appending a second copy of the same day.

Quality flags travel with the row and nothing is corrected: a price that is
zero, missing or unreadable is marked, not fixed and not dropped.

Runs offline against the ALD-13 fixtures.
"""

import json
from dataclasses import replace
from datetime import UTC, date, datetime

from scraper.contract import QueryContext, build_records
from scraper.fixtures import FIXTURES_DIR
from scraper.parsers.results import parse_results
from scraper.pipelines.ndjson import (
    RAW_SCHEMA,
    PartitionWriter,
    to_raw_rows,
    write_partitions,
)
from scraper.query import ALL, DateWindow

# The sweep pins the destination: 49 requests per block instead of 222, and
# the table keeps product, quality, presentation and origin (PRD §8.2).
PUEBLA = "210"

CONTEXT = QueryContext(
    product_id=ALL,
    origin_id=ALL,
    destination_id=PUEBLA,
    prices_per_id="2",
    window=DateWindow(date(2026, 7, 1), date(2026, 7, 3)),
    source_url="https://example.test/results",
    fetched_at=datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
)


def _records(context: QueryContext = CONTEXT):
    html = (FIXTURES_DIR / "range_destination_fixed.html").read_bytes().decode("utf-8")
    return build_records(parse_results(html), context)


# --- the line schema of §10 ---


def test_a_line_carries_exactly_the_fields_of_the_prd_schema():
    row = to_raw_rows(_records())[0]

    assert set(row) == set(RAW_SCHEMA)


def test_a_line_reads_back_as_the_row_the_source_published():
    row = to_raw_rows(_records())[0]

    assert row["fecha"] == "2026-07-01"
    assert row["producto_raw"] == "Aguacate Hass"
    assert row["calidad_raw"] == "Primera"
    assert row["presentacion_raw"] == "Caja de 10 kg."
    assert row["origen_raw"] == "Sinaloa"
    assert row["grupo_raw"] == "Frutas"
    assert row["precio_min"] == 50.0
    assert row["precio_max"] == 60.0
    assert row["precio_frecuente"] == 55.0
    assert row["observaciones"] is None
    assert row["banderas_calidad"] == []
    assert row["extraido_en"] == "2026-08-05T12:00:00+00:00"
    assert row["url_consulta"] == "https://example.test/results"


def test_the_pinned_destination_is_named_from_the_versioned_catalog():
    """The table drops the column the query pinned, but the raw row keeps both."""
    row = to_raw_rows(_records())[0]

    assert row["destino_id"] == 210
    assert row["destino_raw"] == "Puebla: Central de Abasto de Puebla"


def test_the_price_mode_is_written_as_a_name_and_not_as_the_id_that_was_sent():
    kilogram = to_raw_rows(_records())[0]
    commercial = to_raw_rows(_records(replace(CONTEXT, prices_per_id="1")))[0]

    assert kilogram["tipo_precio"] == "kilogramo_calculado"
    assert commercial["tipo_precio"] == "presentacion_comercial"


# --- the row key ---


def test_the_row_key_is_stable_across_runs():
    assert to_raw_rows(_records())[0]["llave_fila"] == to_raw_rows(_records())[0][
        "llave_fila"
    ]


def test_the_row_key_tells_the_two_price_modes_apart():
    """Same observation, different price: the modes must not collide."""
    kilogram = to_raw_rows(_records())[0]
    commercial = to_raw_rows(_records(replace(CONTEXT, prices_per_id="1")))[0]

    assert kilogram["llave_fila"] != commercial["llave_fila"]


# --- quality flags: marked, never corrected ---


def test_a_thousands_separator_does_not_make_a_price_unreadable():
    records = _records()
    row = to_raw_rows([replace(records[0], price_max="1,234.50")])[0]

    assert row["precio_max"] == 1234.50
    assert row["banderas_calidad"] == []


def test_prices_out_of_order_are_flagged_and_kept():
    records = _records()
    row = to_raw_rows([replace(records[0], price_min="99.00")])[0]

    assert "precios_incoherentes" in row["banderas_calidad"]
    assert row["precio_min"] == 99.0


def test_a_zero_price_is_flagged_and_kept():
    records = _records()
    row = to_raw_rows([replace(records[0], price_frequent="0.00")])[0]

    assert "precio_cero" in row["banderas_calidad"]
    assert row["precio_frecuente"] == 0.0


def test_an_unreadable_price_is_flagged_and_left_null():
    records = _records()
    row = to_raw_rows([replace(records[0], price_min="N/D")])[0]

    assert "precio_no_numerico" in row["banderas_calidad"]
    assert row["precio_min"] is None


def test_a_missing_price_is_flagged():
    records = _records()
    row = to_raw_rows([replace(records[0], price_frequent=None)])[0]

    assert "precio_ausente" in row["banderas_calidad"]


# --- partitioning ---


def test_rows_are_written_one_file_per_date(tmp_path):
    paths = write_partitions(to_raw_rows(_records()), tmp_path)

    assert sorted(path.parent.name for path in paths) == [
        "fecha=2026-07-01",
        "fecha=2026-07-02",
        "fecha=2026-07-03",
    ]


def test_every_line_is_one_json_object_of_its_own_partition(tmp_path):
    write_partitions(to_raw_rows(_records()), tmp_path)

    written = (tmp_path / "fecha=2026-07-01").glob("*.ndjson")
    lines = [json.loads(line) for path in written for line in path.read_text("utf-8").splitlines()]

    assert lines
    assert all(line["fecha"] == "2026-07-01" for line in lines)


def test_the_file_name_separates_market_and_price_mode(tmp_path):
    paths = write_partitions(to_raw_rows(_records()), tmp_path)

    assert paths[0].name == "destino=210_precio=kilogramo_calculado.ndjson"


def test_running_the_same_window_twice_does_not_duplicate_rows(tmp_path):
    """The load is idempotent by rewriting the partition, not by appending."""
    first = write_partitions(to_raw_rows(_records()), tmp_path)
    write_partitions(to_raw_rows(_records()), tmp_path)

    assert len(first[0].read_text("utf-8").splitlines()) == sum(
        1 for row in to_raw_rows(_records()) if row["fecha"] == "2026-07-01"
    )


def test_the_writer_truncates_a_partition_once_and_appends_after_that(tmp_path):
    """Scrapy hands rows over one at a time, but a rerun still has to replace
    the day instead of doubling it."""
    rows = to_raw_rows(_records())
    same_day = [row for row in rows if row["fecha"] == "2026-07-01"]

    for _ in range(2):
        writer = PartitionWriter(tmp_path)
        for row in same_day:
            writer.write(row)
        writer.close()

    written = list((tmp_path / "fecha=2026-07-01").glob("*.ndjson"))
    assert len(written) == 1
    assert len(written[0].read_text("utf-8").splitlines()) == len(same_day)


def test_the_writer_lands_the_same_files_as_the_batch_helper(tmp_path):
    rows = to_raw_rows(_records())
    batch = write_partitions(rows, tmp_path / "batch")

    writer = PartitionWriter(tmp_path / "stream")
    for row in rows:
        writer.write(row)
    writer.close()

    streamed = sorted((tmp_path / "stream").rglob("*.ndjson"))
    assert [path.relative_to(tmp_path / "stream") for path in streamed] == [
        path.relative_to(tmp_path / "batch") for path in sorted(batch)
    ]
    assert all(
        (tmp_path / "batch" / path.relative_to(tmp_path / "stream")).read_text("utf-8")
        == path.read_text("utf-8")
        for path in streamed
    )


def test_accents_are_written_as_utf8_and_not_escaped(tmp_path):
    """BigQuery reads UTF-8; escaping would only make the files unreadable."""
    paths = write_partitions(to_raw_rows(_records()), tmp_path)
    text = "".join(path.read_text("utf-8") for path in paths)

    assert "Chile ancho" in text
    assert "\\u" not in text
