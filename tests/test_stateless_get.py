"""Live guard: the SNIIM results page must stay reachable by a plain, cookieless GET.

See docs/consulta-sniim.md (ALD-8). If any of these break, the scraper's
whole query strategy needs rethinking, so fail loud rather than silently
falling back to the stateful POST form.
"""

import re
import urllib.parse
import urllib.request

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
    """
    url = f"{RESULTS_URL}?{urllib.parse.urlencode(query)}"
    opener = urllib.request.build_opener()
    with opener.open(url, timeout=60) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


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
