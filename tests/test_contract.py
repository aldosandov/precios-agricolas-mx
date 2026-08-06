"""What may enter the raw layer, and what stops the ingest (ALD-17).

A row without a date, a product, a destination, a price mode or a natural key
is not a price observation: nothing downstream could place it in time, join it
to a market, or deduplicate it. Those rows do not get written and do not get
patched — they stop the run, because in 392 real rows not one of those fields
was ever blank, so a blank one means the response changed shape.

Prices are deliberately not part of the contract. A missing price is a quality
problem, and quality is flagged in the intermediate layer, never corrected on
the way in.

Runs offline against the ALD-13 fixtures.
"""

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from scraper.contract import (
    DuplicateNaturalKey,
    IncompleteRow,
    QueryContext,
    build_records,
)
from scraper.fixtures import FIXTURES_DIR
from scraper.parsers.results import ParsedTable, parse_results
from scraper.query import ALL, DateWindow


def _table(name: str):
    return parse_results((FIXTURES_DIR / f"{name}.html").read_bytes().decode("utf-8"))


WINDOW = DateWindow(date(2026, 7, 1), date(2026, 7, 3))


def _context(
    *,
    product_id: str = "740",
    origin_id: str = ALL,
    destination_id: str = ALL,
    window: DateWindow = WINDOW,
) -> QueryContext:
    return QueryContext(
        product_id=product_id,
        origin_id=origin_id,
        destination_id=destination_id,
        prices_per_id="2",
        window=window,
        source_url="https://example.test/results",
        fetched_at=datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
    )


# --- a complete row goes through ---


def test_a_complete_row_becomes_a_record():
    records = build_records(_table("range_product_fixed"), _context())

    assert len(records) == 113
    first = records[0]
    assert first.date == "01/07/2026"
    assert first.presentation == "Kilogramo"
    assert first.origin == "Guanajuato"
    assert first.destination == (
        "Aguascalientes: Centro Comercial Agropecuario de Aguascalientes"
    )
    assert first.price_min == "20.00"
    assert first.prices_per_id == "2"
    assert first.category == "Hortalizas"


def test_a_pinned_criterion_is_recorded_as_the_id_that_was_sent():
    """The table never mentions the product when the query pinned it."""
    records = build_records(_table("range_product_fixed"), _context(product_id="740"))

    assert records[0].product is None
    assert records[0].product_id == "740"
    assert records[0].natural_key[1] == "id:740"


def test_a_label_the_table_served_identifies_the_criterion_instead_of_the_id():
    records = build_records(
        _table("range_destination_fixed"), _context(product_id=ALL, destination_id="210")
    )

    assert records[0].product == "Aguacate Hass"
    assert records[0].quality == "Primera"
    assert records[0].natural_key[1] == "Aguacate Hass - Primera"
    assert records[0].natural_key[4] == "id:210"


def test_a_missing_price_does_not_stop_the_ingest():
    """Quality is the intermediate layer's job; it flags and never corrects."""
    table = _table("range_product_fixed")
    without_price = ParsedTable(table.columns, ({**table.rows[0], "price_min": None},))

    records = build_records(without_price, _context())

    assert records[0].price_min is None


# --- the date, which a single-day query does not put in the table ---


def test_a_single_day_query_takes_the_date_from_the_window():
    day = date(2026, 7, 1)

    records = build_records(
        _table("single_day_no_date_column"), _context(window=DateWindow(day, day))
    )

    assert records[0].date == "01/07/2026"


def test_a_multi_day_window_without_a_date_column_is_not_attributable():
    with pytest.raises(IncompleteRow, match="date"):
        build_records(_table("single_day_no_date_column"), _context())


# --- rows that may not enter the raw layer ---


def test_a_row_missing_its_destination_stops_the_ingest():
    table = _table("range_product_fixed")
    without_destination = ParsedTable(
        table.columns, ({**table.rows[0], "destination": None},)
    )

    with pytest.raises(IncompleteRow, match="destination"):
        build_records(without_destination, _context())


def test_a_row_missing_its_presentation_stops_the_ingest():
    table = _table("range_product_fixed")
    without_presentation = ParsedTable(
        table.columns, ({**table.rows[0], "presentation": None},)
    )

    with pytest.raises(IncompleteRow, match="presentation"):
        build_records(without_presentation, _context())


def test_an_unrecorded_price_mode_stops_the_ingest():
    """Mode 1 and mode 2 give different prices for the same presentation."""
    context = replace(_context(), prices_per_id="")

    with pytest.raises(IncompleteRow, match="prices_per_id"):
        build_records(_table("range_product_fixed"), context)


def test_the_price_mode_is_checked_even_when_the_window_had_no_prices():
    """Otherwise a spider misconfigured this way runs clean until the first
    window that happens to return rows."""
    context = replace(_context(), prices_per_id="")

    with pytest.raises(IncompleteRow, match="prices_per_id"):
        build_records(ParsedTable((), ()), context)


def test_the_error_names_every_missing_field_at_once():
    table = _table("range_product_fixed")
    broken = ParsedTable(
        table.columns, ({**table.rows[0], "destination": None, "origin": None},)
    )

    with pytest.raises(IncompleteRow, match="destination.*origin|origin.*destination"):
        build_records(broken, _context())


# --- the natural key has to be one ---


def test_the_natural_key_is_unique_across_a_whole_response():
    records = build_records(
        _table("range_destination_fixed"), _context(product_id=ALL, destination_id="210")
    )

    assert len({record.natural_key for record in records}) == len(records)


def test_a_repeated_natural_key_stops_the_ingest():
    table = _table("range_product_fixed")
    twice = ParsedTable(table.columns, (table.rows[0], table.rows[0]))

    with pytest.raises(DuplicateNaturalKey):
        build_records(twice, _context())
