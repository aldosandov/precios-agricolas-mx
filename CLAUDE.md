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
alerta llega a Sentry con el DSN real (ALD-25).

**Fase 2 en curso.** Cerrados ALD-53 (consola de progreso), ALD-26 (tabla de
cobertura y reanudación), ALD-54 (`errback`), ALD-30 (spider de backfill),
ALD-55 (carga y purga), ALD-57 (desacoplar la carga, cargar por año) y ALD-56
(documentación operativa). Sigue ALD-27 (la corrida). El backfill **se ejecuta
a mano en la laptop**, en sesiones de tiempo libre a lo largo de varios días:
arrancar y parar es el modo normal de operación, no una falla.

La documentación operativa **vive en el README, no en `docs/backfill.md`**
(ALD-56 se ajustó a eso): cómo funciona el scraping, el esquema de
`crudo.precios`, la estrategia diaria vs. histórica, una sección de idempotencia
que explica en cuatro capas por qué repetir trabajo nunca duplica ni pierde
filas, las consultas de comprobación —SQLite y BigQuery— y una tabla de síntoma
→ archivo. `docs/` sigue siendo solo evidencia de investigación de la fuente.

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
| `scraper/spiders/daily.py` | Barrido diario: 43 mercados activos × 2 modos, ventana de 5 días |
| `scraper/console.py` | Extensión de progreso para corridas largas (`rich`). Apagada por defecto |
| `scraper/backfill_state.py` | Rejilla del backfill, checkpoint en SQLite (incluye `cargado_en`) y contabilidad de bloques |
| `scraper/harvest.py` | Respuesta → filas y por qué una respuesta no sirve. Lo que comparten los dos spiders |
| `scraper/spiders/backfill.py` | Backfill por bloques trimestrales, reanudable entre sesiones. Su `main()` carga por año después del barrido |
| `transform/load.py` | Upsert de NDJSON a la capa cruda, un job de carga por año |
| `transform/raw/prices_table.sql` | DDL de `crudo.precios` |
| `transform/monitor.py` | Reporte de cobertura y alertas a Sentry |

Proyecto Scrapy en `scrapy.cfg` + `scraper/settings.py`; workflow en
`.github/workflows/daily.yml`. Falta el spider de backfill, `transform/
canonical|metrics/` y `app/`.

Lint con ruff configurado en `pyproject.toml` (line-length 100). `uv run ruff
check .` debe pasar limpio antes de commitear.

El barrido diario pide solo los **43 mercados activos** (`coverage.active_markets()`),
no los 49 del catálogo: seis llevan años sin publicar. Riesgo aceptado: un
mercado que reviva queda invisible hasta que se vuelva a correr
`scraper.coverage`. La ventana es de 5 días —el fin de semana se come dos, y
desde un lunes hay que alcanzar el jueves anterior—. Ampliarla no cuesta
peticiones (86 fijas), solo bytes: medido, 5 MB por corrida con 1 día, 15 MB
con 3 y 25 MB con 7.

**La fecha es la de México, no la del runner.** Los runners de Actions van en
UTC y el SNIIM publica en hora del centro de México: después de las 18:00
local, `date.today()` del runner ya es el día siguiente y pide un día que la
fuente no ha publicado (cero filas, job en verde). Todo lo que necesite "hoy"
usa `scraper.query.source_date()`. Encontrado en la primera corrida real en
Actions.

Ojo con Scrapy ≥2.13: el entry point es `async def start()`. Definir solo
`start_requests()` no falla — el spider rastrea cero páginas y reporta
`finished`.

Toda petición lleva `errback`. Scrapy no manda al `errback` lo que reintenta:
agotados los `RETRY_TIMES` devuelve la respuesta, sube `retry/max_reached` y
sigue; lo que sí llega es el `HttpError` que levanta `HttpErrorMiddleware`
después, y las excepciones de transporte. Sin `errback` esa ventana no entra a
`failures`, no llega a `out/failures.txt`, no aparece en Sentry y la corrida
termina en verde con un mercado de menos (ALD-54).

