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
└── catalogs/          Mapeo producto/mercado (nunca inferido en runtime)

tests/               Pruebas de parser y aritmética
└── fixtures/          HTML real capturado del SNIIM

.github/workflows/   CI (pendiente de definir)
```

Cada carpeta está vacía por ahora (solo `.gitkeep`) — issue ALD-5 crea el
árbol; los siguientes issues de Fase 1 llenan cada parte.

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
