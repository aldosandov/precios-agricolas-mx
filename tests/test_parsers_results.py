"""Header-driven parsing of the SNIIM results table (ALD-16).

The table's shape depends on the query: every criterion pinned to a single
value drops out of the columns. A parser that mapped by position would not
fail on those responses, it would write shifted data. So the tests pin the
shapes that differ, not just the one the spider sweeps with.

Runs offline against the ALD-13 fixtures.
"""

import pytest

from scraper.fixtures import FIXTURES_DIR
from scraper.parsers.results import (
    UnexpectedResponse,
    UnknownColumn,
    parse_results,
)
from scraper.query import QueryRejected


def _fixture(name: str) -> str:
    return (FIXTURES_DIR / f"{name}.html").read_bytes().decode("utf-8")


# --- the shape the spider sweeps with: date range, one product ---


def test_full_columns_are_mapped_by_header_name():
    table = parse_results(_fixture("range_product_fixed"))

    assert table.columns == (
        "date",
        "presentation",
        "origin",
        "destination",
        "price_min",
        "price_max",
        "price_frequent",
        "obs",
    )


def test_first_row_carries_the_values_of_its_own_columns():
    table = parse_results(_fixture("range_product_fixed"))

    assert table.rows[0] == {
        "category": "Hortalizas",
        "date": "01/07/2026",
        "presentation": "Kilogramo",
        "origin": "Guanajuato",
        "destination": "Aguascalientes: Centro Comercial Agropecuario de Aguascalientes",
        "price_min": "20.00",
        "price_max": "24.00",
        "price_frequent": "22.00",
        "obs": None,
    }


# --- the shapes a positional parser would get wrong without failing ---


def test_single_day_response_has_no_date_column():
    """fechaInicio == fechaFinal drops Fecha, and everything shifts left.

    This is the critical case: by position, `Kilogramo` would be read as the
    date and every column after it would be off by one, with no error.
    """
    table = parse_results(_fixture("single_day_no_date_column"))

    assert "date" not in table.columns
    assert table.rows[0]["presentation"] == "Kilogramo"
    assert table.rows[0]["origin"] == "Guanajuato"


def test_all_products_response_swaps_destination_for_product_and_quality():
    table = parse_results(_fixture("range_destination_fixed"))

    assert "destination" not in table.columns
    assert table.columns[:5] == ("date", "product", "quality", "presentation", "origin")
    assert table.rows[0]["product"] == "Aguacate Hass"
    assert table.rows[0]["quality"] == "Primera"


def test_unknown_header_stops_the_parse():
    html = _fixture("range_product_fixed").replace("Precio Frec", "Precio Modal")

    with pytest.raises(UnknownColumn, match="Precio Modal"):
        parse_results(html)


# --- category separators ---


def test_category_separators_are_carried_onto_the_rows_below_them():
    table = parse_results(_fixture("range_destination_fixed"))

    first_of_each = {}
    for row in table.rows:
        first_of_each.setdefault(row["category"], row["product"])

    assert first_of_each == {
        "Frutas": "Aguacate Hass",
        "Frutas de Temporada": "Ciruela Roja",
        "Hortalizas": "Ajo Morado",
        "Chiles Secos": "Chile ancho",
    }


def test_separator_rows_are_not_emitted_as_data():
    table = parse_results(_fixture("range_destination_fixed"))

    assert len(table.rows) == 192


# --- responses that are not a table of prices ---


def test_rejected_criteria_raise_instead_of_reading_as_empty():
    with pytest.raises(QueryRejected):
        parse_results(_fixture("rejected_all_criteria"))


def test_a_range_without_prices_parses_as_an_empty_table():
    table = parse_results(_fixture("probe_no_records"))

    assert table.columns == ()
    assert table.rows == ()


def test_a_response_without_a_results_table_raises():
    with pytest.raises(UnexpectedResponse):
        parse_results("<html><body><p>Servicio no disponible</p></body></html>")


def test_a_row_narrower_than_the_header_raises():
    """A short row is never dropped: zip() would pad it into a valid-looking one."""
    html = _fixture("range_product_fixed").replace(
        '<td class="Datos2">22.00</td><td class="Datos2"></td>',
        '<td class="Datos2">22.00</td>',
        1,
    )

    with pytest.raises(UnexpectedResponse, match="7"):
        parse_results(html)