La consola de progreso (`scraper/console.py`, ALD-53) solo se enciende con
`PROGRESS_CONSOLE_ENABLED`, y quien la enciende **tiene que fijar `LOG_FILE` en
la misma corrida**: es lo único que le quita a Scrapy el handler de consola, y
se lee cuando se configura el logging, así que no se puede fijar más tarde. Sin
eso, el log y el área viva se pelean la terminal y no sobrevive ninguno. Lee
dos miembros opcionales del spider —`failures: list[str]` y
`progress() -> Snapshot`—; sin el segundo la barra no tiene total y solo cuenta
respuestas. Los handlers de señales no declaran `spider` ni `item`: pydispatch
solo pasa los argumentos que el receptor acepta.

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

## Rejilla y checkpoint del backfill (ALD-26)

`scraper/backfill_state.py`. La unidad de trabajo es **(mercado, modo de
precio, trimestre)**; el estado vive en `out/cobertura.sqlite`, no versionado.

- La rejilla sale de `market_coverage_by_year.csv`, **nunca del calendario**:
  solo años con datos. Eso deja fuera los dos mercados muertos, los quince que
  arrancan tarde y los huecos internos. Son **9 628 bloques**, no 9 712: el
  trimestre en curso se recorta a hoy y el resto del año no se pide.
- **Solo `hecho` con `cargado_en` saca un bloque de la cola** (ALD-57). Un
  bloque `abierto` (sesión interrumpida), uno `fallido` y uno barrido pero no
  cargado vuelven igual. Y "cubierto" significa cubierto *hasta la misma
  fecha*: el trimestre en curso crece un día a la vez, así que un bloque
  cerrado ayer deja de cuadrar con la rejilla y regresa solo, sin caso especial
  para "hoy".
- El trimestre es la unidad atómica pero son muchas peticiones, porque la
  ventana se subdivide en vuelo. `BlockLedger` lleva las peticiones en vuelo
  por bloque y cierra al llegar a cero. **Una sola ventana ilegible tiñe el
  trimestre entero de `fallido`** y se reintenta completo: registrar como
  cubierto un trimestre del que faltó un pedazo es el único fallo que no deja
  rastro.
- `synchronous=FULL` en WAL, un commit por bloque. Un fsync por bloque contra
  una petición al SNIIM cada segundo y medio no se nota, y el valor entero del
  archivo es sobrevivir a la corrida que lo escribió.
- `uv run python -m scraper.backfill_state` dice cuánto falta y qué falló, sin
  tocar el spider.

Dos cosas que se midieron al cablear el spider (ALD-30) y que no son obvias:

- **Scrapy drena `start()` entero.** No hay contrapresión útil: medido, 226
  bloques abiertos mientras 4 terminaban. Sin tope, los 9 628 quedarían
  `abierto` en el primer minuto y el estado dejaría de significar nada. El
  spider espera con `await asyncio.sleep()` dentro de `start()` —eso sí le
  devuelve el reactor— hasta que haya menos de `MAX_OPEN_BLOCKS` en vuelo.
- **`PartitionWriter` tenía un techo de descriptores.** Un trimestre son ~90
  fechas × 47 mercados × 2 modos = **8 460 archivos abiertos a la vez**, o sea
  `EMFILE` en cualquier máquina con el 1024 de siempre. Ahora hay un tope LRU
  de 512: la regla de "truncar la primera vez, anexar después" ya permitía
  cerrar y reabrir, solo faltaba hacerlo. Esto sí es del lado del barrido y
  sigue vivo, a diferencia de todo lo demás que leía particiones a media
  corrida.

## Carga por año, después del barrido (ALD-55 + ALD-57)

**La carga ya no corre dentro del barrido.** ALD-55 la acopló al crawl y
funcionó, pero los tres bugs que costó salieron todos de la misma raíz:
compartir proceso y ciclo de vida con el reactor. Ninguno existe si la carga
corre después, con el reactor muerto y el `PartitionWriter` cerrado. Están
documentados abajo porque explican por qué el orden es este, no porque sigan
vivos.

Orden de `backfill.main()`:

1. cargar y purgar lo que ya estuviera en `out/raw`, y marcarlo;
2. barrer;
3. cargar y purgar lo de esta sesión, por año, y marcarlo.

