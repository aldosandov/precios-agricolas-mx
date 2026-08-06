# precios-agricolas-mx

Pipeline SNIIM → BigQuery → Streamlit. Traduce el precio de mayoreo del SNIIM
en un precio de referencia en parcela para productores de Atzitzintla,
Puebla. Proyecto de CIMA A.C., publicado en subdominio de cimaac.org.mx.

**PRD (fuente de verdad):** https://app.notion.com/p/3b32fa43c7628114820bca655336b6ef
**Tracking:** Linear, proyecto `precios-agricolas-mx` (equipo ALD). Todo el
trabajo desglosado en issues por fase, con dependencias como relaciones de
bloqueo.

## Estado actual

El PRD marca la Fase 0 como "hecha" pero ningún artefacto había
materializado — Fase 1 reconstruyó eso desde cero. **Fase 1 completa salvo la
verificación en Actions**: camino SNIIM → NDJSON → BigQuery funcionando de
punta a punta.

Cerrados: ALD-5 a ALD-25 (todos). Quedan dos verificaciones que dependen de
GitHub: empujar a `main`, disparar el workflow (ALD-24) y confirmar que la
alerta llega a Sentry con el DSN real (ALD-25). Sigue ALD-30 (spider de
backfill) y ALD-27 (backfill histórico).

Observabilidad: `transform/monitor.py` consulta BigQuery después de cada
corrida y manda a Sentry el resumen, los mercados activos sin datos, la
ventana vacía, el atraso de la fuente (3 días hábiles) y las ventanas que
reventaron. El check-in del monitor `ingesta-diaria-sniim` avisa además si el
job **no corrió**. Requiere el secret `SENTRY_DSN`; sin él, el paso del
workflow queda en `continue-on-error` y no tumba la ingesta.

Auth de Actions contra GCP: Workload Identity Federation, ya configurada en
el proyecto (service account `ingesta-diaria`, pool y proveedor `github`,
restringida al repo `aldosandov/precios-agricolas-mx`). Sin llaves ni
secrets.

`crudo.precios` existe y está **vacía**: la primera carga real la hará el job
diario o el backfill, no una corrida manual.

En pie:

| Módulo | Qué hace |
|---|---|
| `scraper/catalogs.py` | Regenera `data/catalogs/` desde los `<select>` del formulario |
| `scraper/fixtures.py` | Recaptura `tests/fixtures/sniim/` + manifiesto |
| `scraper/coverage.py` | Profundidad histórica por mercado (sondeo barato) |
| `scraper/query.py` | URL y subdivisión de ventana. Puro, sin red |
| `scraper/parsers/results.py` | Tabla → columnas y filas, por encabezado |
| `scraper/contract.py` | Registro crudo, llave natural, rechazo |
| `scraper/pipelines/ndjson.py` | Fila §10, banderas de calidad, NDJSON por fecha |
| `scraper/spiders/daily.py` | Barrido diario: 49 mercados × 2 modos |
| `transform/load.py` | Upsert de NDJSON a la capa cruda |
| `transform/raw/prices_table.sql` | DDL de `crudo.precios` |
| `transform/monitor.py` | Reporte de cobertura y alertas a Sentry |

Proyecto Scrapy en `scrapy.cfg` + `scraper/settings.py`; workflow en
`.github/workflows/daily.yml`. Falta el spider de backfill, `transform/
canonical|metrics/` y `app/`.

Lint con ruff configurado en `pyproject.toml` (line-length 100). `uv run ruff
check .` debe pasar limpio antes de commitear.

**La fecha es la de México, no la del runner.** Los runners de Actions van en
UTC y el SNIIM publica en hora del centro de México: después de las 18:00
local, `date.today()` del runner ya es el día siguiente y pide un día que la
fuente no ha publicado (cero filas, job en verde). Todo lo que necesite "hoy"
usa `scraper.query.source_date()`. Encontrado en la primera corrida real en
Actions.

Ojo con Scrapy ≥2.13: el entry point es `async def start()`. Definir solo
`start_requests()` no falla — el spider rastrea cero páginas y reporta
`finished`.

El registro crudo guarda cada criterio en dos planos: la etiqueta que dio la
tabla y el id que se envió. La llave natural es
`(date, product, presentation, origin, destination, prices_per_id)`, con la
etiqueta cuando existe y `id:<n>` cuando la consulta fijó ese criterio. El
contrato exige identidad, no valores: un precio faltante lo marca la capa
intermedia, no lo rechaza la ingesta.

## Fuente SNIIM — trampas verificadas

Detalle completo y evidencia en `docs/consulta-sniim.md`. Lo que no se debe
re-derivar:

- La página de resultados se alcanza por `GET` sin cookies ni `__VIEWSTATE`.
  El formulario real vive en `ConsultaFrutasYHortalizas.aspx?SubOpcion=4|0`;
  la URL del navegador es un contenedor con `<iframe>`.
- **El esquema de la tabla cambia según lo que se fije.** Todo criterio
  fijado a un solo valor desaparece de la tabla. Con
  `fechaInicio == fechaFinal` no hay columna `Fecha`; con `ProductoId=-1`
  aparecen `Producto` y `Calidad`. Siete columnas posibles.
- Filas de una sola celda son separadores de categoría (`Frutas`,
  `Frutas de Temporada`, `Hortalizas`, `Chiles Secos`). Salen en cualquier
  forma de consulta; hay que saltarlas siempre.
- Sin `<meta charset>`: decodificar por el header `Content-Type`. Los
  resultados traen UTF-8 crudo; el formulario sí usa entidades.
