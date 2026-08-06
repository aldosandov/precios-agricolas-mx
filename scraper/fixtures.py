"""Capture real SNIIM responses as parser test fixtures.

The parser is never tested against hand-written HTML. Invented markup agrees
with whatever the parser already does; only the real thing catches how SNIIM
actually encodes entities, whitespace and its shifting column set.

Each fixture is stored as raw bytes exactly as the server sent them — no
decoding, no reformatting — alongside a manifest recording the URL that
produced it and what it is meant to exercise.

Usage:
    uv run python -m scraper.fixtures              # recapture every fixture
    uv run python -m scraper.fixtures single_day   # just one

Recapturing rewrites history: SNIIM revises past prices, so expect diffs in
the data rows. Review them before committing.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

RESULTS_URL = (
    "https://www.economia-sniim.gob.mx/nuevo/Consultas/MercadosNacionales"
    "/PreciosDeMercado/Agricolas/ResultadosConsultaFechaFrutasYHortalizas.aspx"
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "sniim"
MANIFEST_PATH = FIXTURES_DIR / "manifest.json"

# Papa Alpha - Primera; Central de Abasto de Puebla. Both are staples of the
# Atzitzintla use case, so the fixtures double as realistic sample data.
PAPA_ALPHA = "740"
CEDA_PUEBLA = "210"

# A window with real data, narrow enough to keep fixtures small.
RANGE = {"fechaInicio": "01/07/2026", "fechaFinal": "03/07/2026"}
SINGLE_DAY = {"fechaInicio": "01/07/2026", "fechaFinal": "01/07/2026"}

BASE = {"OrigenId": "-1", "DestinoId": "-1", "PreciosPorId": "2", "RegistrosPorPagina": "5000"}

FIXTURES: dict[str, dict[str, object]] = {
    "range_product_fixed": {
        "purpose": (
            "Rango de fechas con un producto fijo y todos los orígenes y "
            "destinos. Trae la columna Fecha y, a diferencia del barrido, "
            "también Destino."
        ),
        "query": {**BASE, **RANGE, "ProductoId": PAPA_ALPHA},
    },
    "range_destination_fixed": {
        "purpose": (
            "Forma que produce el barrido en producción (PRD §8.2): todos los "
            "productos contra un solo mercado. Añade Producto y Calidad, quita "
            "Destino, y mete filas separadoras de categoría (Frutas / Frutas de "
            "Temporada / Hortalizas / Chiles Secos)."
        ),
        "query": {**BASE, **RANGE, "ProductoId": "-1", "DestinoId": CEDA_PUEBLA},
    },
    "paginated_overflow": {
        "purpose": (
            "Truncamiento silencioso: mismo rango con RegistrosPorPagina chico, "
            "para que el paginador diga 'Página 1 de N' con N > 1."
        ),
        "query": {**BASE, **RANGE, "ProductoId": PAPA_ALPHA, "RegistrosPorPagina": "50"},
    },
    "single_day_no_date_column": {
        "purpose": (
            "fechaInicio == fechaFinal elimina la columna Fecha. Un parser por "
            "posición no falla aquí, solo escribe datos corridos."
        ),
        "query": {**BASE, **SINGLE_DAY, "ProductoId": PAPA_ALPHA},
    },
    "probe_row_count": {
        "purpose": (
            "Sondeo de profundidad histórica (ALD-14): RegistrosPorPagina=-1 "
            "devuelve una sola fila y 'Página 1 de -N', con N = total real del "
            "conjunto. Truco de sondeo, prohibido en la ingesta."
        ),
        "query": {
            **BASE,
            "fechaInicio": "01/01/2024",
            "fechaFinal": "31/12/2024",
            "ProductoId": "-1",
            "DestinoId": CEDA_PUEBLA,
            "RegistrosPorPagina": "-1",
        },
    },
    "probe_no_records": {
        "purpose": (
            "Año sin datos para un mercado: 200 con 'NO HAY REGISTROS' y sin "
            "paginador. Es lo que marca el piso del backfill por mercado."
        ),
        "query": {
            **BASE,
            "fechaInicio": "01/01/1996",
            "fechaFinal": "31/12/1996",
            "ProductoId": "-1",
            "DestinoId": CEDA_PUEBLA,
            "RegistrosPorPagina": "-1",
        },
    },
    "rejected_all_criteria": {
        "purpose": (
            "Producto, origen y destino en -1 a la vez: 200 sin filas y con "
            "mensaje de rechazo. El parser no debe leerlo como 'no hubo precios'."
        ),
        "query": {**BASE, **RANGE, "ProductoId": "-1"},
    },
}


def build_url(query: dict[str, str]) -> str:
    """Keep the parameter order the docs use, so manifest URLs stay readable."""
    order = [
        "fechaInicio",
        "fechaFinal",
        "ProductoId",
        "OrigenId",
        "DestinoId",
        "PreciosPorId",
        "RegistrosPorPagina",
    ]
    ordered = sorted(query.items(), key=lambda item: order.index(item[0]))
    return f"{RESULTS_URL}?{urllib.parse.urlencode(ordered)}"


def capture(url: str) -> bytes:
    """Fetch raw bytes, cookieless. No decode: entity bugs live in the bytes."""
    with urllib.request.urlopen(url, timeout=120) as resp:
        if resp.status != 200:
            raise RuntimeError(f"expected 200, got {resp.status} for {url}")
        return resp.read()


def main(argv: list[str]) -> int:
    wanted = argv or list(FIXTURES)
    unknown = [name for name in wanted if name not in FIXTURES]
    if unknown:
        print(f"unknown fixture(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"available: {', '.join(FIXTURES)}", file=sys.stderr)
        return 2

    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8")) if MANIFEST_PATH.exists() else {}

    for name in wanted:
        spec = FIXTURES[name]
        url = build_url(spec["query"])
        body = capture(url)
        (FIXTURES_DIR / f"{name}.html").write_bytes(body)
        manifest[name] = {
            "file": f"{name}.html",
            "url": url,
            "purpose": spec["purpose"],
            "bytes": len(body),
        }
        print(f"{name}: {len(body)} bytes")

    ordered = {key: manifest[key] for key in FIXTURES if key in manifest}
    MANIFEST_PATH.write_text(json.dumps(ordered, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"manifest -> {MANIFEST_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
