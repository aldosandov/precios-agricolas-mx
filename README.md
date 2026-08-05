# precios-agricolas-mx

Precios agrícolas abiertos para México · Scraper del SNIIM, pipeline ETL y
visualizador web · En colaboración con CIMA A.C.

Pipeline SNIIM → BigQuery → Streamlit. Traduce el precio de mayoreo del
SNIIM en un precio de referencia en parcela para productores de
Atzitzintla, Puebla. Ver [`CLAUDE.md`](CLAUDE.md) para arquitectura y
restricciones del proyecto; el PRD completo vive en Notion (link en
`CLAUDE.md`).

## Estructura

```
scraper/            Scrapy. Extracción y normalización sintáctica del SNIIM.
├── spiders/         Arañas de scraping
├── parsers/         Parseo de encabezados y filas de respuesta
└── pipelines/        Escritura a la capa cruda

transform/           SQL de BigQuery
├── canonical/        Cruda → intermedia (tipado, banderas de calidad)
└── metrics/           Intermedia → final (precio en parcela, agregados)

app/                 Streamlit. Solo presentación e interacción.
├── views/            Páginas/vistas de la app
└── texts/            Copys y textos estáticos

data/                Catálogos y mapeos canónicos versionados
└── catalogs/          products/origins/destinations.csv (nunca inferidos en runtime)
                       + market_coverage*.csv (profundidad histórica por mercado)

tests/               Pruebas de parser y aritmética
└── fixtures/sniim/   HTML real capturado del SNIIM + manifest.json

docs/                Notas de investigación de la fuente
.github/workflows/   CI (pendiente de definir)
```

Cada carpeta está vacía por ahora (solo `.gitkeep`) — issue ALD-5 crea el
árbol; los siguientes issues de Fase 1 llenan cada parte.

## Cómo se consulta el SNIIM

El scraper no envía el formulario ASP.NET: va directo por `GET` a la página
de resultados, con todos los criterios en la cadena de consulta y sin
cookies. Pide `RegistrosPorPagina=5000` y controla el volumen por ventana
de fechas, subdividiendo cuando el paginador reporta más de una página —
nunca paginando. Endpoint, parámetros, restricciones del servidor y
evidencia en [`docs/consulta-sniim.md`](docs/consulta-sniim.md).

`scraper/query.py` arma esas URLs y decide la subdivisión. Es puro: no hace
peticiones, solo construye la consulta y lee el indicador de página de la
respuesta que le pasen.

Los ids internos que espera ese endpoint salen de los `<select>` del
formulario y viven versionados en `data/catalogs/`. Se regeneran con:

```bash
uv run python -m scraper.catalogs            # todos los catálogos
uv run python -m scraper.catalogs products   # solo uno
```

Nunca se infieren en tiempo de consulta. `tests/test_catalogs.py` compara
el archivo versionado contra el formulario en vivo, así que un producto
nuevo o renombrado en el SNIIM rompe la prueba y obliga a revisar el diff.

## Profundidad histórica

El histórico del SNIIM arranca en **1998** (el PRD suponía 2007) y no es
parejo: 32 de los 49 mercados llegan hasta 1998, 15 empiezan después, dos no
tienen datos en ningún año y cinco dejaron de reportar. Medido mercado por
mercado y año por año, sin descargar resultados:

```bash
uv run python -m scraper.coverage        # regenera la tabla (~1 h)
uv run python -m scraper.coverage 210    # sondea un mercado, sin escribir
```

Resultado versionado en `data/catalogs/market_coverage.csv` (primer y último
año, huecos y volumen por mercado) con la evidencia por año en
`market_coverage_by_year.csv`. Hallazgos y método en
[`docs/profundidad-historica.md`](docs/profundidad-historica.md).

## Fixtures

El parser se prueba contra HTML real guardado, nunca inventado. Las
respuestas están en `tests/fixtures/sniim/`, con `manifest.json` guardando
la URL exacta que produjo cada una y qué caso ejercita. Cubren las formas
en que cambia el esquema de la tabla, el truncamiento por paginador, los
separadores de categoría y la respuesta de rechazo.

```bash
uv run python -m scraper.fixtures                     # recapturar todas
uv run python -m scraper.fixtures paginated_overflow  # solo una
```

Recapturar reescribe historia: el SNIIM revisa precios pasados, así que hay
que revisar el diff antes de commitear. `tests/test_fixtures.py` corre sin
red y detecta si una recaptura cambió la forma de la respuesta.

## Setup

Requiere Python ≥3.11 y [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync              # instala dependencias (scraper + transform + app + dev)
```

## Correr el proyecto

Aún no hay entry points ejecutables — scraper, transform y app se
construyen en los issues siguientes de Fase 1 (ver Linear, proyecto
`precios-agricolas-mx`). Esta sección se actualiza según se materialice
cada pieza:

```bash
# scraper (pendiente)
uv run scrapy crawl <spider> ...

# tests
uv run pytest
```

## Dependencias

Gestionadas con `uv` en `pyproject.toml` / `uv.lock`. Stack: Scrapy,
parsel, pandas, google-cloud-bigquery, Streamlit, Plotly, pytest.
