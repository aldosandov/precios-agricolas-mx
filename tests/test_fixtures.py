"""What the captured SNIIM fixtures actually contain (ALD-13).

These run offline against the bytes in tests/fixtures/sniim/. They are not
parser tests — the parser lands in ALD-16 — they pin the shape of the real
responses so that whoever writes the parser knows exactly what has to be
handled, and so a recapture that quietly changes shape gets caught.

Recapture with: uv run python -m scraper.fixtures
"""

import html
import json
import re

import pytest

from scraper.fixtures import FIXTURES, FIXTURES_DIR, MANIFEST_PATH, RESULTS_URL

REQUIRED_PARAMS = [
    "fechaInicio",
    "fechaFinal",
    "ProductoId",
    "OrigenId",
    "DestinoId",
    "PreciosPorId",
    "RegistrosPorPagina",
]

# Present in every query shape, so they anchor the header row.
PRICE_COLUMNS = ["Precio Mín", "Precio Max", "Precio Frec", "Obs."]


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _text(fixture: str) -> str:
    """Decode as UTF-8, which is what the Content-Type header promises."""
    return (FIXTURES_DIR / f"{fixture}.html").read_bytes().decode("utf-8")


def _rows(fixture: str) -> tuple[list[str] | None, list[list[str]], list[str]]:
    """Split a fixture into header, data rows and one-cell separator rows.

    Deliberately crude: enough to describe the fixture, not a parser.
    """
    page = _text(fixture)
    header: list[str] | None = None
    data: list[list[str]] = []
    separators: list[str] = []
    for row in re.findall(r"<tr[^>]*>.*?</tr>", page, re.S):
        cells = [
            html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cell))).strip()
            for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
        ]
        if "Precio Frec" in cells:
            header = cells
        elif header is None:
            continue
        elif len(cells) == len(header):
            data.append(cells)
        elif len(cells) == 1 and cells[0] and "gina" not in cells[0]:
            separators.append(cells[0])
    return header, data, separators


def _pages(fixture: str) -> int:
    match = re.search(r"Página\s+(-?\d+)\s+de\s+(-?\d+)", _text(fixture))
    assert match, "paginator label missing"
    return int(match.group(2))


# --- the manifest is the documentation; keep it honest ---


def test_manifest_covers_every_fixture(manifest):
    assert set(manifest) == set(FIXTURES)


def test_no_orphan_fixture_files(manifest):
    on_disk = {path.stem for path in FIXTURES_DIR.glob("*.html")}
    assert on_disk == set(manifest)


@pytest.mark.parametrize("fixture", FIXTURES)
def test_manifest_records_a_reproducible_url(fixture, manifest):
    url = manifest[fixture]["url"]
    assert url.startswith(RESULTS_URL + "?")
    assert all(f"{param}=" in url for param in REQUIRED_PARAMS)


@pytest.mark.parametrize("fixture", FIXTURES)
def test_manifest_byte_count_matches_the_file(fixture, manifest):
    """A stale manifest means a fixture was edited by hand. It must not be."""
    assert (FIXTURES_DIR / f"{fixture}.html").stat().st_size == manifest[fixture]["bytes"]


@pytest.mark.parametrize("fixture", FIXTURES)
def test_fixture_is_utf8_without_a_charset_declaration(fixture):
    """The results page declares no charset; decoding must use the HTTP header.

    _text() would raise on invalid UTF-8, so calling it is the assertion.
    """
    assert "charset" not in _text(fixture).lower()


# --- the column set moves with the query (see docs/consulta-sniim.md) ---

EXPECTED_HEADERS = {
    "range_product_fixed": ["Fecha", "Presentación", "Origen", "Destino", *PRICE_COLUMNS],
    "range_destination_fixed": [
        "Fecha",
        "Producto",
        "Calidad",
        "Presentación",
        "Origen",
        *PRICE_COLUMNS,
    ],
    "paginated_overflow": ["Fecha", "Presentación", "Origen", "Destino", *PRICE_COLUMNS],
    "single_day_no_date_column": ["Presentación", "Origen", "Destino", *PRICE_COLUMNS],
}


@pytest.mark.parametrize("fixture", EXPECTED_HEADERS)
def test_header_matches_the_documented_shape(fixture):
    header, data, _ = _rows(fixture)
    assert header == EXPECTED_HEADERS[fixture]
    assert data, "fixture has a header but no data rows"


def test_single_day_query_drops_the_date_column():
    """The trap: fechaInicio == fechaFinal removes Fecha entirely."""
    header, _, _ = _rows("single_day_no_date_column")
    assert "Fecha" not in header


def test_all_products_query_adds_product_and_quality():
    header, _, _ = _rows("range_destination_fixed")
    assert header[1:3] == ["Producto", "Calidad"]
    assert "Destino" not in header, "the pinned destination is echoed, not tabulated"


# --- rows that are not data ---


def test_category_separators_appear_in_every_shape():
    """One-cell rows carrying a category name sit between the data rows.

    They show up even when a single product is queried, so the parser has to
    skip them regardless of query shape.
    """
    for fixture in EXPECTED_HEADERS:
        _, _, separators = _rows(fixture)
        assert separators, f"{fixture}: expected at least one category separator"


def test_all_four_categories_are_captured():
    """SNIIM splits into four, not the three the PRD listed."""
    _, _, separators = _rows("range_destination_fixed")
    assert separators == ["Frutas", "Frutas de Temporada", "Hortalizas", "Chiles Secos"]


def test_separator_rows_are_not_counted_as_data():
    header, data, separators = _rows("range_destination_fixed")
    assert all(len(row) == len(header) for row in data)
    assert all(name not in {row[0] for row in data} for name in separators)


# --- pagination and rejection ---


def test_overflow_fixture_reports_more_than_one_page():
    assert _pages("paginated_overflow") > 1


def test_overflow_fixture_is_truncated_to_the_page_size():
    """50 rows returned out of a set the unclamped fixture shows is larger."""
    _, truncated, _ = _rows("paginated_overflow")
    _, full, _ = _rows("range_product_fixed")
    assert len(truncated) == 50
    assert len(full) > len(truncated)


def test_full_fixture_reports_a_single_page():
    """Same window without the tiny page size: nothing was cut."""
    assert _pages("range_product_fixed") == 1


def test_rejection_fixture_has_no_table_and_says_why():
    """200 with zero rows is not the same as "no prices that week"."""
    page = _text("rejected_all_criteria")
    assert "No puede seleccionar todos los productos" in page
    header, data, _ = _rows("rejected_all_criteria")
    assert header is None
    assert data == []
