"""Live guard: the SNIIM results page must stay reachable by a plain, cookieless GET.

See docs/consulta-sniim.md (ALD-8, ALD-9). If any of these break, the
scraper's whole query strategy needs rethinking, so fail loud rather than
silently falling back to the stateful POST form.
"""

import re
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

RESULTS_URL = (
    "https://www.economia-sniim.gob.mx/nuevo/Consultas/MercadosNacionales"
    "/PreciosDeMercado/Agricolas/ResultadosConsultaFechaFrutasYHortalizas.aspx"
)

# Papa Alpha - Primera, first week of January 2026, price per kilogram.
SAMPLE_QUERY = {
    "fechaInicio": "01/01/2026",
    "fechaFinal": "07/01/2026",
    "ProductoId": "740",
    "OrigenId": "-1",
    "DestinoId": "-1",
    "PreciosPorId": "2",
    "RegistrosPorPagina": "1000",
}

EXPECTED_HEADERS = [
    "Fecha",
    "Presentación",
    "Origen",
    "Destino",
    "Precio Mín",
    "Precio Max",
    "Precio Frec",
    "Obs.",
]


def _get(query: dict[str, str]) -> tuple[int, str]:
    """Fetch the results page with no cookies and no prior form request.

    urllib sends no cookies unless a CookieProcessor is installed, so this
    opener is exactly the cookieless case we need to prove.

    Retries once on a dropped connection or timeout. SNIIM occasionally
    stalls under a burst of requests, and a flaky suite gets ignored — an
    HTTPError is a real answer from the server and is never retried.
    """
    url = f"{RESULTS_URL}?{urllib.parse.urlencode(query)}"
    opener = urllib.request.build_opener()
    for attempt in (1, 2):
        try:
            with opener.open(url, timeout=60) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 2:
                raise
            time.sleep(2)
    raise AssertionError("unreachable")


def _cells(html: str) -> list[list[str]]:
    rows = []
    for row in re.findall(r"<tr[^>]*>.*?</tr>", html, re.S):
        cells = [
            re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cell)).strip()
            for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        ]
        if len(cells) >= len(EXPECTED_HEADERS):
            rows.append(cells)
    return rows


def test_cookieless_get_returns_data():
    status, html = _get(SAMPLE_QUERY)
    assert status == 200
    rows = _cells(html)
    assert rows, "results page returned no table rows"
    assert rows[0] == EXPECTED_HEADERS, f"unexpected header row: {rows[0]}"
    assert len(rows) > 1, "header present but no data rows"


def test_selecting_everything_is_rejected():
    """ProductoId, OrigenId and DestinoId cannot all be -1 at once.

    Documents why the scraper sweeps product by product.
    """
    _, html = _get({**SAMPLE_QUERY, "ProductoId": "-1"})
    assert "No puede seleccionar todos los productos" in html
    assert not _cells(html)


def test_pagination_marker_is_present():
    """Truncation is silent; the paginator label is the only signal.

    The adaptive date-window subdivision relies on reading this.
    """
    _, html = _get(SAMPLE_QUERY)
    assert re.search(r"Página\s+\d+\s+de\s+\d+", html), "paginator label missing"


# --- RegistrosPorPagina limits (ALD-9) ---

# Whole January 2026 for the same product: 769 records, enough to paginate
# at the server's default page size without downloading megabytes.
MONTH_QUERY = {**SAMPLE_QUERY, "fechaFinal": "31/01/2026"}


def _pages(html: str) -> int:
    match = re.search(r"Página\s+(-?\d+)\s+de\s+(-?\d+)", html)
    assert match, "paginator label missing"
    return int(match.group(2))


def test_omitting_page_size_falls_back_to_100():
    """The scraper must always send the parameter; the default is tiny."""
    query = {k: v for k, v in MONTH_QUERY.items() if k != "RegistrosPorPagina"}
    _, html = _get(query)
    assert _pages(html) > 1
    assert len(_cells(html)) - 1 == 100


def test_negative_page_size_loses_data_silently():
    """Pins the one failure mode that returns 200 with almost no rows.

    The paginator denominator goes negative instead of erroring, which is
    why the parser rejects denominators <= 0 rather than just != 1.
    """
    status, html = _get({**MONTH_QUERY, "RegistrosPorPagina": "-1"})
    assert status == 200
    assert _pages(html) < 0
    assert len(_cells(html)) - 1 == 1


@pytest.mark.parametrize("value", ["0", "abc", "2147483648"])
def test_invalid_page_sizes_fail_loudly(value):
    """Zero, non-numeric and Int32 overflow all blow up server-side."""
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get({**MONTH_QUERY, "RegistrosPorPagina": value})
    assert excinfo.value.code == 500


def test_configured_page_size_returns_one_page():
    """5000 is what the scraper sends; a month of one product fits in it."""
    _, html = _get({**MONTH_QUERY, "RegistrosPorPagina": "5000"})
    assert _pages(html) == 1
    # 769 records when measured; not pinned exactly, SNIIM may revise history.
    assert len(_cells(html)) - 1 > 100
