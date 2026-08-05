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
materializado — Fase 1 reconstruye eso desde cero antes de tocar BigQuery.
Cerrados: ALD-5, 7, 8, 9, 10, 11, 12, 13, 15. Sigue ALD-16 (parser por
encabezado), luego ALD-14, 19, 30.

En pie: `scraper/catalogs.py` (regenera `data/catalogs/`),
`scraper/fixtures.py` (recaptura `tests/fixtures/sniim/`),
`scraper/query.py` (URL + subdivisión de ventana, puro). Todavía vacíos
`transform/`, `app/`, `scraper/spiders|parsers|pipelines/`.

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
- `PreciosPorId` cambia los precios pero no la columna `Presentación`: no es
  inferible de la respuesta, hay que registrar qué se mandó.
- `RegistrosPorPagina` acepta hasta `Int32.MaxValue`; el límite real es el
  peso de la respuesta. Negativo devuelve 1 fila con `Página 1 de -N`, sin
  error.

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
transform/   SQL de BigQuery: canonical/ y metrics/.
app/         Streamlit. Solo presentación e interacción.
data/        Catálogos y mapeos canónicos versionados.
tests/       Fixtures de HTML real y pruebas de parser/aritmética.
docs/        Notas de investigación de la fuente.
```

BigQuery en tres capas: **cruda** (inmutable) → **intermedia** (tipada,
banderas de calidad) → **final** (lo único que lee la app). Partición por
fecha (día), clustering por producto/mercado. Carga por archivo (`bq load`),
nunca streaming.

## Stack

Python, Scrapy, parsel, BigQuery, pandas, Streamlit, Plotly, GitHub Actions,
pytest. Dependencias con `uv`/`pip-tools` (`pyproject.toml`).

## Convenciones de trabajo

- Trabajo individual: sin asignaciones, sin ciclos/sprints en Linear.
- Cada que hagas una tarea actualiza el readme si es necesario. 
- Cada que sea necesario actualiza el CLAUDE.md
- Git: commits directos a `dev`; `main` se actualiza al cerrar una fase.
- Pruebas moderadas y dirigidas a lo que realmente se rompe (parser contra
  fixtures HTML reales, aritmética del precio en parcela, integridad del
  mapeo canónico) — no cobertura formal exhaustiva.
- Al cerrar un issue: commit en español con el porqué, y actualizar el
  issue de Linear con hallazgos y decisiones antes de pasarlo a Done.
- Parte de la suite pega al SNIIM en vivo (robots, GET sin estado, cotejo de
  catálogos). Lo demás corre offline contra fixtures.
- Restricción de costo: GCP ≤ USD 20/mes. Vistas/tablas precalculadas,
  nunca agregación en tiempo real desde la app (`@st.cache_data`).