- `ProductoId`, `OrigenId` y `DestinoId` no pueden ser `-1` los tres a la
  vez (200 sin filas). El sentinela a excluir de catálogos es `-1`; el `0`
  sí es miembro (`Sin Especificar`).
- **El barrido fija el destino** (PRD §8.2): 49 peticiones por bloque contra
  222 si se fijara el producto, y la tabla conserva `Producto`, `Calidad`,
  `Presentación` y `Origen`. Cada bloque se pide dos veces, una por cada
  `PreciosPorId`. Los docs de ALD-8 decían "por producto"; se corrigió en
  ALD-18.
- `PreciosPorId` cambia los precios pero no la columna `Presentación`: no es
  inferible de la respuesta, hay que registrar qué se mandó.
- `RegistrosPorPagina` acepta hasta `Int32.MaxValue`; el límite real es el
  peso de la respuesta. Negativo devuelve 1 fila con `Página 1 de -N`, sin
  error. Ese `-N` es el total exacto del conjunto: prohibido en la ingesta,
  es el sondeo barato que usa `scraper/coverage.py`.
- Rango válido sin registros ≠ consulta rechazada: el primero responde 200
  con `NO HAY REGISTROS` y sin paginador.
- El SNIIM devuelve **503** bajo carga. Con 4 peticiones en paralelo hay que
  esperar entre reintentos (5/15/45/120 s) y poder retomar desde checkpoint.

## Profundidad histórica (ALD-14)

Medida mercado por mercado, no supuesta. Detalle en
`docs/profundidad-historica.md`; tablas en `data/catalogs/market_coverage.csv`
y `market_coverage_by_year.csv`.

- **El histórico arranca en 1998, no en 2007.** El backfill de la Fase 2
  (ALD-27) va desde 1998. ~15.3 millones de filas en total.
- 32 de 49 mercados llegan a 1998, 15 empiezan después. **Dos nunca tienen
  datos** (`71` Tapachula, `122` Chilpancingo) y **cinco dejaron de
  reportar** (Irapuato y Celaya en 2017, Cancún 2019, Durango 2022, Cuautla
  2025). Estar en el catálogo no implica tener datos.
- Dos mercados tienen huecos internos (Villahermosa 1999–2001, Zacatecas
  2003): una ventana vacía no siempre es un fallo de ingesta.

## Restricciones no negociables

1. Nunca paginar. El volumen se controla por ventana de fechas (subdivisión
   adaptativa, arranca en bloques trimestrales).
2. El parser lee el encabezado de cada respuesta. Prohibido mapear columnas
   por posición fija.
3. Un encabezado desconocido detiene la ingesta. No se descarta en silencio.
4. La capa cruda es inmutable. La validación marca, nunca corrige.
5. El mapeo canónico (producto/mercado) vive en archivos versionados en
   `data/`, nunca inferido en tiempo de consulta ni incrustado en código.
6. La app (`app/`) solo lee la capa final. Cero lógica de transformación ahí.
7. El campo Origen es de comercialización, no de producción. Ninguna capa
   puede afirmar lo contrario.
8. Documentación (README, CLAUDE.md, PRs, commits) en español. Código:
   nombres de archivo y carpeta, y comentarios, en inglés.

## Arquitectura

```
scraper/     Scrapy. Solo extracción y normalización sintáctica.
transform/   SQL de BigQuery: raw/, canonical/ y metrics/, más load.py
             (upsert de NDJSON a la capa cruda; corre aparte del scraper).
app/         Streamlit. Solo presentación e interacción.
data/        Catálogos y mapeos canónicos versionados.
tests/       Fixtures de HTML real y pruebas de parser/aritmética.
docs/        Notas de investigación de la fuente.
```

BigQuery en tres capas: **cruda** (inmutable) → **intermedia** (tipada,
banderas de calidad) → **final** (lo único que lee la app). Partición por
fecha (día), clustering por producto/mercado. Carga por archivo (`bq load`),
nunca streaming.

Proyecto GCP `precios-agricolas-mx`, facturación activa, **ubicación US**
(no se puede cambiar sin recrear). Datasets y tablas en español, como el
modelo de datos del PRD: `crudo.precios` ya existe (DDL en
`transform/raw/prices_table.sql`); faltan `intermedio` y `final`.

## Stack

Python, Scrapy, parsel, BigQuery, pandas, Streamlit, Plotly, GitHub Actions,
pytest. Dependencias con `uv`/`pip-tools` (`pyproject.toml`).

## Convenciones de trabajo

- Trabajo individual: sin asignaciones, sin ciclos/sprints en Linear.
- Cada que hagas una tarea actualiza el readme si es necesario. 
- Cada que sea necesario actualiza el CLAUDE.md
- Git: commits directos a `dev`; `main` se actualiza al cerrar una fase.
  Ojo: `workflow_dispatch` solo aparece si el workflow existe en `main`, así
  que un workflow nuevo no se puede probar sin llevarlo antes a esa rama.
- Pruebas moderadas y dirigidas a lo que realmente se rompe (parser contra
  fixtures HTML reales, aritmética del precio en parcela, integridad del
  mapeo canónico) — no cobertura formal exhaustiva.
- Al cerrar un issue: commit en español con el porqué, y actualizar el
  issue de Linear con hallazgos y decisiones antes de pasarlo a Done.
- Parte de la suite pega al SNIIM en vivo (robots, GET sin estado, cotejo de
  catálogos). Lo demás corre offline contra fixtures.
- Restricción de costo: GCP ≤ USD 20/mes. Vistas/tablas precalculadas,
  nunca agregación en tiempo real desde la app (`@st.cache_data`).