El paso 1 es lo que evita tirar peticiones: una sesión que muere entre el
barrido y la carga deja bloques `hecho` sin `cargado_en` **y** su NDJSON en
disco. `--no-load` salta 1 y 3; `--solo-cargar` hace 1 y termina.

- **`hecho` ya no significa "a salvo".** Un bloque sale de la cola con
  `estado = 'hecho' AND cargado_en IS NOT NULL`. Al diferir la carga, el NDJSON
  en disco es la única copia durante toda la sesión: perderlo devuelve esos
  bloques a la cola en vez de perder datos. Un bloque que capturó **cero filas**
  se marca cargado al cerrar —no escribió partición, no hay nada que perder—; si
  no, se repetiría cada sesión para siempre.
- Correr `transform.load --purge` a mano **no marca** `cargado_en`: esos bloques
  se vuelven a pedir. Fallo seguro (cuesta peticiones, no datos). Para cargar a
  mano lo del backfill está `--solo-cargar`.
- **El lote es el año calendario**, no el trimestre: un bloque nunca cruza el
  año, así que agrupar por año no parte ninguna unidad de trabajo. 29 jobs de
  carga y 29 `MERGE` para el histórico completo, contra 116 por trimestre.
- **`transform.load` manda un solo job de carga por lote, no uno por archivo.**
  BigQuery permite **1 500 jobs de carga por tabla por día**; por archivo el
  histórico serían ~30 000 jobs. Medido antes del arreglo: 198 jobs para medio
  año de un mercado. Concatena a un temporal y carga una vez.
- **Antes de purgar se cuentan las filas del disco contra las de la tabla de
  paso**, y solo después se marca el año. Un año que falló en cualquier punto
  deja sus particiones donde están y sus bloques en la cola.
- El costo en BigQuery **no es la razón para nada de esto**: los jobs de carga
  son cuota y no cobro, los `MERGE` del backfill completo escanean ~16 GB
  (free tier: 1 TiB/mes) y el almacenamiento son ~$0.33/mes. Lo que sí sostiene
  eso es el **orden cronológico de la rejilla**: el rango de fechas poda el
  `MERGE` al año cargado. Barrer por mercado haría que cada `MERGE` leyera el
  histórico entero: ~620 GB acumulados, $3.90.
- El spider **nunca importa `transform/`**. El único lugar donde las dos
  mitades se conocen es el `main()` del backfill.
- `Spider.closed(reason)` **no es un handler de señal**: `Spider.close` lo llama
  directo con el motivo, así que el argumento no es opcional. Quitarlo (por
  ejemplo para callar a `ARG002`) levanta un `TypeError` que Scrapy atrapa y
  loguea, y lo que hubiera que cerrar nunca se cierra.

Lo que costó acoplar la carga al crawl (ALD-55), y que ya no aplica:

1. **Scrapy procesa los items después de que el generador del callback
   terminó**, así que "todos los bloques del trimestre resolvieron" no era
   "todas las filas se escribieron": 7 585 filas a BigQuery de 7 770.
2. **Las filas se quedan en el búfer del objeto archivo** hasta cerrarlo: sin
   cerrar el rango, 7 249.
3. **El reactor se detiene en cuanto el crawl queda ocioso**, y una carga en su
   hilo en ese momento quedaba a medias: el último trimestre se cargó dos veces.

## Volúmenes (medidos en ALD-57, no estimados)

Los ~350 B por fila de las primeras versiones eran una estimación sin medir.
La medición dio **825 B**, o sea 2.4× todo lo dimensionado sobre esa base.

| | Medido |
| -- | -- |
| NDJSON por fila | 825 B |
| Por bloque | ~2.6 MB |
| Un año en disco | ~870 MB |
| Histórico completo en NDJSON | **25.3 GB** |
| Una hora de barrido | ~6.3 GB |
| `crudo.precios` al terminar | 16.4 GB |

Como nada se carga hasta que el barrido termina, **`--limit` es el control de
disco de la sesión**, no una comodidad. El arranque lo anuncia: bloques
pendientes, cuántos hará la sesión y cuánto NDJSON son.

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
